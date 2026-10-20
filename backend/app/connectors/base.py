"""数仓连接器抽象与公共实现（同步引擎 + asyncio.to_thread 包装）。"""
from __future__ import annotations

import asyncio
import decimal
import datetime as dt
import logging
import time
from abc import ABC
from dataclasses import dataclass, field
from urllib.parse import quote_plus

import sqlalchemy as sa

from app.core.config import settings
from app.core.crypto import decrypt
from app.db.models.datasource import DataSource, DbType

logger = logging.getLogger(__name__)


def jsonify_value(v):
    """把数据库返回值转为 JSON 安全类型。"""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, bytes):
        return v[:64].hex()
    return str(v)


@dataclass
class ColumnMeta:
    column_name: str
    data_type: str = ""
    comment: str = ""
    is_primary_key: bool = False
    nullable: bool = True


@dataclass
class TableMeta:
    schema_name: str = ""
    table_name: str = ""
    comment: str = ""
    columns: list[ColumnMeta] = field(default_factory=list)
    row_count: int | None = None
    foreign_keys: list[dict] = field(default_factory=list)  # [{column, ref_table, ref_column}]
    create_time: dt.datetime | None = None    # 源库建表时间（方言相关，取不到留空）
    storage_format: str | None = None         # 存储格式（Hive/Spark：TextFile/Parquet/ORC 等）


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    duration_ms: int = 0


