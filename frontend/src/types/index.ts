/** 与后端 Pydantic DTO 对齐的 TS 类型。 */

export type Role = 'admin' | 'analyst' | 'viewer'

export interface TokenOut {
  token: string
  username: string
  display_name: string
  role: Role
}

export interface Me {
  id: string
  username: string
  display_name: string
  role: Role
}

export interface UserRow {
  id: string
  username: string
  display_name: string
  role: Role
  status: string
  last_login_at: string | null
  created_at: string
}

export interface MaskingRuleRow {
  id: string
  datasource_id: string
  column_pattern: string
  strategy: string
  enabled: boolean
  created_at: string
}

export interface Datasource {
  id: string
  name: string
  db_type: string
  host: string
  port: number
  database_name: string
  username: string
  status: 'connected' | 'error' | 'untested'
  status_message: string
  table_whitelist: string[]
  table_blacklist: string[]
  scan_cron: string | null
  sched_write_enabled: boolean
  readonly_enforced: boolean
  is_builtin: boolean
  last_test_at: string | null
  last_scan_at: string | null
  created_at: string
}

export interface MetaTableRow {
  id: string
  table_name: string
  comment: string
  layer: string
  row_count: number | null
  partition_cols: string[]
  source: 'auto' | 'manual'
  last_scan_at: string | null
}

export interface MetaColumnRow {
  id: string
  column_name: string
  data_type: string
  comment: string
  is_primary_key: boolean
  semantic_role: 'metric' | 'dimension' | 'time' | 'unknown'
  agg_hint: string | null
  value_examples: unknown[]
  source: 'auto' | 'manual'
}

export interface ScanJob {
  id: string
  datasource_id: string
  mode: 'full' | 'incremental'
  triggered_by: string
  status: 'running' | 'success' | 'failed' | 'partial'
  progress: number
  tables_total: number
  tables_changed: number
  entities_created: number
  entities_updated: number
  entities_skipped_manual: number
  error_log: string[]
  started_at: string | null
  finished_at: string | null
}

export interface Metric {
  id: string
  name: string
  aliases: string[]
  business_def: string
  formula: string
  agg_function: string | null
  unit: string
  value_range: number[] | null
  period_grain: string | null
  business_domain: string
  table_id: string | null
  status: string
  confidence: number | null
  source: 'auto' | 'manual'
  edited_fields: string[]
}

export interface Dimension {
  id: string
  name: string
  aliases: string[]
  description: string
  dimension_type: string
  value_examples: unknown[]
  related_columns: { table_id?: string; table_name?: string; column_name?: string }[]
  status: string
  source: 'auto' | 'manual'
}

export interface KgNode {
  id: string
  name: string
  type: 'metric' | 'dimension' | 'table'
  business_domain?: string
  agg?: string | null
  unit?: string
  source?: string
  table_id?: string | null
  dimension_type?: string
  layer?: string
  comment?: string
}

export interface KgEdge {
  src: string
  src_type: string
  dst: string
  dst_type: string
  edge_type: string
  properties: Record<string, unknown>
}

export interface KgGraph {
  nodes: KgNode[]
  edges: KgEdge[]
}

export interface KgRule {
  id: string
  rule_type: string
  target_type: string
  target_id: string | null
  rule_config: Record<string, unknown>
  severity: 'block' | 'warn'
  message: string
  enabled: boolean
  source: string
}

export interface ValidationFinding {
  category: string
  /** 校验器实际会返回 warn / error；block 为规则拦截级别 */
  severity: 'warn' | 'error' | 'block'
  entity_type: string
  entity_id: string
  entity_name: string
  message: string
  suggestion?: string
}

export interface LlmConfig {
  id: string
  name: string
  protocol: 'openai' | 'anthropic'
  kind: 'chat' | 'embedding'
  endpoint: string
  model: string
  temperature: number
  max_tokens: number
  top_p: number
  timeout_sec: number
  retry_count: number
  stream_enabled: boolean
  is_default: boolean
  is_default_embedding: boolean
  fallback_config_id: string | null
  switch_policy: 'immediate' | 'new_session'
  status: string
  status_message: string
  health_latency_ms: number | null
  last_health_at: string | null
}

export interface SceneBindings {
  scenes: string[]
  bindings: { scene: string; config_id: string }[]
}

export interface LlmCallLog {
  id: string
  config_id: string | null
  scene: string | null
  protocol: string | null
  model: string | null
  input_tokens: number
  output_tokens: number
  latency_ms: number
  status: 'ok' | 'timeout' | 'error' | 'degraded'
  error_brief: string
  created_at: string
}

export interface Conversation {
  id: string
  title: string
  datasource_id: string | null
  llm_config_id: string | null
  message_count: number
  last_active_at: string | null
  created_at: string
}

export interface LlmChatOption {
  id: string
  name: string
  model: string
  protocol: string
  is_default: boolean
  health_status: string
}

