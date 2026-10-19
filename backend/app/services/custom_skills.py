"""自定义 Skill 托管（PRD 3.3C）：包解析 / 静态安检 / 路由匹配 / 执行。

两类 Skill：
- prompt：提示词模板，走 LLM 网关（支持 ${question} / ${datasource} / ${kg_context} 占位符）
- python：沙箱子进程执行 main.py 的 run(question, ctx)；ctx 为平台预取的只读快照
  （数据源信息 + 表清单 + 指标清单），沙箱不持有任何数据库凭证，也无法直接查库。

安全：上传时静态黑名单扫描 + zip 炸弹声明大小预检；运行时超时/内存限制/环境变量清空
+ 路径围栏（chroot 优先，降级为 open/os.open/io.open monkeypatch，禁读沙箱外绝对路径）；
网络访问靠静态扫描拦截（socket/requests/urllib/http/ssl/asyncio 等命中即拒绝上传）。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import resource
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field

import yaml

from app.core.errors import ValidationError

logger = logging.getLogger(__name__)

MAX_ZIP_BYTES = 1024 * 1024          # 1MB
MAX_FILE_BYTES = 256 * 1024          # 单文件 256KB
MAX_FILES = 20
PYTHON_TIMEOUT_SEC = 30
PYTHON_MEM_LIMIT = 256 * 1024 * 1024  # 256MB

# Python 型静态黑名单（命中即拒绝上传）
_PY_BLACKLIST: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bos\.(system|popen|spawn|exec[lv])"), "os.system/popen 等进程调用"),
    (re.compile(r"\bimport\s+subprocess|\bfrom\s+subprocess\b"), "subprocess"),
    (re.compile(r"\bimport\s+(socket|requests|urllib|urllib3|httpx|aiohttp|http|ftplib|smtplib|ssl)\b|"
                r"\bfrom\s+(socket|requests|urllib|urllib3|httpx|aiohttp|http|ftplib|smtplib|ssl)\b"),
     "网络/协议访问库"),
    (re.compile(r"\bimport\s+asyncio\b|\bfrom\s+asyncio\b"), "asyncio（open_connection 等网络能力）"),
    (re.compile(r"\bimport\s+pathlib\b|\bfrom\s+pathlib\b"), "pathlib 文件系统访问"),
    (re.compile(r"\b(os|io)\.open\s*\("), "os.open/io.open 文件句柄"),
    (re.compile(r"\bimport\s+(ctypes|multiprocessing|shutil|pty)\b"), "ctypes/multiprocessing/shutil"),
    (re.compile(r"\b(eval|exec)\s*\("), "eval/exec 动态执行"),
    (re.compile(r"\bopen\s*\([^)]*['\"][wax]\+?['\"]"), "文件写入"),
    (re.compile(r"__import__\s*\("), "__import__ 动态导入"),
    (re.compile(r"\bimport\s+importlib\b|import_module\s*\("), "importlib 动态导入"),
    (re.compile(r"\bfrom\s+os\s+import\b"), "os 成员导入（system 等）"),
    (re.compile(r"\bimport\s+(sqlalchemy|pymysql|psycopg|clickhouse)\b"), "数据库直连库（必须走平台 ctx）"),
]


@dataclass
class SkillPackage:
    name: str
    description: str
    skill_type: str               # prompt / python
    trigger_keywords: list[str] = field(default_factory=list)
    entry: str = ""               # prompt.md / main.py
    files: dict = field(default_factory=dict)


def _safe_member(name: str) -> bool:
    return (not name.startswith("/") and ".." not in name.split("/")
            and "__pycache__" not in name and not name.endswith((".pyc", ".pyo")))


def parse_package(zip_bytes: bytes) -> SkillPackage:
    """解析并校验上传的 Skill zip 包。非法即抛 ValidationError。"""
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise ValidationError(f"Skill 包超过大小上限（{MAX_ZIP_BYTES // 1024}KB）")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise ValidationError("不是合法的 zip 文件") from e

    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names or len(names) > MAX_FILES:
        raise ValidationError(f"包内文件数异常（需 1-{MAX_FILES} 个）")
    bad = [n for n in names if not _safe_member(n)]
    if bad:
        raise ValidationError(f"包内含不安全路径: {bad[0]}")

    files: dict[str, str] = {}
    for n in names:
        info = zf.getinfo(n)
        # zip 炸弹防御：先看声明的解压后大小，超限直接拒绝，不读内容
        if info.file_size > MAX_FILE_BYTES:
            raise ValidationError(f"文件 {n} 声明大小超过上限（256KB），拒绝解压（防 zip 炸弹）")
        data = zf.read(n)
        if len(data) > MAX_FILE_BYTES:
            raise ValidationError(f"文件 {n} 超过大小上限（256KB）")
        try:
            files[n] = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ValidationError(f"文件 {n} 不是 UTF-8 文本（仅支持文本型 Skill）") from e

    manifest_name = next((n for n in files if n.endswith("skill.yaml")
                          or n.endswith("skill.yml")), None)
    if not manifest_name:
        raise ValidationError("缺少清单文件 skill.yaml")
    try:
        manifest = yaml.safe_load(files[manifest_name]) or {}
    except yaml.YAMLError as e:
        raise ValidationError(f"skill.yaml 解析失败: {e}") from e

    name = str(manifest.get("name") or "").strip()
    if not re.match(r"^[a-zA-Z0-9_一-鿿-]{2,64}$", name):
        raise ValidationError("skill.yaml 中 name 不合法（2-64 位字母/数字/中文/下划线）")
    skill_type = str(manifest.get("type") or "prompt").lower()
    if skill_type not in ("prompt", "python", "scheduler"):
        raise ValidationError("skill.yaml 中 type 必须是 prompt / python / scheduler")

    keywords = manifest.get("trigger_keywords") or []
    if not isinstance(keywords, list) or not all(isinstance(k, str) and k.strip() for k in keywords):
        raise ValidationError("trigger_keywords 必须是非空字符串列表")
    # scheduler 型不参与对话路由，无需触发词；prompt/python 型必须配置
    if not keywords and skill_type != "scheduler":
        raise ValidationError("至少配置一个 trigger_keywords，否则 Skill 无法被触发")

    # 入口文件
    if skill_type == "prompt":
        entry = str(manifest.get("entry") or "prompt.md")
        if entry not in files:
            raise ValidationError(f"缺少入口文件 {entry}")
    else:
        entry = str(manifest.get("entry") or "main.py")
        src = files.get(entry)
        if src is None:
            raise ValidationError(f"缺少入口文件 {entry}")
        if skill_type == "scheduler":
            # 调度平台契约：build_package(task) -> {filename, content, note}
            if "def build_package(" not in src:
                raise ValidationError(
                    f"{entry} 必须定义调度平台契约函数 def build_package(task: dict) -> dict")
        elif "def run(" not in src:
            raise ValidationError(f"{entry} 必须定义入口函数 def run(question, ctx)")
        hits = [label for pat, label in _PY_BLACKLIST if pat.search(src)]
        # 只扫描 .py 文件
        for fn, content in files.items():
            if fn.endswith(".py") and fn != entry:
                hits += [f"{fn}: {label}" for pat, label in _PY_BLACKLIST if pat.search(content)]
        if hits:
            raise ValidationError(f"静态安全检查未通过，检出危险调用: {'；'.join(sorted(set(hits)))}")

    return SkillPackage(name=name, description=str(manifest.get("description") or ""),
                        skill_type=skill_type,
                        trigger_keywords=[k.strip() for k in keywords][:20],
                        entry=entry, files=files)


# ---------- 路由匹配 ----------

def match_by_keywords(skills: list, question: str):
    """触发词命中（取命中词最长的 Skill，词越长越具体）。"""
    q = question.lower()
    best, best_len = None, 0
    for s in skills:
        for kw in s.trigger_keywords or []:
            if kw and kw.lower() in q and len(kw) > best_len:
                best, best_len = s, len(kw)
    return best


# ---------- Prompt 型执行 ----------

def render_prompt(template: str, *, question: str, datasource: str, kg_context: str) -> str:
    out = template
    for k, v in (("question", question), ("datasource", datasource),
                 ("kg_context", kg_context)):
        out = out.replace("${" + k + "}", v)
    return out


# ---------- Python 型沙箱执行 ----------

# 运行时路径围栏（注入 runner 子进程，在 import main 之前生效）：
# posix 下优先尝试 chroot 到沙箱临时目录（需要 root，通常不可用），
# 失败静默降级为 monkeypatch builtins.open / os.open / io.open 的方案，
# 拒绝访问沙箱目录之外的绝对路径（解释器标准库 / site-packages 前缀只读放行，
# 保证 skill 正常 import 系统库不受影响）。
_FENCE = r"""
import json, sys, os, io, contextlib
import builtins as _builtins

