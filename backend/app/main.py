"""FastAPI 应用入口。"""
from __future__ import annotations

import contextvars
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings, validate_production_config
from app.core.errors import AppError

# 当前请求 ID（contextvar 保证并发请求互不串扰；无请求上下文时为 "-"）
_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """把当前请求的 X-Request-ID 注入 LogRecord，配合格式串 %(request_id)s 输出。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_ctx.get()
        return True


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s")
# Filter 挂到 root logger 的 handler 上：所有 propagate 上来的记录格式化前都会注入 request_id
_req_id_filter = RequestIdFilter()
for _h in logging.getLogger().handlers:
    _h.addFilter(_req_id_filter)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 本地开发（SQLite 应用库）自动建表；Docker 里由 bootstrap 先行完成
    if settings.bootstrap_create_all and settings.is_sqlite_app_db:
        from app.db.models import Base
        from app.db.session import get_engine
        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        from app.bootstrap import bootstrap
        await bootstrap()
    # 回收上次进程遗留的悬挂扫描任务（RUNNING 超时 → FAILED）。
    # Docker 部署下 bootstrap 在 entrypoint 进程执行，这里需独立兜底。
    try:
        from app.bootstrap import reap_stale_scan_jobs
        from app.db.session import get_session_factory
        async with get_session_factory()() as s:
            await reap_stale_scan_jobs(s)
            await s.commit()
    except Exception:
        logger.exception("reap stale scan jobs failed")
    from app.jobs.scheduler import start_scheduler, stop_scheduler
    start_scheduler()
    yield
    stop_scheduler()
    # 关闭应用库引擎连接池（await dispose，不残留）
    from app.bootstrap import dispose_app_engine
    await dispose_app_engine()


def create_app() -> FastAPI:
    # 生产模式配置强制校验：不合规直接抛错拒绝启动（开发模式仅 warning）
    validate_production_config()
    app = FastAPI(title="仓颉 Cangjie · AI 数仓工作台", version="1.0.0", lifespan=lifespan)
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """请求追踪：透传入站 X-Request-ID，缺省生成；注入日志上下文并回写响应头。"""
        rid = request.headers.get("X-Request-ID", "").strip() or uuid.uuid4().hex
        token = _request_id_ctx.set(rid)
        try:
            response = await call_next(request)
        finally:
            _request_id_ctx.reset(token)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        logger.exception("AppError %s: %s", exc.code, exc.message)
        return JSONResponse(status_code=exc.http_status,
                            content={"code": exc.code, "message": exc.message})

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        # 路径/查询参数的 uuid、枚举转换失败属客户端输入错误 → 400 而非 500
        return JSONResponse(status_code=400,
                            content={"code": "BAD_REQUEST",
                                     "message": f"参数格式错误：{exc}"[:200]})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        # 兜底：未捕获异常统一 500，带堆栈日志便于排查（不向客户端泄露内部细节）
        logger.exception("unhandled error: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500,
                            content={"code": "INTERNAL_ERROR", "message": "服务器内部错误"})

    app.include_router(api_router)

    @app.get("/api/health")
    @app.get("/api/healthz")
    async def health():
        """liveness：进程存活即 ok，不探测依赖（供容器 livenessProbe）。"""
        return {"status": "ok"}

    @app.get("/api/readyz")
    async def readyz():
        """readiness：探测关键依赖。

        - DB（必选）：SELECT 1 失败 → 整体 503
        - Qdrant（可选增强组件，仅承载语义记忆/指标向量索引，主链路有降级路径）：
          不可达只标注 degraded，不影响整体状态码
        """
        import sqlalchemy as sa

        checks: dict[str, str] = {}

        from app.db.session import get_engine
        try:
            async with get_engine().connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
            checks["db"] = "ok"
        except Exception as e:
            checks["db"] = f"error: {type(e).__name__}: {e}"[:200]

        try:
            import httpx
            headers = {}
            if settings.qdrant_api_key:
                headers["api-key"] = settings.qdrant_api_key
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{settings.qdrant_url.rstrip('/')}/collections",
                                        headers=headers)
            checks["qdrant"] = "ok" if resp.status_code < 500 else f"error: http {resp.status_code}"
        except Exception as e:
            checks["qdrant"] = f"degraded: {type(e).__name__}"[:200]

        db_ok = checks["db"] == "ok"
        if not db_ok:
            status = "error"
        elif checks["qdrant"] != "ok":
            status = "degraded"
        else:
            status = "ok"
        return JSONResponse(status_code=200 if db_ok else 503,
                            content={"status": status, "checks": checks})

    return app


app = create_app()