class Connector(ABC):
    """一个数据源连接器实例。"""

    db_type: DbType = DbType.MYSQL

    def __init__(self, ds: DataSource):
        self.ds = ds
        self._password = decrypt(ds.password_cipher, aad=f"ds:{ds.id}") if ds.password_cipher else ""

    # ---- 子类实现 ----
    def build_url(self) -> str:
        raise NotImplementedError

    def create_engine(self) -> sa.Engine:
        return sa.create_engine(self.build_url(), pool_pre_ping=True, pool_size=4, max_overflow=4)

    # ---- 公共能力 ----
    def engine(self) -> sa.Engine:
        """带 TTL 的引擎缓存：数据源配置变更后自动重建。"""
        key = (str(self.ds.id), self.ds.updated_at.timestamp() if self.ds.updated_at else 0)
        cache: dict = getattr(Connector, "_engine_cache", None) or {}
        Connector._engine_cache = cache
        hit = cache.get(key)
        if hit is not None:
            return hit
        # 清理同数据源旧引擎
        for k in [k for k in cache if k[0] == key[0]]:
            old = cache.pop(k)
            try:
                old.dispose()
            except Exception:
                pass
        eng = self.create_engine()
        cache[key] = eng
        return eng

    @classmethod
    def drop_cached_engine(cls, ds_id: str) -> None:
        """销毁指定数据源的引擎缓存（PRD 4.4：删除数据源时不留残留连接）。"""
        cache: dict = getattr(cls, "_engine_cache", None) or {}
        for k in [k for k in cache if k[0] == str(ds_id)]:
            old = cache.pop(k)
            try:
                old.dispose()
            except Exception:
                pass

    def _session_readonly_statements(self) -> list[str]:
        """执行查询前的会话级只读/超时设置。"""
        return []

    def _quote_ident(self, name: str) -> str:
        """标识符引用：默认双引号（SQL 标准）。

        MySQL 系方言（mysql/doris/starrocks）默认 sql_mode 下双引号是字符串常量，
        `COUNT(DISTINCT "col")` 会退化成对常量计数（恒为 1），子类覆盖为反引号。
        """
        return '"' + name.replace('"', '""') + '"'

    def _readonly_statements(self) -> list[str]:
        """会话级只读设置：仅当数据源开启「强制只读会话」时下发（PRD 4.4）。"""
        return []

    def _timeout_statement(self) -> str | None:
        return None

    def _session_statements(self) -> list[str]:
        """查询前下发的会话语句：只读（受开关控制）+ 超时（始终）。"""
        stmts = self._session_readonly_statements()
        # 「强制只读会话」开关（默认开）：开启时下发 READ ONLY；关闭时仅保留超时
        if getattr(self.ds, "readonly_enforced", True):
            stmts = stmts + self._readonly_statements()
        timeout_stmt = self._timeout_statement()
        if timeout_stmt:
            stmts = stmts + [timeout_stmt]
        return stmts

    async def test(self) -> tuple[bool, str]:
        """连通性 + 写权限探测。"""

        def _run() -> tuple[bool, str]:
            try:
                with self.engine().connect() as conn:
                    conn.execute(sa.text(self._probe_sql()))
                return True, "连接成功"
            except Exception as e:
                return False, f"{e.__class__.__name__}: {e}"[:500]

        ok, msg = await asyncio.to_thread(_run)
        return ok, msg

    def _probe_sql(self) -> str:
        return "SELECT 1"

    async def execute(self, sql: str, params: dict | None = None) -> QueryResult:
        """执行只读查询（sql_guard 已在上游拦截非 SELECT）。"""
        t0 = time.monotonic()

        def _run() -> QueryResult:
            with self.engine().connect() as conn:
                for stmt in self._session_statements():
                    try:
                        conn.execute(sa.text(stmt))
                    except Exception:  # 只读/超时设置失败不阻塞查询
                        logger.debug("session stmt failed: %s", stmt)
                result = conn.execute(sa.text(sql), params or {})
                columns = list(result.keys())
                rows = [{c: jsonify_value(v) for c, v in zip(columns, row)} for row in result.fetchmany(settings.sql_max_rows)]
                return QueryResult(columns=columns, rows=rows, row_count=len(rows))

        qr = await asyncio.to_thread(_run)
        qr.duration_ms = int((time.monotonic() - t0) * 1000)
        return qr

    async def introspect(self, tables: list[str] | None = None) -> list[TableMeta]:
        """拉取表/字段元数据。tables 为 None 时拉全部（已应用白/黑名单）。"""

        def _run() -> list[TableMeta]:
            insp = sa.inspect(self.engine())
            out: list[TableMeta] = []
            schema = self._default_schema(insp)
            names = self._filter_tables(insp, schema)
            if tables is not None:
                want = set(tables)
                names = [n for n in names if n in want]
            for name in names:
                try:
                    tm = self._introspect_one(insp, schema, name)
                    out.append(tm)
                except Exception as e:
                    logger.warning("introspect %s failed: %s", name, e)
            return out

        return await asyncio.to_thread(_run)

    def _default_schema(self, insp) -> str:
        if self.db_type == DbType.POSTGRESQL:
            return "public"
        return ""

    def _filter_tables(self, insp, schema: str) -> list[str]:
        names = insp.get_table_names(schema=schema) if schema else insp.get_table_names()
        names = [n for n in names if not n.startswith("_meta_")]  # 演示库 sidecar 注释表
        wl, bl = set(self.ds.table_whitelist or []), set(self.ds.table_blacklist or [])
        if wl:
            names = [n for n in names if n in wl]
        if bl:
            names = [n for n in names if n not in bl]
        return sorted(names)

    def _introspect_one(self, insp, schema: str, name: str) -> TableMeta:
        cols_raw = insp.get_columns(name, schema=schema) if schema else insp.get_columns(name)
        pk_info = (insp.get_pk_constraint(name, schema=schema) if schema
                   else insp.get_pk_constraint(name)) or {}
        pk = set(pk_info.get("constrained_columns") or [])
        tm = TableMeta(schema_name=schema, table_name=name)
        try:
            tm.comment = self._table_comment(insp, schema, name)
        except Exception:
            pass
        for c in cols_raw:
            tm.columns.append(ColumnMeta(
                column_name=c.get("name") or "",
                data_type=str(c.get("type") or ""),
                comment=(c.get("comment") or "") if isinstance(c.get("comment"), str) else "",
                is_primary_key=c.get("name") in pk,
                nullable=bool(c.get("nullable", True)),
            ))
        try:
            for fk in (insp.get_foreign_keys(name, schema=schema) if schema else insp.get_foreign_keys(name)) or []:
                for pair in zip(fk.get("constrained_columns") or [], fk.get("referred_columns") or []):
                    tm.foreign_keys.append({
                        "column": pair[0],
                        "ref_table": (fk.get("referred_table") or "").split(".")[-1],
                        "ref_column": pair[1],
                    })
        except Exception:
            pass
        tm.row_count = self._estimate_rows(name, schema)
        try:
            tm.create_time, tm.storage_format = self._table_storage_info(name, schema)
        except Exception:  # 补采失败不阻塞主流程
            pass
        self._attach_sidecar_comments(tm)
        return tm

    def _table_storage_info(self, name: str, schema: str) -> tuple:
        """补采「建表时间、存储格式」：方言相关，默认取不到留空（子类按方言覆盖）。"""
        return None, None

    def _table_comment(self, insp, schema: str, name: str) -> str:
        if self.db_type == DbType.POSTGRESQL and schema:
            return (insp.get_table_comment(name, schema=schema) or {}).get("text", "")
        return ""

    def _estimate_rows(self, name: str, schema: str) -> int | None:
        try:
            with self.engine().connect() as conn:
                if self.db_type == DbType.SQLITE:
                    return conn.execute(
                        sa.text(f"SELECT COUNT(*) FROM {self._quote_ident(name)}")).scalar()
                return None
        except Exception:
            return None

    def _attach_sidecar_comments(self, tm: TableMeta) -> None:
        """SQLite 演示库：从 _meta_* sidecar 表读注释。"""
        if self.db_type != DbType.SQLITE:
            return
        try:
            with self.engine().connect() as conn:
                tc = conn.execute(
                    sa.text("SELECT comment FROM _meta_table_comments WHERE table_name=:t"),
                    {"t": tm.table_name},
                ).scalar()
                if tc:
                    tm.comment = tc
                for r in conn.execute(
                    sa.text("SELECT column_name, comment FROM _meta_column_comments WHERE table_name=:t"),
                    {"t": tm.table_name},
                ):
                    for c in tm.columns:
                        if c.column_name == r[0]:
                            c.comment = r[1]
        except Exception:
            pass

    async def sample_column_stats(self, table: str, columns: list[str], sample_rows: int = 2000) -> dict[str, dict]:
        """采样统计：distinct 数、非空数、样例值（用于指标/维度分类）。"""

        def _run() -> dict[str, dict]:
            stats: dict[str, dict] = {}
            if not columns:
                return stats
            qcols = ", ".join(self._quote_ident(c) for c in columns)
            sub = f"SELECT {qcols} FROM {self._quote_ident(table)} LIMIT {int(sample_rows)}"
            select_expr = ", ".join(
                f"COUNT(DISTINCT {self._quote_ident(c)}) AS d_{i}, "
                f"COUNT({self._quote_ident(c)}) AS n_{i}" for i, c in enumerate(columns)
            )
            with self.engine().connect() as conn:
                row = conn.execute(sa.text(f"SELECT {select_expr} FROM ({sub})")).mappings().first()
                rowd = dict(row) if row else {}
                for i, c in enumerate(columns):
                    distinct = rowd.get(f"d_{i}")
                    non_null = rowd.get(f"n_{i}")
                    examples: list = []
                    try:
                        qc = self._quote_ident(c)
                        ex = conn.execute(sa.text(
                            f"SELECT DISTINCT {qc} FROM ({sub}) WHERE {qc} IS NOT NULL LIMIT 15"
                        )).scalars().all()
                        examples = [jsonify_value(x) for x in ex]
                    except Exception:
                        pass
                    stats[c] = {
                        "distinct": distinct or 0,
                        "non_null": non_null or 0,
                        "ratio": (distinct / non_null) if non_null else None,
                        "examples": examples,
                    }
            return stats

        try:
            return await asyncio.to_thread(_run)
        except Exception as e:
            logger.warning("sample stats failed for %s: %s", table, e)
            return {}


def _quote(v: str) -> str:
    return quote_plus(v or "")
