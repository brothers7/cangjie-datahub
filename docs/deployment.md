# 生产部署清单

本文档面向生产/准生产环境的部署与升级。本地体验请直接 `make up`（见 README）。

## 1. 部署前必改项

复制 `.env.example` 为 `.env` 后，**生产环境必须修改以下项**（不改 = 重大安全风险）：

| 配置项 | 要求 |
| --- | --- |
| `JWT_SECRET` | 随机 >= 32 字符强密钥，例如 `openssl rand -hex 32` |
| `SECRET_ENCRYPTION_KEY` | base64 编码的 32 字节主密钥：`python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"` |
| `POSTGRES_PASSWORD` | 数据库口令；注意 `APP_DB_URL` 中的口令要同步修改 |
| `APP_ENV` | 设为 `production`：弱密钥（默认 `JWT_SECRET`、空 `SECRET_ENCRYPTION_KEY`）将**拒绝启动** |
| `DEFAULT_ADMIN_PASSWORD` | 首次登录后立即在"用户管理"界面修改管理员密码 |

建议同时检查：`LOGIN_MAX_FAILS` / `LOGIN_LOCK_MINUTES`（登录限流）、`ALERT_WEBHOOK_URL`（失败告警）。

## 2. 启动

```bash
git clone https://github.com/brothers7/cangjie-datahub.git
cd cangjie-datahub
cp .env.example .env   # 按上表修改
docker compose up -d --build
```

- 前端：http://localhost:8080
- 后端 API 文档：http://localhost:9000/docs
- 健康检查：`curl http://localhost:9000/api/readyz`（compose 已配置为 backend 的 healthcheck，frontend 等 backend healthy 后才启动）

镜像以非 root 用户运行（后端 `appuser`，前端 `nginxinc/nginx-unprivileged` 官方非 root 镜像）。

## 3. 反向代理与 TLS

生产环境建议在 frontend（nginx）之前再挂一层反向代理（Caddy / Nginx / 云 LB）终止 TLS：

- 只暴露 443，将 `FRONTEND_PORT` 绑定到内网或 `127.0.0.1:8080`。
- 内置 nginx 已透传 `X-Forwarded-Proto`，外层代理设置 `X-Forwarded-Proto https` 即可。
- SSE（问答流式输出）要求代理**关闭响应缓冲**；前端容器内 nginx 已调优（`proxy_buffering off`），外层代理若再套一层需同样关闭缓冲（如 nginx 的 `proxy_buffering off;` 或加 `X-Accel-Buffering: no`）。
- 上传/请求体上限为 10m（`client_max_body_size`），外层代理不要比这更小。

## 4. 数据卷与备份

compose 定义了三个命名卷，**全部需要纳入备份**：

| 卷 | 内容 | 备份建议 |
| --- | --- | --- |
| `pg_data` | 应用库（用户、配置、任务、审计……全部业务数据） | `docker compose exec postgres pg_dump -U data_agent data_agent > backup-$(date +%F).sql`，每日 + 异地 |
| `qdrant_data` | 向量索引（语义记忆/指标召回） | 可直接打包卷目录；丢失也可由元数据重扫重建，优先级低于 pg_data |
| `demo_data` | 演示 SQLite 库 | 一般无需备份，`make reseed-demo` 可重建 |

备份命名卷目录的通用方式（以 pg_data 为例）：

```bash
docker run --rm -v cangjie-datahub_pg_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/pg_data-$(date +%F).tar.gz -C /data .
```

（卷名以 `docker volume ls` 实际前缀为准。）

## 5. 升级流程

```bash
git pull
docker compose build
docker compose up -d
```

数据库迁移（本项目已引入 Alembic，迁移脚本在 `backend/alembic/`）：

- **老部署（库已由旧版 create_all/bootstrap 建过表）**：首次接入 Alembic 先标记基线，之后只应用增量：

  ```bash
  docker compose exec backend alembic -c alembic/alembic.ini stamp head   # 仅首次
  docker compose exec backend alembic -c alembic/alembic.ini upgrade head
  ```

- **新部署**：`upgrade head` 即可。

过渡期 bootstrap 仍保留 `create_all` + 轻量补列（`_migrate_columns`）兜底，幂等兼容；正式迁移请走 Alembic。

升级顺序建议：先备份 `pg_data` → 构建新镜像 → `up -d` → 执行迁移 → 验证 `/api/readyz`。

## 6. 回滚

1. 代码回滚：`git checkout <上一个可用 tag/commit>`，然后 `docker compose up -d --build`。
2. 数据库回滚：Alembic 支持 `downgrade`（如 `alembic -c alembic/alembic.ini downgrade -1`），但**降级不一定可逆**；生产回滚以"恢复升级前的 `pg_dump` 备份"为准：

   ```bash
   docker compose down
   docker volume rm cangjie-datahub_pg_data   # 确认已另有备份！
   docker compose up -d postgres
   cat backup-2026-10-20.sql | docker compose exec -T postgres psql -U data_agent data_agent
   docker compose up -d
   ```

3. 验证：`curl http://localhost:9000/api/readyz` 返回 `db: ok`，再登录界面抽查关键功能。