/** ETL 开发助手产出（PRD 3.3B）：仅生成，DDL 绝不自动执行 */
export interface EtlDesignResult {
  target_table: string
  layer: string
  schedule: string
  ddl: string
  etl_sql: string
  dialect: string
  source_tables: { table: string; layer: string; reason: string }[]
  field_mapping: { target: string; source: string; rule: string }[]
  notes: string
  warnings: string[]
}

/** 自定义 Skill 记录（PRD 3.3C） */
export interface CustomSkillRow {
  id: string
  name: string
  description: string
  version: number
  skill_type: 'prompt' | 'python' | 'scheduler'
  trigger_keywords: string[]
  status: 'enabled' | 'disabled'
  created_by: string
  last_invoked_at: string | null
  invoke_count: number
  created_at: string
}

export interface AzkabanConfigOut {
  configured: boolean
  url: string
  username: string
  default_project: string
  verify_tls: boolean
  status: string
  status_message: string
}

export interface AzkabanPushResult {
  ok: boolean
  message: string
  project?: string
  flow?: string
  url?: string
}

/** ETL 建模任务（PRD 3.3D 自动化建模） */
export interface EtlTaskRow {
  id: string
  name: string
  target_table: string
  layer: string
  dialect: string
  schedule: string
  cron: string
  depends_on: string[]
  ddl: string
  etl_sql: string
  source_tables: { table: string; layer: string; reason: string }[]
  field_mapping: { target: string; source: string; rule: string }[]
  notes: string
  warnings: string[]
  status: 'draft' | 'pushed'
  source: 'chat' | 'manual' | 'ai' | 'imported'
  azkaban_project: string
  azkaban_flow: string
  pushed_at: string | null
  datasource_id: string | null
  sched_enabled: boolean
  approved_by: string
  approved_at: string | null
  created_at: string
  updated_at: string
}

