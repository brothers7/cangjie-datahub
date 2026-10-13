"""v1 总路由。"""
from fastapi import APIRouter

from app.api.v1 import scheduler as scheduler_api
from app.api.v1 import (airflow, auth, azkaban, chat, datasources, etl_tasks, governance,
                        kg, llm,
                        logs,
                        modeling,
                        modeling_sessions,
                        reports, scans, skills, standards, users)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(datasources.router)
api_router.include_router(scans.router)
api_router.include_router(kg.router)
api_router.include_router(llm.router)
api_router.include_router(chat.router)
api_router.include_router(reports.router)
api_router.include_router(logs.router)
api_router.include_router(azkaban.router)
api_router.include_router(airflow.router)
api_router.include_router(scheduler_api.router)
api_router.include_router(skills.router)
api_router.include_router(standards.router)
api_router.include_router(governance.router)
api_router.include_router(etl_tasks.router)
api_router.include_router(modeling.router)
api_router.include_router(modeling_sessions.router)
