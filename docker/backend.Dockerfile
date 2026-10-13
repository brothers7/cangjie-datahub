FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 系统依赖：clickhouse-connect 需要 libssl/cffi 运行库
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml ./
# 安装基础依赖 + 全部数仓驱动（mysql/postgres/clickhouse/hive/spark/doris/starrocks）
RUN pip install uv && uv pip install --system -r pyproject.toml && \
    uv pip install --system "pymysql>=1.1" "psycopg[binary]>=3.1" \
        "clickhouse-connect>=0.7" "clickhouse-sqlalchemy>=0.3" "PyHive[hive]>=0.7"

COPY backend/app ./app
COPY docker/backend-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /app/demo_data

EXPOSE 9000

ENTRYPOINT ["/entrypoint.sh"]