/** 内置调度平台（PRD 3.3G） */
export interface SchedulerRunRow {
  id: string
  task_id: string
  task_name: string
  target_table: string
  ds: string
  status: 'waiting' | 'running' | 'success' | 'failed' | 'blocked' | 'dep_timeout'
  trigger_type: string
  message: string
  duration_ms: number
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export interface SchedulerDag {
  nodes: { id: string; name: string; target_table: string; layer: string; cron: string
    approved: boolean; last_status: string | null; last_ds: string | null }[]
  edges: { from: string; to: string }[]
}

export interface AirflowConfigOut {
  configured: boolean
  url: string
  username: string
  verify_tls: boolean
  status: string
  status_message: string
}

export interface AirflowMetadata {
  configured: boolean
  error?: string
  total?: number
  dags: { dag_id: string; is_paused: boolean; schedule: string | null
    last_run: { state: string; start_date: string } | null }[]
}

export interface EtlTaskVersionRow {
  version: number
  saved_by: string
  created_at: string
  ddl_len: number
  sql_len: number
}

export interface ModelingOverview {
  table_total: number
  by_layer: Record<string, number>
  uncommented: number
  uncommented_ratio: number
  isolated_tables: string[]
  no_task_tables: string[]
  task_total: number
  task_pushed: number
}

/** 任务元数据统一搜索（PRD 3.3D-4） */
export interface TaskMetaResult {
  tasks: {
    id: string; name: string; target_table: string; layer: string; cron: string
    status: string; source: string; azkaban_project: string; azkaban_flow: string
    sched_enabled: boolean; updated_at: string
  }[]
  tables: {
    name: string; layer: string; comment: string; partition_cols: string[]
    row_count: number | null; col_count: number
  }[]
}

/** 调度平台（内置调度 + Azkaban/Airflow 内置适配 + scheduler 型 Skill） */
export interface SchedulerPlatform {
  key: string; name: string; kind: 'local' | 'builtin' | 'scheduler_skill'; description?: string
}

/** 调度 Skill 导出的工程包 */
export interface ExportPackage {
  ok: boolean; filename: string; content: string; note: string; log?: string
}

export interface ModelingAdvice {
  findings: { level: 'ok' | 'info' | 'warn' | 'block'; title: string; detail: string }[]
  report: string
  overview: ModelingOverview
}

export interface CompletionData {
  keywords: string[]
  tables: {
    name: string; comment: string; layer: string; partition_cols: string[]
    columns: { name: string; type: string; comment: string }[]
  }[]
}

export interface AzkabanMetadata {
  configured: boolean
  error?: string
  projects: {
    name: string
    flows: {
      flow: string; cron: string; linked_task_id: string | null
      last_status?: string; last_end_time?: number
    }[]
  }[]
}

/** AI 建模建议会话（PRD 3.3D-1） */
export interface ProposalTable {
  name: string
  layer: string
  comment: string
  grain?: string
  ddl: string
  etl_sql: string
  schedule?: string
  cron?: string
  depends_on?: string[]
  source_tables?: string[]
  rationale?: string
}

export interface ModelingProposal {
  summary: string
  tables: ProposalTable[]
  reuse_notes?: string
  risks?: string[]
}

export interface ModelingSessionRow {
  id: string
  title: string
  requirement: string
  status: 'active' | 'accepted' | 'archived'
  message_count: number
  datasource_id: string | null
  llm_config_id: string | null
  created_at: string
  updated_at: string
}

export interface ModelingMessageRow {
  id: string
  seq: number
  role: 'user' | 'assistant'
  content: string
  proposal: ModelingProposal | null
  steps: { step: string; status: string; detail: string; ms: number }[]
  created_at: string
}

export interface ModelingSessionDetail extends ModelingSessionRow {
  messages: ModelingMessageRow[]
}

export interface AcceptResult {
  results: { table: string; ok: boolean; task_id?: string; error?: string }[]
  created: number
  message: string
}

/** 任务级 AI 修订产出（PRD 3.3D-3） */
export interface TaskRevision {
  ddl: string
  etl_sql: string
  change_summary: string
  notes?: string
}

/** 图表规格（后端 ChartSpec，前端 ECharts 渲染契约） */
export interface ChartSpec {
  type: 'stat_card' | 'line' | 'bar' | 'hbar' | 'pie' | 'table'
  title: string
  unit: string
  x: string[]
  series: { name: string; data: (number | null)[]; _dashed?: boolean }[]
  cards: {
    label: string; value: number | null; unit?: string; sub?: string
    prev?: number | null; pct?: number | null; trend?: 'up' | 'down' | 'flat'
  }[]
  pies: { name: string; value: number; share?: number }[]
  columns: string[]
  rows: (string | number | null)[][]
  anomalies: { x: string; value: number; direction: string; note: string; severity: string }[]
  compare_label: string
}

export interface AnomalyReport {
  points: { x: string; value: number; direction: 'up' | 'down'; note: string; severity: string }[]
  missing_dates: string[]
}

/** 一次问答落库的完整结构（Message.payload） */
/** 指标口径卡（PRD 3.3-7 库表与指标答疑） */
export interface MetricCard {
  name: string
  aliases: string[]
  business_def: string
  formula: string
  agg_function: string
  unit: string
  period_grain: string
  business_domain: string
  tables: { name: string; layer: string; comment: string }[]
}

export interface MessagePayload {
  steps: { step: string; status: string; detail: string; ms: number }[]
  intent: string
  rewritten: string
  matched_metrics: { id: string; name: string }[]
  dimensions: { id: string; name: string }[]
  time_range: { start: string; end: string; label: string; grain?: string } | null
  sql: string
  chart_spec: ChartSpec | null
  anomalies: AnomalyReport | Record<string, never>
  validation: { warnings?: string[]; empty?: boolean; [k: string]: unknown }
  web_evidences: { title: string; url: string; snippet: string }[]
  cache_hit: boolean
  error: string
  report?: { id: string; title: string }
  etl?: EtlDesignResult | null
  metric_cards?: MetricCard[] | null
  pending_confirm?: { id: string; name: string; confidence: number; why: string }[]
}

export interface MessageRow {
  id: string
  role: 'user' | 'assistant'
  content: string
  payload: MessagePayload | null
  created_at: string
}

export interface ReportRow {
  id: string
  title: string
  view_count: number
  created_at: string
  sections: string[]
}

export interface ReportDetail {
  id: string
  title: string
  content_md: string
  sections: { key: string; title: string; content_md: string }[]
  view_count: number
  created_at: string
}

export interface QueryRecordRow {
  id: string
  question: string
  rewritten_question: string
  datasource_id: string | null
  status: string
  row_count: number
  duration_ms: number
  cache_hit: boolean
  chart_type: string
  sql_text: string
  error_brief: string
  created_at: string
}

export interface AuditLogRow {
  id: string
  username: string
  action: string
  resource_type: string
  resource_id: string
  detail: Record<string, unknown>
  ip: string
  created_at: string
}

export interface Paged<T> {
  total: number
  page: number
  rows: T[]
}

export interface UsageRow {
  key: string
  input_tokens: number
  output_tokens: number
  calls: number
  errors: number
}

/** 数仓规范（PRD 3.3E） */
export interface DwStandardRow {
  id: string
  name: string
  content: string
  rules: string[]
  enabled: boolean
  updated_by: string
  created_at: string
  updated_at: string
}

/** AI 数据治理（PRD 3.3F） */
export interface GovernanceFinding {
  domain: string
  level: 'block' | 'warn' | 'info'
  object: string
  title: string
  evidence: string
  suggestion: string
  action: 'none' | 'new_task' | 'revise_task'
  task_id: string
}

export interface GovernanceRunRow {
  id: string
  stats: Record<string, number>
  report: string
  finding_count: number
  created_at: string
  findings?: GovernanceFinding[]
}
