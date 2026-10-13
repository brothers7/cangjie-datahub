/** HTTP 客户端：token 注入、统一错误、401 自动登出。 */
import type {
  AuditLogRow, AzkabanConfigOut, AzkabanMetadata, AzkabanPushResult, CompletionData,
  Conversation, CustomSkillRow, Datasource, Dimension, EtlDesignResult, EtlTaskRow,
  EtlTaskVersionRow, KgGraph, KgRule, LlmCallLog, LlmChatOption, LlmConfig, MaskingRuleRow,
  Me, MessageRow, MetaColumnRow, MetaTableRow, Metric, ModelingAdvice, ModelingOverview,
  ModelingSessionDetail, ModelingSessionRow, AcceptResult,
  Paged, QueryRecordRow, ReportDetail, ReportRow, ScanJob, SceneBindings, UsageRow, UserRow,
  ValidationFinding, DwStandardRow, GovernanceFinding, GovernanceRunRow,
  TaskMetaResult, SchedulerPlatform, ExportPackage, SchedulerRunRow, SchedulerDag,
  AirflowConfigOut, AirflowMetadata,
} from '@/types'

const TOKEN_KEY = 'da.token'

export class ApiError extends Error {
  code: string
  status: number
  constructor(message: string, code = 'ERROR', status = 0) {
    super(message)
    this.code = code
    this.status = status
  }
}

export const tokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY) || '',
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

/** 由 App 注册：401 时清理登录态并跳转 */
let onUnauthorized: (() => void) | null = null
export const setUnauthorizedHandler = (fn: () => void) => { onUnauthorized = fn }

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  const token = tokenStore.get()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')

  let resp: Response
  try {
    resp = await fetch(`/api/v1${path}`, { ...init, headers })
  } catch {
    throw new ApiError('网络请求失败，请检查后端服务是否启动', 'NETWORK', 0)
  }

  if (resp.status === 401) {
    tokenStore.clear()
    onUnauthorized?.()
    throw new ApiError('登录已过期，请重新登录', 'UNAUTHORIZED', 401)
  }
  if (resp.status === 204) return undefined as T

  const text = await resp.text()
  let data: unknown = null
  if (text) {
    try { data = JSON.parse(text) } catch { data = text }
  }
  if (!resp.ok) {
    const obj = (data ?? {}) as { message?: string; code?: string; detail?: unknown }
    const msg = obj.message || (typeof obj.detail === 'string' ? obj.detail : '') || `请求失败 (${resp.status})`
    throw new ApiError(msg, obj.code || 'HTTP_ERROR', resp.status)
  }
  return data as T
}

const qs = (params: Record<string, unknown>) => {
  const sp = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') sp.set(k, String(v))
  })
  const s = sp.toString()
  return s ? `?${s}` : ''
}

