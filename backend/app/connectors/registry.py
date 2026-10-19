"""各数据库类型的具体连接器与注册表。"""
from __future__ import annotations

from app.connectors.base import Connector
from app.core.errors import DatasourceError
from app.db.models.datasource import DbType

_DRIVER_HINTS = {
    DbType.MYSQL: "pymysql（pip install pymysql）",
    DbType.POSTGRESQL: "psycopg（pip install 'psycopg[binary]'）",
    DbType.CLICKHOUSE: "clickhouse-connect + clickhouse-sqlalchemy",
}


def _require(module: str, hint: str) -> None:
    try:
        __import__(module)
    except ImportError as e:
        raise DatasourceError(f"缺少数据库驱动 {module}，请安装 {hint}") from e


class MySQLConnector(Connector):
    db_type = DbType.MYSQL

    def _quote_ident(self, name: str) -> str:
        # MySQL 默认 sql_mode 下双引号是字符串常量，标识符必须用反引号
        return "`" + name.replace("`", "``") + "`"

    def build_url(self) -> str:
        _require("pymysql", _DRIVER_HINTS[DbType.MYSQL])
        from app.connectors.base import _quote
        return (f"mysql+pymysql://{_quote(self.ds.username)}:{_quote(self._password)}"
                f"@{self.ds.host}:{self.ds.port}/{_quote(self.ds.database_name)}?charset=utf8mb4")

    def _readonly_statements(self) -> list[str]:
        return ["SET SESSION TRANSACTION READ ONLY"]

    def _timeout_statement(self) -> str | None:
        return f"SET SESSION max_execution_time={settings_query_timeout_ms()}"


class PostgresConnector(Connector):
    db_type = DbType.POSTGRESQL

    def build_url(self) -> str:
        _require("psycopg", _DRIVER_HINTS[DbType.POSTGRESQL])
        from app.connectors.base import _quote
        return (f"postgresql+psycopg://{_quote(self.ds.username)}:{_quote(self._password)}"
                f"@{self.ds.host}:{self.ds.port}/{_quote(self.ds.database_name)}")

    def _readonly_statements(self) -> list[str]:
        return ["SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"]

    def _timeout_statement(self) -> str | None:
        return f"SET SESSION statement_timeout = '{settings_query_timeout_ms()}ms'"


class ClickHouseConnector(Connector):
    db_type = DbType.CLICKHOUSE

    def build_url(self) -> str:
        _require("clickhouse_connect", _DRIVER_HINTS[DbType.CLICKHOUSE])
        _require("clickhouse_sqlalchemy", _DRIVER_HINTS[DbType.CLICKHOUSE])
        from app.connectors.base import _quote
        sec = "https" if (self.ds.extra_params or {}).get("secure") else "http"
        return (f"clickhouse+connect://{_quote(self.ds.username)}:{_quote(self._password)}"
                f"@{self.ds.host}:{self.ds.port}/{_quote(self.ds.database_name)}?{sec}")

    def _default_schema(self, insp) -> str:
        return self.ds.database_name or "default"

    def _table_comment(self, insp, schema, name) -> str:
        return (insp.get_table_comment(name, schema=schema) or {}).get("text", "")


class SqliteConnector(Connector):
    db_type = DbType.SQLITE

    def build_url(self) -> str:
        path = (self.ds.extra_params or {}).get("path") or self.ds.database_name
        return f"sqlite:///{path}"


class _MySQLProtocolConnector(Connector):
    """Doris / StarRocks 兼容 MySQL 协议，复用 pymysql 驱动。
    两者都不支持事务级只读模式，但支持会话级执行超时。
    """

    def _quote_ident(self, name: str) -> str:
        # Doris / StarRocks 同 MySQL：默认 sql_mode 下双引号是字符串常量
        return "`" + name.replace("`", "``") + "`"

    def build_url(self) -> str:
        _require("pymysql", _DRIVER_HINTS[DbType.MYSQL])
        from app.connectors.base import _quote
        return (f"mysql+pymysql://{_quote(self.ds.username)}:{_quote(self._password)}"
                f"@{self.ds.host}:{self.ds.port}/{_quote(self.ds.database_name)}?charset=utf8mb4")

    def _default_schema(self, insp) -> str:
        return self.ds.database_name

    def _timeout_statement(self) -> str | None:
        # Doris / StarRocks 以秒为单位
        return f"SET SESSION query_timeout = {settings_query_timeout_ms() // 1000}"


class DorisConnector(_MySQLProtocolConnector):
    db_type = DbType.DORIS


class StarRocksConnector(_MySQLProtocolConnector):
    db_type = DbType.STARROCKS


class _ThriftConnector(Connector):
    """Hive / Spark SQL：通过 PyHive 走 HiveServer2 Thrift 接口。
    SQLAlchemy 方言由 PyHive 提供（hive://）。
    extra_params 支持: http_path(仅 HS2 HTTP 模式)、auth(KERBEROS/LDAP 等)。
    """

    driver_module = "pyhive"

    def build_url(self) -> str:
        _require("pyhive.hive", "PyHive（pip install 'PyHive[hive]'）")
        from app.connectors.base import _quote
        auth = ""
        if self.ds.username:
            auth = f"{_quote(self.ds.username)}:{_quote(self._password)}@"
        extra = self.ds.extra_params or {}
        qs = ""
        if extra.get("http_path"):
            qs += f"&http_path={_quote(str(extra['http_path']))}"
        if extra.get("auth"):
            qs += f"&auth={_quote(str(extra['auth']))}"
        port = self.ds.port or 10000
        return (f"hive://{auth}{self.ds.host}:{port}/{_quote(self.ds.database_name or 'default')}"
                + (f"?{qs[1:]}" if qs else ""))

    def _default_schema(self, insp) -> str:
        return self.ds.database_name or "default"

    def _timeout_statement(self) -> str | None:
        return f"SET hive.query.timeout.seconds={settings_query_timeout_ms() // 1000}"

    def _estimate_rows(self, name: str, schema: str) -> int | None:
        return None   # Hive/Spark 大表不做 COUNT(*)


class HiveConnector(_ThriftConnector):
    db_type = DbType.HIVE

    def build_url(self) -> str:
        _require("pyhive.hive", "PyHive（pip install 'PyHive[hive]'）")
        return super().build_url()


class SparkConnector(_ThriftConnector):
    db_type = DbType.SPARK

    def build_url(self) -> str:
        _require("pyhive.hive", "PyHive（pip install 'PyHive[hive]'），Spark ThriftServer 兼容 HiveServer2 协议")
        return super().build_url()


def settings_query_timeout_ms() -> int:
    from app.core.config import settings as _s
    return _s.query_timeout_sec * 1000


_REGISTRY: dict[DbType, type[Connector]] = {
    DbType.MYSQL: MySQLConnector,
    DbType.POSTGRESQL: PostgresConnector,
    DbType.CLICKHOUSE: ClickHouseConnector,
    DbType.SQLITE: SqliteConnector,
    DbType.HIVE: HiveConnector,
    DbType.SPARK: SparkConnector,
    DbType.DORIS: DorisConnector,
    DbType.STARROCKS: StarRocksConnector,
}


def get_connector(ds) -> Connector:
    cls = _REGISTRY.get(ds.db_type)
    if cls is None:
        raise DatasourceError(f"不支持的数据源类型: {ds.db_type}")
    return cls(ds)
