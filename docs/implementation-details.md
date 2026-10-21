# 仓颉 DataHub 核心能力实现详解

> 对应产品规格 `prd.md`，本文按五条能力主线逐条拆解「具体是怎么实现的」，
> 所有引用均为仓库内真实文件路径，可对照源码阅读。

---

## 一、统一 LLM 网关

> 适配 OpenAI/Anthropic 兼容协议，按场景绑定模型，支持会话级选模、超时重试、故障降级，以及 Token、时延和错误率观测。

### 1. 协议适配层

- 位置：`backend/app/llm/adapters/`（`openai_adapter.py`、`anthropic_adapter.py`）
- 两个适配器实现统一接口：入参为内部 `ChatMessage` 列表（`llm/types.py`），输出统一的 `LLMResponse`（文本、token 用量、时延、模型名）。
- **任何 OpenAI 兼容端点都能接**（DeepSeek、通义、vLLM、OneAPI 等），Anthropic 走其 Messages API 协议。适配器把协议差异（流式帧格式、tool 调用结构、usage 字段位置）全部收敛在适配层，上层无感知。
- HTTP 客户端为按 `(endpoint, 事件循环)` 键控共享的 `httpx.AsyncClient`（`adapters/__init__.py`），高频流式调用下连接池复用，避免每次新建握手；超时逐请求生效。

### 2. 场景路由与会话级选模

- 位置：`backend/app/llm/gateway.py` 的 `resolve_scene()`；模型 `db/models/llm.py`
- 每个业务场景（`LlmScene`：问答 `chat`、建模代理 `modeling_agent`、ETL 助手、治理报告、改写、嵌入……）在配置页独立绑定一个 LLM 配置；网关按场景解析出 `ResolvedConfig`（endpoint、模型、温度、超时、重试次数、降级链）。
- **会话级覆盖**：对话会话（`Conversation.llm_config_id`）与建模会话都可单独选模型，优先级高于场景绑定。
- **切换策略语义**：配置上的 `switch_policy` 为 `new_session` 时，会话首次解析会快照 `llm_snapshot_config_id`，进行中的旧会话继续走旧配置，新会话用新配置；`immediate` 则全局即时生效。配置缓存（15s TTL）在 create/update/delete/activate 时即刻失效。

### 3. 超时重试与故障降级

- `_call_with_retry()`（`gateway.py`）：按配置的 `retry_count` 重试，仅对**可重试错误**（网络异常、5xx、限流）重试；4xx 鉴权类错误直接失败不浪费配额。
- **降级链**：`fallback_config_id` 最多串联 3 级——主配置失败自动切到备用配置（可以是另一家服务商），全部失败才把错误抛给上层。三级上限防止配置成环。
- 降级/重试全程记录进调用日志，失败率超阈值时可经 `ALERT_WEBHOOK_URL` 主动告警（5 分钟节流防轰炸），配置列表页同步显示近 15 分钟失败率徽标。

### 4. 可观测性

- 每次调用落 `LlmCallLog`（配置、场景、模型、输入/输出 token、时延、状态、错误信息）。
- `/llm/usage` 用 SQL `group_by` 聚合（按天/场景/模型），前端用量面板 ECharts 出图；`/llm/call-logs` 支持分页筛选。
- 嵌入（embedding）与对话分开计量，向量召回的成本也可观测。

---

## 二、问数 Agent 链路

> 多轮问题改写、意图路由、混合召回、LLM 辅助指标识别、SQL 生成与有限修复，经受控查询输出图表和报告，SSE 展示步骤。

主管线：`backend/app/agent/pipeline/orchestrator.py`（编排）+ `analysis.py`（结果分析）+ `finalize.py`（落库收尾）。完整步骤：

```
落库 → 缓存快查 → 改写 → 意图路由 → 记忆 → 实体链接(可中断确认) →
口径校验 → 表路由 → SQL生成 → 安全校验(修复循环) → 执行 → 校验计算 →
异常检测 → 图表 → (联网) → 流式解读 → 报表 → 回写记忆
```

### 1. 多轮问题改写与意图路由

- **改写**（`_rewrite`）：结合短期记忆里的会话上下文，把「那上个月呢？」这类指代问题改写成自包含问题。
- **路由**（`router_agent.py`）：LLM 把问题分类到：指标问数 / 库表与指标答疑 / ETL 需求 / 自定义 Skill / 闲聊。自定义 Skill 先按触发词匹配，内置安全拦截优先级最高。
- 时间语义由确定性解析器（`nlu/time_parser.py`）处理：「近 30 天」「上月」「环比」等转成明确日期区间与环比基准，不让 LLM 自由发挥日期。

### 2. 混合召回与指标识别

