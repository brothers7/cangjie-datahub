/** 前后端共用的标签映射（键与后端枚举值严格一致），文案双语。
 *
 * 兼容说明：旧的默认中文导出（SCENE_LABELS / AUDIT_ACTIONS / CALL_STATUS_LABELS /
 * QUERY_STATUS / auditLabel）保留为中文别名，新代码请使用带 lang 参数的
 * sceneLabels / auditActions / callStatusLabels / queryStatus / auditLabel(action, lang)。
 */
import type { Lang } from '@/i18n'

/** LLM 场景（app/db/models/llm.py :: LlmScene） */
const SCENE_LABELS_I18N: Record<Lang, Record<string, string>> = {
  en: {
    question_rewrite: 'Question Rewrite',
    intent_route: 'Intent Routing',
    entity_link_assist: 'Entity Linking Assist',
    sql_generate: 'SQL Generation',
    sql_fix: 'SQL Fixing',
    anomaly_analysis: 'Anomaly Analysis',
    report_generate: 'Report Generation',
    chat_answer: 'Chat Answer',
    table_qa: 'Table Q&A',
    etl_design: 'ETL Design',
    custom_skill: 'Custom Skill',
    modeling_agent: 'AI Modeling Engineer',
    governance: 'AI Data Governance',
    reflection: 'Experience Reflection',
    embed_query: 'Query Embedding',
  },
  zh: {
    question_rewrite: '问题改写',
    intent_route: '意图识别',
    entity_link_assist: '实体链接辅助',
    sql_generate: 'SQL 生成',
    sql_fix: 'SQL 纠错',
    anomaly_analysis: '异常解读',
    report_generate: '报表生成',
    chat_answer: '对话回答',
    table_qa: '库表答疑',
    etl_design: 'ETL 方案生成',
    custom_skill: '自定义 Skill',
    modeling_agent: 'AI 建模工程师',
    governance: 'AI 数据治理',
    reflection: '经验反思',
    embed_query: '查询向量化',
  },
}

/** 审计动作（后端 AuditLog.action 的实际取值） */
const AUDIT_ACTIONS_I18N: Record<Lang, Record<string, string>> = {
  en: {
    login: 'Login',
    login_failed: 'Login Failed',
    'datasource.create': 'Create Datasource',
    'datasource.update': 'Update Datasource',
    'datasource.delete': 'Delete Datasource',
    'llm.create': 'Create Model',
    'llm.update': 'Update Model',
    'llm.activate': 'Switch Default Model',
    'azkaban.config': 'Configure Azkaban',
    'azkaban.push': 'Push Azkaban Project',
    'skill.upload': 'Upload Custom Skill',
    'skill.enabled': 'Enable Skill',
    'skill.disabled': 'Disable Skill',
    'skill.delete': 'Delete Skill',
    'skill.invoke': 'Invoke Skill',
    'modeling.propose': 'AI Modeling Proposal',
    'modeling.accept': 'Accept Modeling Proposal',
    'governance.run': 'Run Governance Inspection',
    'governance.to_task': 'Finding to Task',
    'governance.probe': 'Data Quality Probe',
    'standard.create': 'Create DW Standard',
    'standard.update': 'Update DW Standard',
    'standard.delete': 'Delete DW Standard',
    'etl_task.ai_revise': 'AI Revise Task',
    'etl_task.export': 'Export Scheduler Package',
    'airflow.config': 'Configure Airflow Platform',
    'scheduler.approve': 'Approve Task (Built-in Scheduler)',
    'scheduler.trigger': 'Trigger Task Manually',
    'scheduler.retry': 'Rerun Instance',
  },
  zh: {
    login: '登录',
    login_failed: '登录失败',
    'datasource.create': '新建数据源',
    'datasource.update': '修改数据源',
    'datasource.delete': '删除数据源',
    'llm.create': '新建模型',
    'llm.update': '修改模型',
    'llm.activate': '切换默认模型',
    'azkaban.config': '配置 Azkaban',
    'azkaban.push': '推送 Azkaban 工程',
    'skill.upload': '上传自定义 Skill',
    'skill.enabled': '启用 Skill',
    'skill.disabled': '停用 Skill',
    'skill.delete': '删除 Skill',
    'skill.invoke': '调用 Skill',
    'modeling.propose': 'AI 建模方案',
    'modeling.accept': '接受建模方案',
    'governance.run': '发起治理巡检',
    'governance.to_task': '治理项转任务',
    'governance.probe': '数据质量探查',
    'standard.create': '新建数仓规范',
    'standard.update': '更新数仓规范',
    'standard.delete': '删除数仓规范',
    'etl_task.ai_revise': 'AI 修订任务',
    'etl_task.export': '导出调度工程包',
    'airflow.config': '配置 Airflow 平台',
    'scheduler.approve': '审核任务（内置调度）',
    'scheduler.trigger': '手动触发任务',
    'scheduler.retry': '重跑运行实例',
  },
}

/** LLM 调用状态（LlmCallStatus） */
const CALL_STATUS_LABELS_I18N: Record<Lang, Record<string, string>> = {
  en: { ok: 'OK', timeout: 'Timeout', error: 'Error', degraded: 'Degraded' },
  zh: { ok: '正常', timeout: '超时', error: '失败', degraded: '降级' },
}

/** 查询状态（QueryStatus） */
const QUERY_STATUS_I18N: Record<Lang, Record<string, { text: string; cls: string }>> = {
  en: {
    success: { text: 'Success', cls: 'ok' },
    empty: { text: 'No Data', cls: 'warn' },
    blocked: { text: 'Blocked', cls: 'danger' },
    error: { text: 'Failed', cls: 'danger' },
  },
  zh: {
    success: { text: '成功', cls: 'ok' },
    empty: { text: '无数据', cls: 'warn' },
    blocked: { text: '已拦截', cls: 'danger' },
    error: { text: '失败', cls: 'danger' },
  },
}

// ---------- 新 API：按语言取标签 ----------

export const sceneLabels = (lang: Lang): Record<string, string> => SCENE_LABELS_I18N[lang]
export const sceneLabel = (key: string, lang: Lang): string => SCENE_LABELS_I18N[lang][key] || key

export const auditActions = (lang: Lang): Record<string, string> => AUDIT_ACTIONS_I18N[lang]
/** 未知动作直接展示原始值，便于后端新增动作时不丢信息 */
export const auditLabel = (action: string, lang: Lang = 'zh'): string =>
  AUDIT_ACTIONS_I18N[lang][action] || action

export const callStatusLabels = (lang: Lang): Record<string, string> => CALL_STATUS_LABELS_I18N[lang]
export const callStatusLabel = (key: string, lang: Lang): string => CALL_STATUS_LABELS_I18N[lang][key] || key

export const queryStatus = (status: string, lang: Lang): { text: string; cls: string } | undefined =>
  QUERY_STATUS_I18N[lang][status]
