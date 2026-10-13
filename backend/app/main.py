"""FastAPI 应用入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import AppError

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
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
    from app.jobs.scheduler import start_scheduler, stop_scheduler
    start_scheduler()
    yield
    stop_scheduler()


def create_app() -> FastAPI:
    app = FastAPI(title="仓颉 Cangjie · AI 数仓工作台", version="1.0.0", lifespan=lifespan)
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return JSONResponse(status_code=exc.http_status,
                            content={"code": exc.code, "message": exc.message})

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        # 路径/查询参数的 uuid、枚举转换失败属客户端输入错误 → 400 而非 500
        return JSONResponse(status_code=400,
                            content={"code": "BAD_REQUEST",
                                     "message": f"参数格式错误：{exc}"[:200]})

    app.include_router(api_router)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
