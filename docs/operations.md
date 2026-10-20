# 运维手册

日常运维操作、健康检查、常见故障处理与告警配置。

## 1. 日志查看

```bash
make logs                                # 跟踪后端日志（等价于 docker compose logs -f backend）
docker compose logs -f frontend          # 前端 nginx 访问/错误日志
docker compose logs -f postgres qdrant   # 依赖组件日志
docker compose logs --since 1h backend   # 最近 1 小时
docker compose ps                        # 各容器状态（含 health 状态）
```

后端启动日志关键行（entrypoint 输出）：

- `[entrypoint] postgres is ready` —— 数据库连通
- `[entrypoint] bootstrap ...` —— 建表 / 默认管理员 / 演示库 / 首次扫描（幂等）
- `[entrypoint] starting uvicorn (workers=N)` —— 服务拉起

## 2. 健康检查

| 端点 | 用途 | 语义 |
| --- | --- | --- |
| `GET /api/healthz` | liveness | 进程存活即返回 `{"status": "ok"}`，不探测依赖 |
| `GET /api/readyz` | readiness | 探测依赖：`db` 失败 → 整体 503；`qdrant` 不可达仅标注 `degraded`，**不影响状态码**（主链路有降级路径） |

```bash
curl -s http://localhost:9000/api/readyz | jq
# 正常：{"status":"ok","checks":{"db":"ok","qdrant":"ok"}}
# Qdrant 挂了：{"status":"ok","checks":{"db":"ok","qdrant":"degraded: ConnectError"}}
```

compose 已把 `/api/readyz` 配置为 backend 的 healthcheck（interval 10s、start_period 90s），
frontend `depends_on: service_healthy`，即后端就绪后前端才启动。

## 3. 常见故障

### 3.1 Qdrant 不可达

现象：问答仍可出数，但向量召回（语义记忆/指标检索）降级；`/api/readyz` 显示 `qdrant: degraded: ...`。

排查：

```bash
docker compose ps qdrant
docker compose logs --tail 100 qdrant
curl -s http://localhost:6333/collections   # 宿主机未映射端口时进容器网络验证：
docker compose exec backend curl -s http://qdrant:6333/collections
```

处理：`docker compose restart qdrant`。向量索引丢失可由元数据重扫重建（数据源管理 → 重新扫描），不阻塞主链路。

### 3.2 数据库迁移失败

现象：升级后 backend 反复重启，日志出现建表/补列报错。

- 当前 bootstrap 保留 `create_all` + 轻量补列兜底（幂等），多数情况下重启即可自愈。
- 已接入 Alembic 的库，手动核对版本：

  ```bash
  docker compose exec backend alembic -c alembic/alembic.ini current
  docker compose exec backend alembic -c alembic/alembic.ini upgrade head
  ```

- 老库首次接入 Alembic 必须先 `stamp head`（见 [deployment.md](deployment.md) 升级流程），否则 `upgrade head` 会重复建表报错。
- 补列失败会在日志中打出"column migrate 失败列清单"，按清单人工执行 DDL 或改用 Alembic 迁移。

### 3.3 扫描任务 stuck（一直 RUNNING）

机制：进程崩溃/重启会遗留永远 RUNNING 的元数据扫描任务。应用启动时自动执行 **reap**：
把 `RUNNING` 且 `started_at` 超过 `STALE_SCAN_JOB_MINUTES`（默认 30 分钟）的 ScanJob 标记为
`FAILED`，并追加错误日志"进程重启回收"。日志可见 `reaped N stale scan jobs`。

处理：一般重启 backend 即自动回收；之后到数据源管理重新触发扫描即可。阈值用
`STALE_SCAN_JOB_MINUTES` 调整。

### 3.4 调度补偿（catchup）窗口

内置调度器停机期间的到期任务不会丢：重启后 catchup 逻辑按 `SCHEDULER_CATCHUP_HOURS`
（默认 24 小时，硬上限 30 天）回溯补跑窗口内缺失的运行实例；单任务单次回补实例数另有
硬上限做双保险。停机超过窗口的历史周期不会补跑——如需补，请用手动触发指定业务日期。

### 3.5 生产模式拒绝启动

`APP_ENV=production` 且 `JWT_SECRET` 为默认值或 `SECRET_ENCRYPTION_KEY` 为空时，启动直接
报错并逐项列出不合规项（开发模式只打 warning）。按 [configuration.md](configuration.md)
安全一节补齐即可。

## 4. 告警 Webhook

配置 `.env` 的 `ALERT_WEBHOOK_URL` 后，**LLM 调用彻底失败**（重试 + 降级链之后仍失败）时，
后端向该 URL 发起 `POST`（`Content-Type: application/json`，超时 5s）：

```json
{
  "type": "llm_failure",
  "config_name": "生产 GPT 配置",
  "config_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "scene": "chat",
  "model": "gpt-4o",
  "error": "...(截断至 500 字符)",
  "time": "2026-10-20T12:00:00+00:00"
}
```

行为约定：

- 留空 = 关闭告警；webhook 发送失败只记日志，绝不影响主调用链。
- 节流：同一 LLM 配置 5 分钟内最多告警 1 次。
- 可接企业微信/飞书/钉钉自定义机器人或自建中转（如需特定机器人报文格式，用一层轻量中转转换）。

## 5. 备份与升级

见 [deployment.md](deployment.md)：三个命名卷（`pg_data` / `qdrant_data` / `demo_data`）的备份方式、
升级流程（git pull → compose build → alembic 迁移）与回滚步骤。
