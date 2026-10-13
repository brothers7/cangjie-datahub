"""SQL 安全守卫（PRD 3.6 / 4.4）：生成 SQL 执行前必须通过。

- 仅单条 SELECT（含 WITH/UNION）
- 表白名单（数据源元数据 ∧ 用户权限）
- 危险函数黑名单
- 强制 LIMIT（AST 改写）
- 方言转换（LLM 输出通用 SQL，此处统一转目标方言）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

DIALECT_MAP = {"mysql": "mysql", "postgresql": "postgres", "clickhouse": "clickhouse",
               "sqlite": "sqlite", "hive": "hive", "spark": "spark", "doris": "doris",
               "starrocks": "starrocks"}
# sqlglot 30+ 取消了 "generic" 方言名，通用方言用空串（即默认）表示
GENERIC_DIALECT = ""

FORBIDDEN_FUNCTIONS = {
    "sleep", "pg_sleep", "benchmark", "load_file", "copy", "pg_read_file", "pg_read_binary_file",
    "dblink", "dblink_exec", "lo_import", "lo_export", "pg_ls_dir", "pg_terminate_backend",
    "remotetables", "remote", "s3", "url", "file", "mysql_query", "sys_exec", "dbms_sql",
    "utl_file", "xp_cmdshell",
}
STATEMENT_KEYWORDS_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|call|set|use|pragma|attach|vacuum|analyze|comment on|lock|unlock)\b",
    re.IGNORECASE,
)
_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


@dataclass
class GuardResult:
    ok: bool
    sql: str = ""
    violations: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)


def guard_sql(sql: str, dialect: str, allowed_tables: set[str], max_rows: int = 10000,
              current_db: str = "") -> GuardResult:
    violations: list[str] = []
    allowed = {t.lower() for t in allowed_tables}

    sql = (sql or "").strip()
    # 关键词粗筛（双保险）：先剥注释与字符串字面量，避免字面量里的 SQL 词误杀合法查询
    stripped = re.sub(r"--[^\n]*|/\*.*?\*/", "", sql, flags=re.S)
    stripped = _STRING_LITERAL_RE.sub("''", stripped)
    kw = STATEMENT_KEYWORDS_RE.search(stripped)
    if kw:
        violations.append(f"包含禁止的关键字: {kw.group(1).upper()}")

    try:
        statements = sqlglot.parse(sql, read=None)
    except Exception as e:
        return GuardResult(False, sql, [f"SQL 解析失败: {e}"])
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        violations.append(f"必须且只能包含一条语句（当前 {len(statements)} 条）")
        return GuardResult(False, sql, violations)
    tree = statements[0]

    # 根节点类型：仅 SELECT（含 CTE/UNION）
    if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery)):
        violations.append(f"仅允许 SELECT 查询（检测到 {tree.__class__.__name__}）")
        return GuardResult(False, sql, violations)
    if isinstance(tree, exp.Subquery):
        inner = tree.this
        if not isinstance(inner, (exp.Select, exp.Union)):
            violations.append("仅允许 SELECT 查询")
            return GuardResult(False, sql, violations)

    # 表白名单
    # CTE 名会被 sqlglot 解析成 Table 节点（`WITH t AS (...) SELECT * FROM t` 里 t 就是 Table），
    # 不先收集别名的话，合法的 WITH 查询会被误判成「引用了未授权表」而拦下来。
    cte_names = {c.alias.lower() for c in tree.find_all(exp.CTE) if c.alias}
    tables: list[str] = []
    for node in tree.walk():
        node = node[0] if isinstance(node, tuple) else node
        if isinstance(node, exp.Table):
            name = (node.name or "").lower()
            if name in cte_names:
                continue
            # schema/catalog 限定名（db.table）：限定库必须是当前数据源库，
            # 否则 `SELECT * FROM other_db.users` 可借撞名表绕过白名单
            qualifier = (node.db or node.catalog or "")
            if qualifier and current_db and qualifier.lower() != current_db.lower():
                violations.append(
                    f"不允许访问当前数据源之外的库表: {qualifier}.{name}")
                continue
            tables.append(name)
            if allowed and name not in allowed:
                violations.append(f"引用了未授权/未知的表: {name}")
        elif isinstance(node, exp.Into):
            violations.append("包含 INTO 写入子句")

    # 危险函数
    for node in tree.walk():
        node = node[0] if isinstance(node, tuple) else node
        fname = ""
        if isinstance(node, exp.Anonymous):
            fname = (node.this or "").lower() if isinstance(node.this, str) else ""
        elif isinstance(node, exp.Func):
            fname = node.sql_name().lower()
        if fname in FORBIDDEN_FUNCTIONS:
            violations.append(f"包含禁止的函数: {fname}")

    if violations:
        return GuardResult(False, sql, violations, tables)

    # 强制 LIMIT
    limit_expr = tree.args.get("limit") if isinstance(tree, exp.Select) else None
    current_limit = None
    if limit_expr is not None:
        try:
            current_limit = int(limit_expr.expression.name) if hasattr(limit_expr, "expression") else None
        except Exception:
            current_limit = None
    if current_limit is None or current_limit > max_rows:
        tree = tree.limit(max_rows, copy=False)

    out_dialect = DIALECT_MAP.get(dialect, dialect)
    try:
        final_sql = tree.sql(dialect=out_dialect or None, pretty=True)
    except Exception:
        final_sql = tree.sql(dialect=None, pretty=True)

    return GuardResult(True, final_sql, [], sorted(set(tables)))


# 用户原始输入的注入模式检测（进入提示词前清洗告警）
INJECTION_PATTERNS = [
    r";\s*--", r"'\s*OR\s+'", r"\bunion\s+select\b", r"--\s*$", r"/\*.*?\*/",
    r"\bxp_cmdshell\b", r"\bwaitfor\s+delay\b", r"\bsys\.sql\b",
    # 分号后接任意语句 = 堆叠注入（"1; DROP TABLE t"）。
    # 正常中文提问不会出现「分号+SQL 动词」，误报代价远低于漏报。
    r";\s*\w+\s+\w+",
]


def detect_injection(text: str) -> str | None:
    for pat in INJECTION_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return pat
    return None
