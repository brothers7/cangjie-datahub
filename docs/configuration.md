# 配置项参考（环境变量）

所有配置均通过环境变量 / 根目录 `.env` 注入（后端为 pydantic-settings，字段不区分大小写）。
模板见 [.env.example](../.env.example)，本文是完整清单。标注 ★ 的为**生产环境必须修改**项。

## 运行环境

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_ENV` ★ | `dev` | `dev` / `production`。production 下弱密钥（默认 `JWT_SECRET`、空 `SECRET_ENCRYPTION_KEY`）拒绝启动 |
| `APP_TZ` | `Asia/Shanghai` | 应用与调度引擎使用的时区（IANA 名称）；compose 同时作为后端容器 `TZ` 传入 |
| `UVICORN_WORKERS` | `1` | uvicorn worker 进程数（entrypoint 读取）。内置调度器为进程内单实例，调大前请确认部署形态 |

## 数据库

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_USER` | `data_agent` | PostgreSQL 用户名（compose postgres 服务） |
| `POSTGRES_PASSWORD` ★ | `data_agent_pg_pw` | PostgreSQL 口令；改了要同步 `APP_DB_URL` |
| `POSTGRES_DB` | `data_agent` | PostgreSQL 库名 |
| `APP_DB_URL` | `postgresql+asyncpg://...@postgres:5432/data_agent` | 应用库连接串；本地开发可用 `sqlite+aiosqlite:///./local/data_agent.db` |

## 安全

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `JWT_SECRET` ★ | `please-change-me-...`（开发占位） | JWT 签名密钥，>= 32 随机字符：`openssl rand -hex 32` |
| `SECRET_ENCRYPTION_KEY` ★ | 空 | AES-256-GCM 主密钥（base64 编码的 32 字节）。留空时由 `JWT_SECRET` 派生，仅限开发 |
| `DEFAULT_ADMIN_USERNAME` | `admin` | 首次启动创建的默认管理员用户名 |
| `DEFAULT_ADMIN_PASSWORD` ★ | `admin12345` | 默认管理员密码，**首次登录后立即在界面修改** |
| `LOGIN_MAX_FAILS` | `5` | 连续密码错误达到该次数后锁定账号 |
| `LOGIN_LOCK_MINUTES` | `10` | 账号锁定时长（分钟） |
| `ALERT_WEBHOOK_URL` | 空 | LLM 彻底失败时的告警 webhook（POST JSON，报文格式见 [operations.md](operations.md)）；空 = 关闭 |

## 依赖服务

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant 向量库地址；不可达时主链路自动降级（`/api/readyz` 仅标注 degraded） |
| `QDRANT_API_KEY` | 空 | Qdrant API Key（自建 Qdrant 一般留空） |
| `TAVILY_API_KEY` | 空 | Tavily 联网检索 Key；未配置则联网检索能力自动关闭 |

## 演示库

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEMO_DB_PATH` | `/app/demo_data/ecom_demo.db` | 演示 SQLite 库路径（compose 挂载 `demo_data` 卷） |
| `DEMO_DB_ENABLED` | `true` | 是否在首次启动时生成并接入演示库 |

## 查询性能与安全

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QUERY_CACHE_TTL_SEC` | `300` | 问答结果缓存 TTL（秒），按"问题 + Schema 指纹"命中 |
| `SQL_MAX_ROWS` | `10000` | 单条查询最大返回行数（强制 LIMIT 上限） |
| `QUERY_TIMEOUT_SEC` | `30` | SQL 执行超时（秒） |
| `SCAN_BATCH_SIZE` | `20` | 元数据扫描每批处理的表数量 |

## Agent / Skill 限额

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT_MAX_STEPS` | `8` | 建模编码代理单轮会话最大步数（推理→工具调用循环上限） |
| `SKILL_PYTHON_TIMEOUT_SEC` | `30` | Python 型 Skill 沙箱执行超时（秒） |

## 后台任务维护

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STALE_SCAN_JOB_MINUTES` | `30` | RUNNING 超过该时长的元数据扫描任务视为进程崩溃遗留，启动时回收为 FAILED |
| `SCHEDULER_CATCHUP_HOURS` | `24` | 内置调度器补跑（catchup）的回溯窗口（小时） |

## 端口（compose 宿主机映射）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BACKEND_PORT` | `9000` | 宿主机 → 后端容器 9000 |
| `FRONTEND_PORT` | `8080` | 宿主机 → 前端容器 8080（非 root nginx 镜像监听容器内 8080） |

## compose 内部固定值（不在 .env）

- backend 服务环境：`APP_DB_URL`（由上面 POSTGRES_* 拼装）、`QDRANT_URL=http://qdrant:6333`、`DEMO_DB_PATH=/app/demo_data/ecom_demo.db`、`TZ=${APP_TZ}`。
- Qdrant 镜像固定为 `qdrant/qdrant:v1.19.2`（升级在 compose 中改 tag 并回归验证）。
