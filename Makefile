.PHONY: up down logs ps build rebuild backend-shell test seed-docs

# 首次运行：没有 .env 则先从示例复制（含默认值，生产环境务必改密钥）
.env:
	@test -f .env || (cp .env.example .env && echo "已从 .env.example 复制 .env，请按需修改密钥")
	@touch .env

# 一键启动（首次会自动构建镜像、初始化数据库与演示库）
up: .env
	docker compose up -d --build
	@echo ""
	@echo "前端:   http://localhost:$${FRONTEND_PORT:-8080}  (默认账号 admin，密码见 .env 的 DEFAULT_ADMIN_PASSWORD，请登录后尽快修改)"
	@echo "后端:   http://localhost:$${BACKEND_PORT:-9000}/docs"

down:
	docker compose down

logs:
	docker compose logs -f backend

ps:
	docker compose ps

# 修改代码后重建并重启
rebuild:
	docker compose up -d --build --force-recreate backend frontend

backend-shell:
	docker compose exec backend bash

# 本地跑后端单测（不依赖 Docker）
test:
	cd backend && uv run pytest -q

# 重建演示库（清空后重新生成并重扫图谱）
reseed-demo:
	docker compose exec backend python -m app.bootstrap --reseed-demo
