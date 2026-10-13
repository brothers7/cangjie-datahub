"""pytest 公共夹具。

测试全部跑在独立的临时 SQLite 上（tmp_path），不碰 local/data_agent.db，
避免污染本地开发数据。配置通过环境变量注入，必须在 import app.* 之前设置。
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 关键：必须在任何 app.* 被 import 之前设好环境变量。
# conftest 是 pytest 加载的第一个模块，此时 app.core.config 尚未求值；
# 若延后到 fixture 里设置，settings 已经指向 local/data_agent.db，
# 测试会直接写进本地开发库（并且被库里已有的数据撞唯一约束）。
_TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="data-agent-tests-"))
os.environ.setdefault("APP_DB_URL", f"sqlite+aiosqlite:///{_TEST_DB_DIR / 'test.db'}")
os.environ.setdefault("DEMO_DB_ENABLED", "false")
os.environ.setdefault("DEMO_DB_PATH", str(_TEST_DB_DIR / "demo.db"))
os.environ.setdefault("BOOTSTRAP_CREATE_ALL", "true")
os.environ.setdefault("QDRANT_URL", "http://127.0.0.1:1")  # 测试不连向量库
os.environ.setdefault("TAVILY_API_KEY", "")


def _assert_isolated_db() -> None:
    from app.core.config import settings

    if "data-agent-tests-" not in settings.app_db_url:
        raise RuntimeError(
            f"测试库未隔离，当前指向 {settings.app_db_url!r}。"
            "检查是否有模块在 conftest 之前 import 了 app.core.config。"
        )


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def _env():
    _assert_isolated_db()
    yield


@pytest.fixture(scope="session")
async def engine(_env):
    # 环境变量就绪后才能导入应用模块（settings 在 import 时求值）
    from app.db.models import Base
    from app.db.session import dispose_engine, get_engine
    from app.db.session import get_engine as _ge

    _assert_isolated_db()
    eng = _ge()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()
    dispose_engine()


@pytest.fixture
async def session(engine):
    """每个用例跑在一个外层事务里，用例内的 commit 只是释放 savepoint。

    这样用例自己调 session.commit() 也不会把数据留在库里，
    用例之间互不影响（否则前一个用例建的同名指标会让后一个用例撞唯一约束）。
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    conn = await engine.connect()
    trans = await conn.begin()
    s = AsyncSession(bind=conn, join_transaction_mode="create_savepoint",
                     expire_on_commit=False, autoflush=False)
    try:
        yield s
    finally:
        await s.close()
        await trans.rollback()
        await conn.close()


@pytest.fixture
async def committed(engine):
    """真实提交的会话（API 级测试用）：HTTP 请求走独立连接，只能看到已提交数据。
    用例结束后按行删除清理，不依赖回滚。"""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(bind=engine, expire_on_commit=False) as s:
        created: list = []
        s._test_created = created  # type: ignore[attr-defined]
        try:
            yield s
            await s.commit()
        finally:
            # 逆序删除测试创建的行（先子后父，规避外键）。
            # 外键行（DatasourcePermission）在 _mk_ds 之后 add，逆序删会先删它再删 DataSource。
            for row in reversed(created):
                try:
                    await s.delete(row)
                except Exception:
                    pass
            try:
                await s.commit()
            except Exception:
                await s.rollback()


@pytest.fixture
def uid() -> callable:
    return uuid.uuid4