export const api = {
  // ---- 认证 / 用户 ----
  login: (username: string, password: string) =>
    request<{ token: string; username: string; display_name: string; role: string }>(
      '/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  me: () => request<Me>('/auth/me'),

  listUsers: () => request<UserRow[]>('/users'),
  createUser: (body: { username: string; password: string; display_name?: string; role?: string }) =>
    request<UserRow>('/users', { method: 'POST', body: JSON.stringify(body) }),
  updateUser: (id: string, body: Record<string, unknown>) =>
    request<UserRow>(`/users/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  getPermissions: (id: string) =>
    request<{ datasource_id: string; can_query: boolean }[]>(`/users/${id}/datasource-permissions`),
  setPermissions: (id: string, permissions: { datasource_id: string; can_query: boolean }[]) =>
    request<{ ok: boolean }>(`/users/${id}/datasource-permissions`,
      { method: 'PUT', body: JSON.stringify({ permissions }) }),

  // ---- 脱敏规则 ----
  listMaskingRules: (dsId: string) => request<MaskingRuleRow[]>(`/datasources/${dsId}/masking-rules`),
  createMaskingRule: (dsId: string, body: Record<string, unknown>) =>
    request<MaskingRuleRow>(`/datasources/${dsId}/masking-rules`, { method: 'POST', body: JSON.stringify(body) }),
  updateMaskingRule: (id: string, body: Record<string, unknown>) =>
    request<MaskingRuleRow>(`/datasources/masking-rules/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteMaskingRule: (id: string) =>
    request<{ ok: boolean }>(`/datasources/masking-rules/${id}`, { method: 'DELETE' }),

  // ---- 数据源 / 元数据 / 扫描 ----
  listDatasources: () => request<Datasource[]>('/datasources'),
  createDatasource: (body: Record<string, unknown>) =>
    request<Datasource>('/datasources', { method: 'POST', body: JSON.stringify(body) }),
  updateDatasource: (id: string, body: Record<string, unknown>) =>
    request<Datasource>(`/datasources/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteDatasource: (id: string) =>
    request<{ ok: boolean }>(`/datasources/${id}`, { method: 'DELETE' }),
  testDatasource: (id: string) =>
    request<{ ok: boolean; message: string }>(`/datasources/${id}/test`, { method: 'POST' }),
  scanDatasource: (id: string, mode: 'full' | 'incremental' = 'incremental') =>
    request<{ scan_job_id: string }>(`/datasources/${id}/scan`,
      { method: 'POST', body: JSON.stringify({ mode }) }),
  listTables: (dsId: string) => request<MetaTableRow[]>(`/datasources/${dsId}/tables`),
  listColumns: (tableId: string) => request<MetaColumnRow[]>(`/datasources/tables/${tableId}/columns`),
  updateColumn: (id: string, body: Record<string, unknown>) =>
    request<{ ok: boolean }>(`/datasources/columns/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  updateTable: (id: string, body: Record<string, unknown>) =>
    request<{ ok: boolean }>(`/datasources/tables/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),

  listScanJobs: (datasource_id?: string, limit = 20) =>
    request<ScanJob[]>(`/scan-jobs${qs({ datasource_id, limit })}`),
  getScanJob: (id: string) => request<ScanJob>(`/scan-jobs/${id}`),

  // ---- 知识图谱 ----
  kgGraph: (params: { datasource_id?: string; q?: string; types?: string; limit?: number } = {}) =>
    request<KgGraph>(`/kg/graph${qs(params)}`),
  listMetrics: (params: { datasource_id?: string; status?: string; domain?: string; q?: string } = {}) =>
    request<Metric[]>(`/kg/metrics${qs(params)}`),
  createMetric: (body: Record<string, unknown>) =>
    request<Metric>('/kg/metrics', { method: 'POST', body: JSON.stringify(body) }),
  updateMetric: (id: string, body: Record<string, unknown>) =>
    request<Metric>(`/kg/metrics/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteMetric: (id: string) => request<{ ok: boolean }>(`/kg/metrics/${id}`, { method: 'DELETE' }),
  mergeMetric: (id: string, target: string) =>
    request<{ ok: boolean }>(`/kg/metrics/${id}/merge`,
      { method: 'POST', body: JSON.stringify({ target_metric_id: target }) }),
  metricContext: (id: string) => request<{ context: string }>(`/kg/metrics/${id}/context`),
  listDimensions: (q?: string) => request<Dimension[]>(`/kg/dimensions${qs({ q })}`),
  updateDimension: (id: string, body: Record<string, unknown>) =>
    request<Dimension>(`/kg/dimensions/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteDimension: (id: string) => request<{ ok: boolean }>(`/kg/dimensions/${id}`, { method: 'DELETE' }),
  listRules: () => request<KgRule[]>('/kg/rules'),
  createRule: (body: Record<string, unknown>) =>
    request<{ id: string }>('/kg/rules', { method: 'POST', body: JSON.stringify(body) }),
  updateRule: (id: string, body: Record<string, unknown>) =>
    request<{ ok: boolean }>(`/kg/rules/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteRule: (id: string) => request<{ ok: boolean }>(`/kg/rules/${id}`, { method: 'DELETE' }),
  setMetricDimRule: (metric_id: string, dim_id: string, allowed: boolean | null) =>
    request<{ ok: boolean }>('/kg/metric-dim-rule',
      { method: 'POST', body: JSON.stringify({ metric_id, dim_id, allowed }) }),
  validationReport: (datasource_id?: string) =>
    request<ValidationFinding[]>(`/kg/validation/report${qs({ datasource_id })}`),

  // ---- LLM ----
  listLlmConfigs: () => request<LlmConfig[]>('/llm/configs'),
  listLlmChatOptions: () => request<LlmChatOption[]>('/llm/chat-options'),
  createLlmConfig: (body: Record<string, unknown>) =>
    request<LlmConfig>('/llm/configs', { method: 'POST', body: JSON.stringify(body) }),
  updateLlmConfig: (id: string, body: Record<string, unknown>) =>
    request<LlmConfig>(`/llm/configs/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteLlmConfig: (id: string) =>
    request<{ ok: boolean }>(`/llm/configs/${id}`, { method: 'DELETE' }),
  healthCheckLlm: (id: string) =>
    request<{ ok: boolean; message: string; latency_ms?: number }>(
      `/llm/configs/${id}/health`, { method: 'POST' }),
  activateLlm: (id: string, as_embedding = false) =>
    request<{ ok: boolean; switch_policy: string }>(`/llm/configs/${id}/activate`,
      { method: 'POST', body: JSON.stringify({ as_embedding }) }),
  getSceneBindings: () => request<SceneBindings>('/llm/scene-bindings'),
  setSceneBindings: (bindings: { scene: string; config_id: string }[]) =>
    request<{ ok: boolean }>('/llm/scene-bindings',
      { method: 'PUT', body: JSON.stringify({ bindings }) }),
  llmUsage: (group_by: 'day' | 'scene' | 'model' = 'day', days = 14) =>
    request<{ group_by: string; rows: UsageRow[] }>(`/llm/usage${qs({ group_by, days })}`),
  llmCallLogs: (params: { status?: string; scene?: string; page?: number; page_size?: number } = {}) =>
    request<Paged<LlmCallLog>>(`/llm/call-logs${qs(params)}`),

  // ---- Azkaban 调度平台 ----
  getAzkabanConfig: () => request<AzkabanConfigOut>('/azkaban/config'),
  putAzkabanConfig: (body: { url: string; username: string; password?: string; default_project?: string; verify_tls?: boolean }) =>
    request<AzkabanConfigOut>('/azkaban/config', { method: 'PUT', body: JSON.stringify(body) }),
  testAzkaban: () => request<{ ok: boolean; message: string }>('/azkaban/test', { method: 'POST' }),
  pushEtlToAzkaban: (design: EtlDesignResult, cron = '') =>
    request<AzkabanPushResult>('/azkaban/push', {
      method: 'POST',
      body: JSON.stringify({
        target_table: design.target_table, ddl: design.ddl, etl_sql: design.etl_sql,
        dialect: design.dialect || 'hive',
        notes: design.notes, cron,
      }),
    }),
  downloadEtlPackage: async (design: EtlDesignResult) => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    const token = tokenStore.get()
    if (token) headers.Authorization = `Bearer ${token}`
    const resp = await fetch('/api/v1/azkaban/package', {
      method: 'POST', headers,
      body: JSON.stringify({
        target_table: design.target_table, ddl: design.ddl, etl_sql: design.etl_sql,
        dialect: design.dialect || 'hive', notes: design.notes,
      }),
    })
    if (!resp.ok) throw new ApiError(`下载失败 (${resp.status})`, 'HTTP_ERROR', resp.status)
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${design.target_table || 'etl_flow'}.zip`
    a.click()
    URL.revokeObjectURL(url)
  },

  // ---- 自定义 Skill ----
  listSkills: (all = false) => request<CustomSkillRow[]>(`/skills${qs({ all: all ? 1 : '' })}`),
  uploadSkill: async (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    const headers: Record<string, string> = {}
    const token = tokenStore.get()
    if (token) headers.Authorization = `Bearer ${token}`
    const resp = await fetch('/api/v1/skills/upload', { method: 'POST', headers, body: fd })
    const data = await resp.json().catch(() => ({}))
    if (!resp.ok) throw new ApiError(data.message || `上传失败 (${resp.status})`, 'HTTP_ERROR', resp.status)
    return data as CustomSkillRow
  },
  setSkillStatus: (id: string, status: 'enabled' | 'disabled') =>
    request<CustomSkillRow>(`/skills/${id}/status`, { method: 'POST', body: JSON.stringify({ status }) }),
  deleteSkill: (id: string) => request<{ ok: boolean }>(`/skills/${id}`, { method: 'DELETE' }),

  // ---- 自动化建模（ETL 任务 / 全景 / 建议 / 编辑器） ----
  listEtlTasks: () => request<EtlTaskRow[]>('/etl/tasks'),
  createEtlTask: (body: Record<string, unknown>) =>
    request<EtlTaskRow>('/etl/tasks', { method: 'POST', body: JSON.stringify(body) }),
  updateEtlTask: (id: string, body: Record<string, unknown>) =>
    request<EtlTaskRow>(`/etl/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteEtlTask: (id: string) =>
    request<{ ok: boolean }>(`/etl/tasks/${id}`, { method: 'DELETE' }),
  validateEtlTask: (id: string) =>
    request<{ warnings: string[]; ok: boolean }>(`/etl/tasks/${id}/validate`, { method: 'POST' }),
  pushEtlTask: (id: string, cron = '') =>
    request<AzkabanPushResult>(`/etl/tasks/${id}/push`,
      { method: 'POST', body: JSON.stringify({ cron }) }),
  etlTaskVersions: (id: string) => request<EtlTaskVersionRow[]>(`/etl/tasks/${id}/versions`),
  rollbackEtlTask: (id: string, version: number) =>
    request<EtlTaskRow>(`/etl/tasks/${id}/rollback`,
      { method: 'POST', body: JSON.stringify({ version }) }),
  etlTaskExecutions: (id: string) =>
    request<{ rows: { exec_id: number; status: string; submit_time: number; end_time: number }[] }>(
      `/etl/tasks/${id}/executions`),
  azkabanMetadata: () => request<AzkabanMetadata>('/azkaban/metadata'),
  // ---- 调度平台配置：Airflow（PRD 3.3G） ----
  getAirflowConfig: () => request<AirflowConfigOut>('/airflow/config'),
  putAirflowConfig: (body: Record<string, unknown>) =>
    request<AirflowConfigOut>('/airflow/config', { method: 'PUT', body: JSON.stringify(body) }),
  testAirflow: () =>
    request<{ ok: boolean; message: string }>('/airflow/test', { method: 'POST' }),
  airflowMetadata: () => request<AirflowMetadata>('/airflow/metadata'),
  // ---- 内置调度平台（PRD 3.3G） ----
  schedulerRuns: (taskId = '') =>
    request<{ runs: SchedulerRunRow[] }>(`/scheduler/runs${qs({ task_id: taskId })}`),
  triggerTask: (id: string, ds = '') =>
    request<{ run: SchedulerRunRow }>(`/scheduler/tasks/${id}/trigger`,
      { method: 'POST', body: JSON.stringify({ ds }) }),
  retrySchedulerRun: (id: string) =>
    request<{ run: SchedulerRunRow }>(`/scheduler/runs/${id}/retry`, { method: 'POST' }),
  approveTask: (id: string) =>
    request<{ ok: boolean; approved_by: string; approved_at: string }>(
      `/scheduler/tasks/${id}/approve`, { method: 'POST' }),
  schedulerDag: () => request<SchedulerDag>('/scheduler/dag'),
  taskMetadata: (q = '') => request<TaskMetaResult>(`/modeling/task-metadata${qs({ q })}`),
  listSchedulers: () => request<{ platforms: SchedulerPlatform[] }>('/etl/tasks/schedulers'),
  exportTaskPackage: (id: string, scheduler: string) =>
    request<ExportPackage>(`/etl/tasks/${id}/export`,
      { method: 'POST', body: JSON.stringify({ scheduler }) }),
  importAzkabanFlow: (project: string, flow: string, cron = '') =>
    request<{ id: string; ok: boolean }>('/azkaban/import',
      { method: 'POST', body: JSON.stringify({ project, flow, cron }) }),
  modelingOverview: () => request<ModelingOverview>('/modeling/overview'),
  modelingAdvice: () =>
    request<ModelingAdvice>('/modeling/advice', { method: 'POST', body: '{}' }),
  completionData: (datasource_id?: string) =>
    request<CompletionData>(`/modeling/completion-data${qs({ datasource_id })}`),
  aiEdit: (body: { mode: string; instruction?: string; ddl?: string; etl_sql?: string
    datasource_id?: string | null; dialect?: string }) =>
    request<{ sql?: string; text?: string; warnings?: string[] }>('/modeling/ai-edit',
      { method: 'POST', body: JSON.stringify(body) }),

  // ---- AI 建模建议会话（PRD 3.3D-1） ----
  runGovernance: () =>
    request<GovernanceRunRow & { findings: GovernanceFinding[] }>('/governance/run', { method: 'POST' }),
  listGovernanceRuns: () => request<{ runs: GovernanceRunRow[] }>('/governance/runs'),
  getGovernanceRun: (id: string) =>
    request<GovernanceRunRow & { findings: GovernanceFinding[] }>(`/governance/runs/${id}`),
  governanceToTask: (body: Record<string, unknown>) =>
    request<{ task_id: string; name: string }>('/governance/findings/to-task', { method: 'POST', body: JSON.stringify(body) }),
  governanceProbe: (body: { datasource_id: string; table: string; columns?: string[] }) =>
    request<{ table: string; stats: Record<string, { distinct: number; non_null: number; ratio: number | null }>;
      findings: { level: string; column: string; issue: string; suggestion: string }[] }>(
      '/governance/probe', { method: 'POST', body: JSON.stringify(body) }),
  listStandards: () => request<{ standards: DwStandardRow[] }>('/standards'),
  standardRuleDefs: () => request<{ rules: { key: string; label: string }[] }>('/standards/rule-defs'),
  importStandard: async (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    const headers: Record<string, string> = {}
    const token = tokenStore.get()
    if (token) headers.Authorization = `Bearer ${token}`
    const resp = await fetch('/api/v1/standards/import', { method: 'POST', headers, body: fd })
    const data = await resp.json().catch(() => ({}))
    if (!resp.ok) throw new ApiError(data.message || `导入失败 (${resp.status})`, 'HTTP_ERROR', resp.status)
    return data as { name: string; content: string; rules: string[] }
  },
  createStandard: (body: Record<string, unknown>) =>
    request<DwStandardRow>('/standards', { method: 'POST', body: JSON.stringify(body) }),
  updateStandard: (id: string, body: Record<string, unknown>) =>
    request<DwStandardRow>(`/standards/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteStandard: (id: string) =>
    request<{ ok: boolean }>(`/standards/${id}`, { method: 'DELETE' }),
  modelingEngine: () =>
    request<{ engine: string; engine_name: string; scene: string; model: string;
      config_id: string; available: boolean }>('/modeling/engine'),
  listModelingSessions: () => request<ModelingSessionRow[]>('/modeling/sessions'),
  updateModelingSession: (id: string, body: { llm_config_id: string | null }) =>
    request<ModelingSessionRow>(`/modeling/sessions/${id}`,
      { method: 'PATCH', body: JSON.stringify(body) }),
  createModelingSession: (body: { title?: string; requirement: string; datasource_id?: string | null; llm_config_id?: string | null }) =>
    request<ModelingSessionRow>('/modeling/sessions', { method: 'POST', body: JSON.stringify(body) }),
  getModelingSession: (id: string) => request<ModelingSessionDetail>(`/modeling/sessions/${id}`),
  deleteModelingSession: (id: string) =>
    request<{ ok: boolean }>(`/modeling/sessions/${id}`, { method: 'DELETE' }),
  acceptModelingProposal: (sessionId: string, messageId: string) =>
    request<AcceptResult>(`/modeling/sessions/${sessionId}/accept`,
      { method: 'POST', body: JSON.stringify({ message_id: messageId }) }),

  // ---- 对话 ----
  listConversations: () => request<Conversation[]>('/chat/conversations'),
  createConversation: (title: string, datasource_id?: string | null, llm_config_id?: string | null) =>
    request<{ id: string; title: string }>('/chat/conversations',
      { method: 'POST', body: JSON.stringify({ title, datasource_id, llm_config_id }) }),
  updateConversation: (id: string, body: Record<string, unknown>) =>
    request<{ ok: boolean }>(`/chat/conversations/${id}`,
      { method: 'PATCH', body: JSON.stringify(body) }),
  deleteConversation: (id: string) =>
    request<{ ok: boolean }>(`/chat/conversations/${id}`, { method: 'DELETE' }),
  listMessages: (cid: string) => request<MessageRow[]>(`/chat/conversations/${cid}/messages`),
  cancelMessage: (cid: string) =>
    request<{ ok: boolean; cancelled: boolean }>(`/chat/conversations/${cid}/cancel`, { method: 'POST' }),

  // ---- 报表 / 记录 / 审计 ----
  listReports: (params: { q?: string; page?: number; page_size?: number } = {}) =>
    request<{ total: number; page: number; rows: ReportRow[] }>(`/reports${qs(params)}`),
  getReport: (id: string) => request<ReportDetail>(`/reports/${id}`),
  deleteReport: (id: string) => request<{ ok: boolean }>(`/reports/${id}`, { method: 'DELETE' }),
  listReportTemplates: () =>
    request<{ id: string; name: string; source: string; usage_count: number; structure: unknown }[]>(
      '/report-templates'),
  listQueryRecords: (params: { mine?: boolean; status?: string; datasource_id?: string; q?: string
    page?: number; page_size?: number } = {}) =>
    request<Paged<QueryRecordRow>>(`/query-records${qs(params)}`),
  listAuditLogs: (params: { action?: string; username?: string; page?: number; page_size?: number } = {}) =>
    request<Paged<AuditLogRow>>(`/audit-logs${qs(params)}`),
}

export type Api = typeof api
