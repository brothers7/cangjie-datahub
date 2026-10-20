# syntax=docker/dockerfile:1

# ================= builder 阶段：编译工具链 + 依赖装入独立 venv =================
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # 依赖安装到独立 venv，runtime 阶段整体复制
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1

# 编译工具链：clickhouse-connect / cffi 等需要
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv

WORKDIR /build

# 先只复制锁文件，源码变更不破坏依赖缓存
COPY backend/pyproject.toml backend/uv.lock ./
# 严格按 uv.lock 安装（--locked）：基础依赖 + 全部数仓驱动
# （mysql/postgres/clickhouse/hive/spark/doris/starrocks，对应 extra: all-db）
# pyproject.toml 依赖变更后需先执行 `cd backend && uv lock` 更新锁文件
RUN uv sync --locked --no-dev --no-install-project --extra all-db

# ================= runtime 阶段：只保留 venv 与运行所需文件 =================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # 容器内默认时区（compose 会再传 TZ=${APP_TZ:-Asia/Shanghai}）
    APP_TZ=Asia/Shanghai \
    # uvicorn worker 数，entrypoint 读取
    UVICORN_WORKERS=1

# 运行库：libffi8（clickhouse-connect/cffi）、curl（compose healthcheck）、tzdata（时区）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libffi8 curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY backend/app ./app
# Alembic 迁移脚本（alembic.ini 在 alembic/ 内，执行：
#   docker compose exec backend alembic -c alembic/alembic.ini upgrade head）
COPY backend/alembic ./alembic
COPY docker/backend-entrypoint.sh /entrypoint.sh

# demo_data 是 compose 命名卷的挂载点：命名卷首次创建时继承镜像内目录属主，
# 这里先 chown 给非 root 用户，保证容器内可写
RUN chmod +x /entrypoint.sh \
    && mkdir -p /app/demo_data \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 9000

ENTRYPOINT ["/entrypoint.sh"]