# 1) 优先 chroot（有权限时是最强隔离）；失败静默降级
_chrooted = False
if hasattr(os, "chroot") and hasattr(os, "geteuid"):
    try:
        if os.geteuid() == 0:
            os.chroot(os.getcwd())
            os.chdir("/")
            _chrooted = True
    except OSError:
        _chrooted = False

if not _chrooted:
    _sandbox_root = os.path.realpath(os.getcwd())
    # 解释器目录前缀（标准库/site-packages 只读放行）
    _allow_prefixes = []
    for _p in (os.path.dirname(os.__file__), sys.base_prefix, sys.prefix,
               getattr(sys, "exec_prefix", sys.prefix)):
        if _p:
            _rp = os.path.realpath(_p)
            if _rp not in _allow_prefixes:
                _allow_prefixes.append(_rp)
    _allow_prefixes = tuple(_allow_prefixes)

    def _path_allowed(path):
        try:
            rp = os.path.realpath(os.path.abspath(os.fsdecode(path)))
        except Exception:
            return False
        if rp == _sandbox_root or rp.startswith(_sandbox_root + os.sep):
            return True
        return rp.startswith(_allow_prefixes)

    _orig_open = _builtins.open
    _orig_os_open = os.open
    _orig_io_open = io.open

    def _safe_open(file, mode="r", *a, **kw):
        if isinstance(file, (str, bytes, os.PathLike)) and not _path_allowed(file):
            raise PermissionError(f"沙箱禁止访问沙箱目录外的路径: {file}")
        return _orig_open(file, mode, *a, **kw)

    def _safe_os_open(path, flags, mode=0o777, *a, **kw):
        if not _path_allowed(path):
            raise PermissionError(f"沙箱禁止访问沙箱目录外的路径: {path}")
        return _orig_os_open(path, flags, mode, *a, **kw)

    _builtins.open = _safe_open
    io.open = _safe_open
    os.open = _safe_os_open