- **实体链接**（`nlu/entity_linker.py` + `kg/service.py` 的 `link_entities`）：把问题里的词面对齐到 KG 里的指标/维度（含别名、表名）。
- **三路候选召回**：元数据过滤 + KG 约束（指标-维度 ALLOWED/FORBIDDEN 边、TABLE_JOIN 邻接）+ Qdrant 向量召回（指标语义嵌入）。三路合并打分出候选表（`TableCandidate`）。
- **歧义不瞎猜**：置信度低于 `CONFIRM_THRESHOLD=0.9` 的指标，管线**中断并弹出候选确认卡片**，用户选定后带着 `confirm` 参数重入管线继续。
- **口径校验**：KG 规则引擎校验指标与维度的组合合法性（不可达组合直接拦截）。

### 3. SQL 生成、有限修复与受控执行

- **生成**（`skills/sql_gen.py`）：候选表 schema + KG 口径 + 数仓规范 + 记忆画像组装 prompt，要求 LLM 输出单条 SELECT。
- **AST 守卫**（`skills/sql_guard.py` 的 `guard_sql`）：sqlglot 解析语法树——单条 SELECT、表名必须在「元数据 ∩ 用户权限」白名单内、跨库限定名拒绝、危险函数黑名单、字符串字面量剥离后扫关键词、强制 LIMIT。
- **有限修复循环**：守卫或执行报错时，把错误信息回喂 LLM 重生成，最多 `SQL_FIX_MAX_ROUNDS=2` 轮，防无限循环烧钱。
- **受控执行**（`connectors/base.py`）：会话级只读设置（可开关）+ 语句超时 + 行数上限；并发同 SQL 走 singleflight 合并，不打爆数仓。
- **校验计算**（`_validate_compute`）：结果行做空值/量级/时间覆盖检查；结果列做列级脱敏（手机号/身份证等规则）后才出链路、才落缓存——**缓存里只有脱敏后的数据**。

### 4. 图表、报告与 SSE

- **图表**（`skills/chart.py` + `analysis.py`）：按结果形态（时间序列/分组占比/单值）自动选折线/柱状/饼图；趋势类自动跑异常检测（`skills/anomaly.py`）标注异常点与缺失日期。
- **报告**（`skills/report.py`）：答案一键沉淀为带图表的 Markdown 报告入报告库；每次查数也自动生成摘要报告留档。
- **SSE**（`pipeline/events.py`）：每个步骤实时推 `meta/step/sql/chart/token/done` 事件，前端步骤时间线逐个点亮；可中途取消；流被网关静默掐断时前端兜底结束运行态。鉴权走 Authorization 头而非 URL。

---

## 三、建模 Coding Agent

> JSON 动作协议驱动模型推理、工具调用与结果反馈；白名单工具查表结构、复用已有模型、加载数仓规范、校验 SQL；多轮生成分层模型、DDL 与加工 SQL。

实现：`backend/app/agent/coding_agent.py`，架构吸收 OpenAI Codex 的核心思想。

### 1. Agentic Loop

- `run_agent(mode, goal, scope)`：每轮把对话历史 + 工具结果发给 LLM，要求输出**严格 JSON 动作**——`{"action": "tool_call", "tool": ..., "args": ...}` 或 `{"action": "final", "proposal": ...}`。
- 宿主解析动作 → 执行工具 → 把结果（`_tool_summary` 摘要）回填 → 继续推理，最多 `AGENT_MAX_STEPS=8` 步。LLM 输出非法 JSON 或超步数都有明确降级提示，不静默产出低质方案。
- 全程 SSE 步骤流（`StepTimeline`），每个工具调用实时可见、可审计。

### 2. 白名单只读工具

六个工具，全部只读：**没有 shell、没有网络、没有文件写**，唯一的"写"是最终方案文本：

| 工具 | 作用 |
| --- | --- |
| `search_tables` | 按关键词/分层检索候选表 |
| `get_table_schema` | 查看字段、类型、注释、采样画像 |
| `list_layer_tables` | 列出某分层的已有表（避免重复建设） |
| `get_existing_tasks` | 查已有 ETL 任务（复用而非重建） |
| `validate_sql` | sqlglot 校验 + 元数据引用校验 |
| `get_dw_standards` | 读取已启用的数仓规范 |

### 3. 产出的三道机审与落地闭环

- `_post_validate()`：方案呈现给用户前必须过——危险语句剔除 → sqlglot 语法校验 → 数仓规范逐条检查（命名前缀、分区、中文注释、禁止反向跨层引用）。不过审则不呈现。
- **一键落地**：方案转 ETL 任务草稿；**依赖自动推导**——按方案的来源表匹配已有任务的目标表生成 `depends_on`（DFS 环检测防循环依赖）。
- **版本留档**：每次保存快照 DDL/加工 SQL，可回滚；任务级 AI 对话修订带行级 diff 对照。

---

## 四、业务语义本体（知识图谱）

> 统一指标口径、公式、聚合方式、粒度、别名与物理表字段映射，JOIN 关系和指标维度约束接入候选表排序、SQL 上下文及语义校验。

实现：`backend/app/kg/`（`builder.py` 构建、`service.py` 查询服务、`validator.py` 规则引擎），存储为关系型模型（`db/models/kg.py`）。

