# 架构说明

从部署视图到代码分层，帮助贡献者快速定位。模块功能详见 README 的模块详解。

## 1. 部署视图

```
                        浏览器
                          │ 443/80
                          ▼
┌────────────────────────────────────────────────────────────┐
│ frontend 容器（nginxinc/nginx-unprivileged，非 root，:8080）│
│  - 静态资源（React 构建产物）+ SPA fallback                 │
│  - /api/ 反代 → backend:9000（SSE 关缓冲、10m 请求体上限）  │
└──────────────────────────────┬─────────────────────────────┘
                               │ depends_on: service_healthy
                               ▼
┌────────────────────────────────────────────────────────────┐
│ backend 容器（FastAPI/uvicorn，非 root appuser，:9000）      │
│  entrypoint：等 PG → bootstrap（建表/管理员/演示库）→ uvicorn│
│  healthcheck：GET /api/readyz                              │
└───────┬──────────────────────┬─────────────────────────────┘
        ▼                      ▼
  postgres:16-alpine     qdrant/qdrant:v1.19.2
  （应用库，pg_data 卷）  （向量索引，qdrant_data 卷；可选，主链路可降级）

  另：demo_data 卷 —— 内置演示 SQLite 库
```

## 2. 后端分层（`backend/app/`）

```
┌────────────────────────── FastAPI app（main.py）──────────────────────────┐
│  全局异常处理 · CORS · 启动钩子（生产配置校验 / 建表兜底 / 悬挂任务回收）   │
│  /api/healthz（liveness） · /api/readyz（readiness：DB + Qdrant 降级标注）│
└───────────────────────────────────┬────────────────────────────────────────┘
                                    ▼
┌─ api/ ────────────────────────────────────────────────────────────────────┐
│  v1/ 路由（问答、图谱、建模、调度、治理、数据源、LLM、Skill、用户、日志…）  │
│  deps.py：JWT 认证、RBAC 鉴权、审计                                       │
└───────────────────────────────────┬────────────────────────────────────────┘
        ┌───────────────┬───────────┴──────────┬──────────────┬─────────────┐
        ▼               ▼                      ▼              ▼             ▼
┌─ services/ ──┐ ┌─ agent/ ─────────┐ ┌─ kg/ ──────────┐ ┌─ jobs/ ──┐ ┌─ llm/ ────┐
│ 业务服务层：  │ │ 编码代理（建模）：│ │ 知识图谱：      │ │ 调度器   │ │ LLM 网关： │
│ governance   │ │ coding_agent     │ │ builder/merger/ │ │ 后台 tick│ │ 场景路由/  │
│ dw_standards │ │ （工具白名单、   │ │ classifier/     │ │ 循环驱动 │ │ 重试降级/  │
│ custom_skills│ │  步数上限）      │ │ validator       │ │          │ │ 失败告警   │
│ scheduler_   │ │ router_agent     │ │                 │ │          │ │ webhook    │
│ engine（调度  │ │ （意图路由）     │ │                 │ │          │ │            │
│ 引擎，见 §3）│ │                  │ │                 │ │          │ │            │
│ airflow/     │ │                  │ │                 │ │          │ │            │
│ azkaban 对接 │ │                  │ │                 │ │          │ │            │
│ cache_service│ │                  │ │                 │ │          │ │            │
└──────┬───────┘ └────────┬─────────┘ └───────┬─────────┘ └────┬─────┘ └─────┬──────┘
       └──────────────────┴─────────┬─────────┴────────────────┴─────────────┘
                                    ▼
┌─ db/（SQLAlchemy 异步 session + ORM 模型） ─────────────────────────────────┐
│  Alembic 迁移：backend/alembic/（老部署 stamp head，之后 upgrade head）     │
├─ connectors/（8 种数仓连接器 registry：MySQL/PG/CH/Hive/Spark/Doris/SR/SQLite）┤
├─ metadata/（元数据扫描 scanner、Schema 指纹 hash）                          │
├─ core/（config 环境变量、crypto AES-256-GCM、security 认证）                │
└─ schemas/（Pydantic DTO） · demo/（演示数据） · bootstrap.py（首启初始化）  │
```

数据流要点：

- **问答链路**：api → router_agent（意图）→ skill 管线（改写/召回/SQL生成/**sqlglot AST 守卫**/执行/图表），全程 SSE 推送。
- **召回**：metadata（库表元数据）+ kg（语义图谱）+ Qdrant（向量）；Qdrant 不可达自动降级。
- **写路径双重闸门**：调度执行的 ETL SQL 需 ① 管理员审核任务 且 ② 数据源开启"允许内置调度写入"；DDL 绝不自动执行。

## 3. 调度引擎时序（内置 scheduler）

`jobs/scheduler.py` 的后台 tick 循环驱动 `services/scheduler_engine.py`：

```
cron 到期
   │
   ▼
生成 run instance（waiting，业务日期 ${ds}=T-1）
   │
   ▼
依赖闸门：上游任务同周期实例是否成功？
   │        ├─ 否 → 保持 waiting（超时标记 blocked）
   ▼ 是
写双闸门：任务已审核 && 数据源允许调度写入？
   │        ├─ 否 → blocked（零写动作）
   ▼ 是
running → 经 connectors 在目标数仓执行 ETL SQL（会话只读策略按数据源开关）
   │
   ▼
success / failed（支持手动触发与重跑）
```

补偿机制：进程停机期间错过的周期，重启后由 catchup 按 `SCHEDULER_CATCHUP_HOURS`
（默认 24h，硬上限 30 天）回溯补跑；单任务单次回补实例数另有硬上限双保险。
悬挂任务回收：启动时把 `RUNNING` 超过 `STALE_SCAN_JOB_MINUTES`（默认 30min）的
元数据扫描任务标记 FAILED（详见 [operations.md](operations.md)）。

## 4. 前端结构（`frontend/src/`）

```
main.tsx → App.tsx（路由 + 鉴权守卫）
  │
  ├─ layouts/      整体布局（侧边栏、语言切换）
  ├─ pages/        一页一模块：Chat / Modeling / KnowledgeGraph / Integrations(调度)
  │                Governance / DataSources / LlmConfig / Reports / Logs / Users / Login
  ├─ components/   复用组件（图表、DAG 画布、图谱画布、SSE 步骤流…）
  ├─ api/          后端接口封装（REST + SSE fetch 流）
  ├─ stores/       zustand 全局状态（会话、用户、语言）
  ├─ hooks/        复用逻辑
  ├─ i18n/         中/英文案
  └─ types/        与后端 schemas 对齐的 TS 类型
```

- 构建：Vite + `tsc -b`；测试：vitest（`npm test`）；lint：eslint（`npm run lint`）。
- 开发时 `VITE_DEV_API` 指向本地后端；生产由 nginx 反代 `/api/`。

## 5. CI 与镜像

- CI（`.github/workflows/ci.yml`）：后端 pytest（含覆盖率）· ruff（宽松不阻塞）· 前端 tsc/vitest/eslint/build · 前后端镜像构建（不推送）。
- 镜像：多阶段构建、非 root 运行；后端依赖按 `backend/uv.lock` 严格安装（`uv sync --locked`），依赖变更后需 `cd backend && uv lock` 重新生成锁文件。
- 运维细节（部署/配置/故障）见 [deployment.md](deployment.md)、[configuration.md](configuration.md)、[operations.md](operations.md)。
