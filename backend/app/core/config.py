"""全局配置：pydantic-settings 读取环境变量 / .env。"""
from __future__ import annotations

import base64
import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("config")

# 默认开发用 JWT 密钥（生产环境必须替换，见 validate_production_config）
_DEFAULT_JWT_SECRET = "dev-insecure-secret-change-me-please-32"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 应用数据库（Docker 内为 PostgreSQL；本地开发/测试可指向 SQLite）
    app_db_url: str = "sqlite+aiosqlite:///./local/data_agent.db"

    # 运行环境与时区：app_env=production 时启用生产配置强制校验（见 validate_production_config）
    app_env: str = "dev"
    app_tz: str = "Asia/Shanghai"

    # 安全
    jwt_secret: str = _DEFAULT_JWT_SECRET
    jwt_expire_hours: int = 24 * 7
    secret_encryption_key: str = ""  # base64(32B)；空则用 jwt_secret 派生（仅限开发）

    # 登录限流：连续 login_max_fails 次密码错误锁定 login_lock_minutes 分钟
    login_max_fails: int = 5
    login_lock_minutes: int = 10

    # 告警 Webhook（空 = 不发送；供调度/扫描等失败告警使用）
    alert_webhook_url: str = ""

    # 默认管理员
    default_admin_username: str = "admin"
    default_admin_password: str = "admin12345"

    # Qdrant / Tavily
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    tavily_api_key: str = ""

    # 演示库
    demo_db_enabled: bool = True
    demo_db_path: str = "./local/ecom_demo.db"

    # 查询性能与安全
    query_cache_ttl_sec: int = 300
    sql_max_rows: int = 10000
    query_timeout_sec: int = 30
    scan_batch_size: int = 20

    # 应用行为
    bootstrap_create_all: bool = True
    cors_origins: str = "*"

    # 代理 / Skill / 任务回收可调参数
    agent_max_steps: int = 8            # 建模代理单轮会话步数上限
    skill_python_timeout_sec: int = 30  # Python 型 Skill 沙箱执行超时
    stale_scan_job_minutes: int = 30    # RUNNING 超过该时长的扫描任务视为崩溃遗留
    scheduler_catchup_hours: int = 24   # 调度器补跑回溯窗口（供调度模块使用）

    # AI 建模工程师（PRD 3.3D-6）：内置编码代理引擎（吸收 OpenAI Codex 核心架构），
    # 代理所用 LLM 经 modeling_agent 场景绑定配置，无需额外引擎配置

    @property
    def encryption_key_bytes(self) -> bytes:
        """32 字节 AES 主密钥。优先 SECRET_ENCRYPTION_KEY（base64）；否则由 jwt_secret 派生（开发用）。"""
        raw = self.secret_encryption_key.strip()
        if raw:
            try:
                key = base64.b64decode(raw)
                if len(key) == 32:
                    return key
            except Exception:
                pass
        # 开发降级：sha256(jwt_secret)
        import hashlib

        return hashlib.sha256(self.jwt_secret.encode()).digest()

    @property
    def is_sqlite_app_db(self) -> bool:
        return self.app_db_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"


def validate_production_config(s: Settings | None = None) -> None:
    """生产模式配置强制校验：app_env=production 时关键安全项不合规则拒绝启动。

    不合规项会在错误信息中逐项列出；非 production 仅打 warning 不阻断（本地开发便利）。
    由 app.main 应用启动时调用。
    """
    s = s or settings
    problems: list[str] = []
    if s.jwt_secret == _DEFAULT_JWT_SECRET:
        problems.append("JWT_SECRET 仍为默认开发值，必须替换为随机强密钥")
    if not s.secret_encryption_key.strip():
        problems.append("SECRET_ENCRYPTION_KEY 未配置（base64 编码的 32 字节密钥），"
                        "生产环境不允许由 jwt_secret 派生主密钥")
    if not problems:
        return
    detail = "\n".join(f"  - {p}" for p in problems)
    if s.is_production:
        raise RuntimeError(
            "生产环境（APP_ENV=production）配置不合规，拒绝启动：\n" + detail)
    logger.warning("安全配置不合规（开发模式仅告警，生产模式将拒绝启动）：\n%s", detail)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# 便于环境变量注入（tests / bootstrap 脚本）
def reload_settings() -> Settings:
    global settings
    get_settings.cache_clear()
    settings = get_settings()
    return settings