"""

_RUNNER = r"""
import json, sys, os, io, contextlib

sys.path.insert(0, os.getcwd())  # -I 隔离模式会把脚本目录移出 sys.path，手动加回

payload = json.loads(sys.stdin.read())
question, ctx = payload["question"], payload["ctx"]

buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf):
        import main
        result = main.run(question, ctx)
    out = {"ok": True, "result": str(result), "log": buf.getvalue()[-2000:]}
except Exception as e:
    out = {"ok": False, "error": f"{e.__class__.__name__}: {e}", "log": buf.getvalue()[-2000:]}
sys.stdout.write("__DA_RESULT__" + json.dumps(out, ensure_ascii=False))
"""


def _limits() -> None:  # pragma: no cover - 子进程内执行
    try:
        resource.setrlimit(resource.RLIMIT_AS, (PYTHON_MEM_LIMIT, PYTHON_MEM_LIMIT))
        resource.setrlimit(resource.RLIMIT_CPU, (PYTHON_TIMEOUT_SEC, PYTHON_TIMEOUT_SEC))
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    except (ValueError, OSError):
        pass  # 部分平台（如 macOS 对 RLIMIT_AS）不支持，尽力而为


def _run_python_sync(files: dict, entry: str, question: str, ctx: dict) -> dict:
    """同步执行（经 asyncio.to_thread 调用）。返回 {ok, result|error, log}。"""
    with tempfile.TemporaryDirectory(prefix="da_skill_") as td:
        for fn, content in files.items():
            path = os.path.join(td, fn)
            os.makedirs(os.path.dirname(path), exist_ok=True) if "/" in fn else None
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        with open(os.path.join(td, "_runner.py"), "w", encoding="utf-8") as f:
            f.write(_FENCE + _RUNNER)   # 路径围栏必须先于 import main 生效
        proc = subprocess.run(
            [sys.executable, "-I", "_runner.py"],   # -I: 隔离模式，忽略用户 site/环境
            input=json.dumps({"question": question, "ctx": ctx}, ensure_ascii=False),
            capture_output=True, text=True, cwd=td,
            timeout=PYTHON_TIMEOUT_SEC, env={"PATH": os.environ.get("PATH", "")},
            preexec_fn=_limits if os.name == "posix" else None,
        )
        out = proc.stdout or ""
        marker = "__DA_RESULT__"
        if marker not in out:
            err = (proc.stderr or out)[-300:]
            return {"ok": False, "error": f"Skill 进程异常退出（code={proc.returncode}）: {err}",
                    "log": ""}
        try:
            return json.loads(out.split(marker, 1)[1])
        except ValueError:
            return {"ok": False, "error": "Skill 输出无法解析", "log": ""}


# ---------- 调度平台型 Skill（PRD 3.3D-4：build_package 契约） ----------

_SCHED_RUNNER = r"""
import json, sys, os, io, contextlib