### 1. 语义模型

- **节点**：表、字段、指标、维度。指标登记业务口径：定义、计算公式、聚合方式、粒度、单位、别名；维度登记取值范围与字段映射。
- **边**：字段属于表、指标落在表、维度映射字段、表间 `TABLE_JOIN`、指标-维度 `ALLOWED`/`FORBIDDEN` 约束。
- **自动构建**：元数据扫描时自动生成图谱——采样值画像（distinct 比率）区分指标列与维度列；表名分层前缀推断 ODS→DWD→DWS→ADS 血缘。人工编辑的口径在重扫后保留，不被覆盖。
- **校验规则**：内置 + 自定义规则（如"指标必须有公式"），阻断/告警两级，支持批量校验报告。

### 2. 语义如何进入问数链路

- **候选表排序**：KG 约束参与召回打分——指标-维度 ALLOWED 边加权、FORBIDDEN 边剔除、JOIN 邻接表限缩组合空间。
- **SQL 上下文**：命中指标的口径卡（口径/公式/聚合/粒度/可查表）注入生成 prompt，LLM 按登记口径写 SQL 而不是自由发挥。
- **语义校验**：`validator.py` 在生成前后双向校验——不可达的指标×维度组合直接拦截，防止"口径上不存在"的查询被执行。
- **库表答疑**：`skills/table_qa.py` 把命中表的详情、字段、JOIN 边、血缘候选（按命名推断并明确标注"推断"）组装成上下文，元数据缺失时明示"暂无注释"而非编造。

---

## 五、平台工程与安全体系

> FastAPI + SQLAlchemy + React，接入多类数仓；AST 守卫、只读执行、权限审核控制查询与 ETL 写入；衔接内置调度、Azkaban 与 Airflow。

### 1. 技术底座

- **后端**：FastAPI 全异步 + SQLAlchemy 2.0 async（SQLite/PG）+ Alembic 迁移；APScheduler 驱动定时扫描与调度 tick。
- **前端**：React 18 + TS + Vite + zustand + ECharts；自研 i18n（中/英）；路由级代码分割（ECharts 605KB 移出首屏）。
- **连接器**（`connectors/registry.py`）：MySQL、PostgreSQL、ClickHouse、Hive、Spark SQL、Doris、StarRocks、SQLite 八种，统一 `Connector` 抽象（方言转译、标识符引号、只读会话、超时、采样画像）。

### 2. 查询与写入的安全边界（服务端强制，不靠提示词）

| 层 | 机制 |
| --- | --- |
| 查询 | sqlglot AST 守卫（仅 SELECT、表白名单、强制 LIMIT）→ 会话只读 + 超时 → 列级脱敏 → 审计 |
| 凭证 | 数据库密码 / LLM Key / 调度平台账号 AES-256-GCM 加密存储，前端永不回显 |
| 认证 | JWT + argon2；连续 5 次失败锁 10 分钟；未知用户 dummy 校验防时序枚举 |
| ETL 写入 | **DDL 任何路径绝不自动执行**；内置调度跑加工 SQL 需双重人工闸门（管理员审核 + 数据源开放写入），缺一则实例阻塞、零写动作 |
| Skill | 上传静态黑名单扫描（仅管理员）；Python 沙箱子进程：禁网、禁写盘、运行时路径围栏（读不到宿主机密）、超时/内存限制；调用全程审计 |
| 外部平台 | 推送 Azkaban 必须人工二次确认并审计；Airflow 只读对接 + DAG 模板导出人工放置 |
| 权限 | RBAC 三角色 + 数据源级授权；治理/图谱/任务接口按权限收窄；用户管理全链路审计 |

### 3. 调度三轨

- **内置引擎**（`services/scheduler_engine.py`）：cron 槽位点火、任务级 depends_on DAG（同业务日期上游全部成功才放行）、`${ds}` 业务日期变量、运行实例状态机（等待/运行中/成功/失败/阻塞/依赖超时）、手动触发（可级联下游）、失败重跑、DAG 总览；进程停机后启动时在补偿窗口内回补错过的时点（幂等去重）。
- **Azkaban**：Flow 2.0 工程 zip 生成（DDL 仅作附件），人工确认后推送，元数据（项目/flow/cron/执行历史）有界并发回读。
- **Airflow / 其他平台**：REST 只读 + DAG 模板导出；或上传 scheduler 型 Skill（沙箱禁网，实现 `build_package(task)` 契约生成可导入工程文件）。

### 4. 生产化

- 部署：Docker Compose（PG + Qdrant + backend + nginx 前端），多阶段非 root 镜像、依赖锁定、`/api/healthz` + `/api/readyz` 健康检查、X-Request-ID 链路日志、告警 webhook、优雅停机。
- 质量：后端 336 项 pytest + 前端 52 项 vitest + CI（pytest / tsc / build / docker build / ruff）门禁。
