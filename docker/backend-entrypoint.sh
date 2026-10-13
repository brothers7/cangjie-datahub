#!/bin/sh
set -e

echo "[entrypoint] waiting for postgres..."
python - <<'EOF'
import os, time, sys
import asyncpg
url = os.environ.get("APP_DB_URL", "")
# postgresql+asyncpg://user:pw@host:5432/db -> postgresql://user:pw@host:5432/db
pg_url = url.replace("+asyncpg", "")
if pg_url.startswith("postgresql"):
    for i in range(60):
        try:
            asyncpg_connect = None
            import asyncio
            async def wait():
                conn = await asyncpg.connect(pg_url.replace("postgresql://", "postgres://"))
                await conn.close()
            asyncio.run(wait())
            print("[entrypoint] postgres is ready")
            return
        except Exception as e:
            if i == 59:
                print(f"[entrypoint] postgres not reachable: {e}")
                sys.exit(1)
            time.sleep(2)
EOF

echo "[entrypoint] bootstrap (create tables / admin / demo db / first scan)..."
# --reseed-demo 可选；正常首启幂等
python -m app.bootstrap "$@"

echo "[entrypoint] starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 9000 --workers 1