sys.path.insert(0, os.getcwd())

task = json.loads(sys.stdin.read())

buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf):
        import main
        result = main.build_package(task)
    if not isinstance(result, dict) or not result.get("filename") or "content" not in result:
        out = {"ok": False, "error": "build_package 必须返回 {filename, content, note}", "log": buf.getvalue()[-2000:]}
    else:
        out = {"ok": True, "result": {"filename": str(result["filename"]),
                                      "content": str(result["content"]),
                                      "note": str(result.get("note", ""))},
               "log": buf.getvalue()[-2000:]}
except Exception as e:
    out = {"ok": False, "error": f"{e.__class__.__name__}: {e}", "log": buf.getvalue()[-2000:]}
sys.stdout.write("__DA_RESULT__" + json.dumps(out, ensure_ascii=False))
"""


def _run_scheduler_sync(files: dict, entry: str, task: dict) -> dict:
    """调度 Skill 沙箱执行（禁网/禁写盘/超时，同 Python 型约束）。"""
    with tempfile.TemporaryDirectory(prefix="da_sched_") as td:
        for fn, content in files.items():
            path = os.path.join(td, fn)
            os.makedirs(os.path.dirname(path), exist_ok=True) if "/" in fn else None
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        with open(os.path.join(td, "_runner.py"), "w", encoding="utf-8") as f:
            f.write(_FENCE + _SCHED_RUNNER)   # 路径围栏必须先于 import main 生效
        proc = subprocess.run(
            [sys.executable, "-I", "_runner.py"],
            input=json.dumps(task, ensure_ascii=False),
            capture_output=True, text=True, cwd=td,
            timeout=PYTHON_TIMEOUT_SEC, env={"PATH": os.environ.get("PATH", "")},
            preexec_fn=_limits if os.name == "posix" else None,
        )
        out = proc.stdout or ""
        marker = "__DA_RESULT__"
        if marker not in out:
            err = (proc.stderr or out)[-300:]
            return {"ok": False, "error": f"调度 Skill 进程异常退出（code={proc.returncode}）: {err}",
                    "log": ""}
        try:
            return json.loads(out.split(marker, 1)[1])
        except ValueError:
            return {"ok": False, "error": "调度 Skill 输出无法解析", "log": ""}


async def run_scheduler_skill(files: dict, entry: str, task: dict) -> dict:
    try:
        return await asyncio.to_thread(_run_scheduler_sync, files, entry, task)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"调度 Skill 执行超时（>{PYTHON_TIMEOUT_SEC}s），已终止", "log": ""}
    except Exception as e:  # noqa: BLE001
        logger.exception("scheduler skill sandbox failed")
        return {"ok": False, "error": f"沙箱执行失败: {e}", "log": ""}


async def run_python_skill(files: dict, entry: str, question: str, ctx: dict) -> dict:
    try:
        return await asyncio.to_thread(_run_python_sync, files, entry, question, ctx)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Skill 执行超时（>{PYTHON_TIMEOUT_SEC}s），已终止", "log": ""}
    except Exception as e:  # noqa: BLE001
        logger.exception("custom skill sandbox failed")
        return {"ok": False, "error": f"沙箱执行失败: {e}", "log": ""}
