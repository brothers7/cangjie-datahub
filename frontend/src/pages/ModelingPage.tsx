/** 自动化建模模块（PRD 3.3D）：建模任务（SQL 编辑器 + AI 修订）/ AI 建模建议会话 / 数仓全景 / 任务元数据（任务+表统一搜索，Azkaban 远端工程）。 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, ApiError } from '@/api/client'
import { streamSse } from '@/api/sse'
import { StepTimeline, type StepState } from '@/components/chat/StepTimeline'
import { Markdown } from '@/components/common/Markdown'
import { Empty, Field, fmtTime, relTime, Spinner, StatusTag, Tabs } from '@/components/common/ui'
import { StandardsPanel } from '@/components/modeling/StandardsPanel'
import { AiModelingPanel } from '@/components/modeling/AiModelingPanel'
import { DiffView } from '@/components/modeling/DiffView'
import { SqlEditor } from '@/components/modeling/SqlEditor'
import { toast } from '@/stores/app'
import { useAuth } from '@/stores/auth'
import { useI18n } from '@/i18n'
import type { AzkabanMetadata, CompletionData, EtlTaskRow, ModelingAdvice, ModelingOverview, SchedulerDag, SchedulerPlatform, SchedulerRunRow, TaskMetaResult, TaskRevision } from '@/types'

const LAYER_CLS: Record<string, string> = { ods: '', dwd: '', dws: 'accent', ads: 'ok', dim: 'warn' }

type TFunc = (key: string, vars?: Record<string, string | number>) => string

/** 运行实例状态文案（模块级中文常量改为随语言计算的函数，见 i18n 规则 5） */
const runStatusLabel = (t: TFunc): Record<string, string> => ({
  waiting: t('modeling.runStatusWaiting'), running: t('modeling.runStatusRunning'),
  success: t('common.success'), failed: t('common.failed'),
  blocked: t('modeling.runStatusBlocked'), dep_timeout: t('modeling.runStatusDepTimeout'),
})

export default function ModelingPage() {
  const { t } = useI18n()
  const [params, setParams] = useSearchParams()
  // 跨页跳转（治理/ETL 助手/AI 建模落地）携带 ?task= 时直接落到任务编辑（?revise= 同理）
  const [tab, setTab] = useState<'ai' | 'tasks' | 'standards' | 'overview' | 'meta' | 'sched'>(
    params.get('task') || params.get('revise') ? 'tasks' : 'ai')
  const openTask = (id: string) => {
    setParams({ task: id })
    setTab('tasks')
  }
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row wrap">
        <h2 style={{ margin: 0 }}>{t('modeling.title')}</h2>
        <span className="spacer" />
        <span className="tiny muted">{t('modeling.subtitle')}</span>
      </div>
      <Tabs value={tab} onChange={setTab} items={[
        { key: 'ai', label: t('modeling.tabAi') },
        { key: 'tasks', label: t('modeling.tabTasks') },
        { key: 'standards', label: t('modeling.tabStandards') },
        { key: 'overview', label: t('modeling.tabOverview') },
        { key: 'meta', label: t('modeling.tabMeta') },
        { key: 'sched', label: t('modeling.tabSched') },
      ]} />
      {tab === 'ai' && <AiModelingPanel onAccepted={() => setTab('tasks')} />}
      {tab === 'tasks' && <TasksPanel key={params.get('task') || ''} />}
      {tab === 'standards' && <StandardsPanel />}
      {tab === 'overview' && <><OverviewPanel /><AdvicePanel /></>}
      {tab === 'meta' && <TaskMetaPanel onOpenTask={openTask} />}
      {tab === 'sched' && <SchedulerCenterPanel onOpenTask={openTask} />}
    </div>
  )
}

/* ================= 建模任务 + SQL 编辑器 ================= */

function TasksPanel() {
  const { t } = useI18n()
  const [params] = useSearchParams()
  const [tasks, setTasks] = useState<EtlTaskRow[]>([])
  const [activeId, setActiveId] = useState('')
  const [draft, setDraft] = useState<EtlTaskRow | null>(null)
  const [completion, setCompletion] = useState<CompletionData | null>(null)
  const [aiInput, setAiInput] = useState('')
  const [busy, setBusy] = useState('')
  const [versions, setVersions] = useState<{ version: number; saved_by: string; created_at: string }[] | null>(null)
  const [execs, setExecs] = useState<{ exec_id: number; status: string; end_time: number }[] | null>(null)
  const isAdmin = useAuth((st) => st.user?.role === 'admin')
  const [localRuns, setLocalRuns] = useState<SchedulerRunRow[] | null>(null)
  // AI 对话式修订（PRD 3.3D-3）
  const [reviseHistory, setReviseHistory] = useState<{ role: string; content: string }[]>([])
  const [reviseInput, setReviseInput] = useState('')
  // 治理项「去 AI 修订」带入的建议指令（PRD 3.3F）
  useEffect(() => {
    const v = params.get('revise')
    if (v) setReviseInput(v)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const [revising, setRevising] = useState(false)
  const [reviseSteps, setReviseSteps] = useState<StepState[]>([])
  const [revision, setRevision] = useState<{ data: TaskRevision; warnings: string[]; engine: string; engine_version: string } | null>(null)
  const reviseAbortRef = useRef<(() => void) | null>(null)

  const load = useCallback(async (keep?: string) => {
    try {
      const rows = await api.listEtlTasks()
      setTasks(rows)
      const want = keep || params.get('task') || ''
      const target = rows.find((t) => t.id === want) || rows[0]
      if (target) {
        setActiveId(target.id)
        setDraft({ ...target })
      }
    } catch (e) { toast.error((e as ApiError).message) }
  }, [params])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    api.completionData(draft?.datasource_id ?? undefined).then(setCompletion).catch(() => undefined)
  }, [draft?.datasource_id])

  const active = useMemo(() => tasks.find((t) => t.id === activeId) || null, [tasks, activeId])
  const dirty = active && draft && (active.ddl !== draft.ddl || active.etl_sql !== draft.etl_sql
    || active.name !== draft.name || active.cron !== draft.cron || active.notes !== draft.notes
    || active.target_table !== draft.target_table || active.sched_enabled !== draft.sched_enabled
    || JSON.stringify(active.depends_on) !== JSON.stringify(draft.depends_on))

  const select = (t: EtlTaskRow) => {
    setActiveId(t.id)
    setDraft({ ...t })
    setVersions(null)
    setExecs(null)
    setLocalRuns(null)
    reviseAbortRef.current?.()
    setRevising(false)
    setReviseHistory([])
    setRevision(null)
    setReviseSteps([])
  }

  const newTask = async () => {
    try {
      const row = await api.createEtlTask({ name: t('modeling.newTaskDefaultName'), source: 'manual' })
      await load(row.id)
      toast.ok(t('modeling.taskDraftCreated'))
    } catch (e) { toast.error((e as ApiError).message) }
  }

  /** 保存当前草稿；返回是否成功——后续动作（校验/推送/导出/启用调度）依赖保存成功才继续 */
  const save = async (): Promise<boolean> => {
    if (!draft) return false
    setBusy('save')
    try {
      const row = await api.updateEtlTask(draft.id, {
        name: draft.name, target_table: draft.target_table, cron: draft.cron,
        schedule: draft.schedule, ddl: draft.ddl, etl_sql: draft.etl_sql, notes: draft.notes,
        depends_on: draft.depends_on, sched_enabled: draft.sched_enabled,
      })
      setTasks((prev) => prev.map((x) => x.id === row.id ? row : x))
      setDraft({ ...row })
      toast.ok(t('modeling.savedArchived'))
      return true
    } catch (e) { toast.error((e as ApiError).message); return false } finally { setBusy('') }
  }

  const validate = async () => {
    if (!draft) return
    if (!(await save())) return
    setBusy('validate')
    try {
      const r = await api.validateEtlTask(draft.id)
      setDraft((d) => d && { ...d, warnings: r.warnings })
      if (r.ok && r.warnings.length === 0) toast.ok(t('modeling.validateOk'))
      else if (r.ok) toast.warn(t('modeling.validateOkHints', { n: r.warnings.length }))
      else toast.error(t('modeling.validateIssues', { n: r.warnings.length }))
    } catch (e) { toast.error((e as ApiError).message) } finally { setBusy('') }
  }

  // 调度平台（PRD 3.3D-4）：内置 Azkaban 直连推送 + scheduler 型 Skill 导出工程包
  const [schedulers, setSchedulers] = useState<SchedulerPlatform[]>([])
  const [platform, setPlatform] = useState('azkaban')
  useEffect(() => {
    api.listSchedulers().then((r) => setSchedulers(r.platforms)).catch(() => undefined)
  }, [])

  const push = async () => {
    if (!draft) return
    const p = schedulers.find((x) => x.key === platform)
    if (p && (p.kind === 'local' || p.key === 'local')) { await enableLocal(); return }
    if (p && (p.kind === 'builtin' && p.key === 'airflow')) { await exportAirflow(); return }
    if (p && p.kind === 'scheduler_skill') { await exportPackage(p); return }
    if (!window.confirm(t('modeling.confirmPushAzkaban', { name: draft.name }))) return
    if (!(await save())) return
    setBusy('push')
    try {
      const r = await api.pushEtlTask(draft.id, draft.cron)
      if (r.ok) { toast.ok(r.message); await load(draft.id) }
      else toast.warn(r.message)
    } catch (e) { toast.error((e as ApiError).message) } finally { setBusy('') }
  }

  const exportPackage = async (p: SchedulerPlatform) => {
    if (!draft) return
    if (!window.confirm(t('modeling.confirmExportPackage', { name: p.name }))) return
    if (!(await save())) return
    setBusy('push')
    try {
      const r = await api.exportTaskPackage(draft.id, p.key.replace(/^skill:/, ''))
      if (!r.ok || !r.content) { toast.warn(r.log || t('modeling.exportFailed')); return }
      const url = URL.createObjectURL(new Blob([r.content], { type: 'text/plain;charset=utf-8' }))
      const a = document.createElement('a')
      a.href = url
      a.download = r.filename || `${draft.target_table || 'task'}.txt`
      a.click()
      URL.revokeObjectURL(url)
      toast.ok(t('modeling.exportedFile', { filename: r.filename }) + (r.note ? ` · ${r.note}` : ''))
    } catch (e) { toast.error((e as ApiError).message) } finally { setBusy('') }
  }

  /** 内置调度平台：保存并启用调度（PRD 3.3G） */
  const enableLocal = async () => {
    if (!draft) return
    if (!draft.cron.trim()) { toast.warn(t('modeling.cronRequired')); return }
    if (!window.confirm(t('modeling.confirmEnableLocal'))) return
    if (!(await save())) return
    try {
      const row = await api.updateEtlTask(draft.id, { sched_enabled: true })
      setTasks((prev) => prev.map((x) => (x.id === row.id ? row : x)))
      setDraft({ ...row })
      toast.ok(t('modeling.localEnabled'))
    } catch (e) { toast.error((e as ApiError).message) }
  }

  /** Airflow 内置模板导出 DAG（PRD 3.3G） */
  const exportAirflow = async () => {
    if (!draft) return
    if (!window.confirm(t('modeling.confirmExportAirflow'))) return
    if (!(await save())) return
    setBusy('push')
    try {
      const r = await api.exportTaskPackage(draft.id, 'airflow')
      const url = URL.createObjectURL(new Blob([r.content], { type: 'text/plain;charset=utf-8' }))
      const a = document.createElement('a')
      a.href = url
      a.download = r.filename
      a.click()
      URL.revokeObjectURL(url)
      toast.ok(t('modeling.exportedFile', { filename: r.filename }) + (r.note ? ` · ${r.note}` : ''))
    } catch (e) { toast.error((e as ApiError).message) } finally { setBusy('') }
  }

  const approve = async () => {
    if (!draft || !window.confirm(t('modeling.confirmApprove', { name: draft.name }))) return
    try {
      const r = await api.approveTask(draft.id)
      setDraft((d) => d && { ...d, approved_by: r.approved_by, approved_at: r.approved_at })
      toast.ok(t('modeling.approvedToast', { name: r.approved_by }))
    } catch (e) { toast.error((e as ApiError).message) }
  }

  const triggerNow = async () => {
    if (!draft) return
    try {
      const r = await api.triggerTask(draft.id)
      const st = r.run.status
      if (st === 'success') toast.ok(t('modeling.runSuccess', { ds: r.run.ds }))
      else if (st === 'blocked') toast.warn(t('modeling.runBlocked', { msg: r.run.message }))
      else if (st === 'waiting') toast.warn(t('modeling.runWaiting', { msg: r.run.message }))
      else toast.error(t('modeling.runFailed', { msg: r.run.message }))
      await loadLocalRuns()
    } catch (e) { toast.error((e as ApiError).message) }
  }

  const loadLocalRuns = async () => {
    if (!draft) return
    try { setLocalRuns((await api.schedulerRuns(draft.id)).runs) }
    catch (e) { toast.error((e as ApiError).message) }
  }

  const retryRun = async (id: string) => {
    try {
      const r = await api.retrySchedulerRun(id)
      toast.ok(t('modeling.retried', { status: r.run.status }))
      await loadLocalRuns()
    } catch (e) { toast.error((e as ApiError).message) }
  }

  const ai = async (mode: 'generate' | 'fix' | 'explain') => {
    if (!draft) return
    if (mode === 'generate' && !aiInput.trim()) { toast.warn(t('modeling.aiInputRequired')); return }
    setBusy('ai')
    try {
      const r = await api.aiEdit({
        mode, instruction: aiInput, ddl: draft.ddl, etl_sql: draft.etl_sql,
        datasource_id: draft.datasource_id, dialect: draft.dialect || 'hive',
      })
      if (mode === 'explain') {
        toast.ok(t('modeling.aiExplained'))
        setDraft((d) => d && { ...d, notes: (d.notes ? d.notes + '\n\n' : '') + (r.text || '') })
      } else if (r.sql) {
        setDraft((d) => d && { ...d, etl_sql: r.sql! })
        toast.ok(t('modeling.aiGeneratedSql'))
      }
    } catch (e) { toast.error((e as ApiError).message) } finally { setBusy('') }
  }

  const loadVersions = async () => {
    if (!draft) return
    try { setVersions(await api.etlTaskVersions(draft.id)) } catch (e) { toast.error((e as ApiError).message) }
  }

  // ---- AI 对话式修订 ----
  const revise = () => {
    const instruction = reviseInput.trim()
    if (!draft || !instruction) return
    if (revising) { toast.warn(t('modeling.reviseInProgress')); return }
    setReviseInput('')
    setRevising(true)
    setReviseSteps([])
    setRevision(null)
    setReviseHistory((prev) => [...prev, { role: 'user', content: instruction }])
    reviseAbortRef.current = streamSse(`/api/v1/etl/tasks/${draft.id}/ai-revise`,
      { instruction, history: reviseHistory }, {
        onStep: (d) => setReviseSteps((prev) => {
          const steps = [...prev]
          const i = steps.findIndex((x) => x.step === d.step && x.status === 'running')
          if (d.status === 'started') steps.push({ step: d.step, status: 'running' })
          else if (i >= 0) steps[i] = { ...steps[i], status: d.status as StepState['status'], detail: d.detail, ms: d.ms }
          else steps.push({ step: d.step, status: d.status as StepState['status'], detail: d.detail, ms: d.ms })
          return steps
        }),
        onEvent: (name, d) => {
          if (name === 'revision') {
            const r = d.revision as TaskRevision | null
            if (r) {
              setRevision({ data: r, warnings: (d.warnings as string[]) || [],
                engine: String(d.engine || ''), engine_version: String(d.engine_version || '') })
              setReviseHistory((prev) => [...prev, { role: 'assistant', content: r.change_summary || t('modeling.revisionProduced') }])
            }
          }
        },
        onError: (d) => toast.error(d.message),
        onDone: (d) => {
          setRevising(false)
          if (d.error) toast.warn(t('modeling.reviseIncomplete', { error: d.error }))
        },
      })
  }

  const acceptRevision = async () => {
    if (!draft || !revision) return
    const next = { ...draft, ddl: revision.data.ddl, etl_sql: revision.data.etl_sql }
    setBusy('save')
    try {
      const row = await api.updateEtlTask(draft.id, { ddl: next.ddl, etl_sql: next.etl_sql })
      setTasks((prev) => prev.map((x) => x.id === row.id ? row : x))
      setDraft({ ...row })
      toast.ok(t('modeling.revisionApplied'))
      setRevision(null)
      setReviseSteps([])
    } catch (e) { toast.error((e as ApiError).message) } finally { setBusy('') }
  }

  const rollback = async (v: number) => {
    if (!draft || !window.confirm(t('modeling.confirmRollback', { v }))) return
    try {
      const row = await api.rollbackEtlTask(draft.id, v)
      setTasks((prev) => prev.map((x) => x.id === row.id ? row : x))
      setDraft({ ...row })
      setVersions(null)
      toast.ok(t('modeling.rolledBack', { v }))
    } catch (e) { toast.error((e as ApiError).message) }
  }

  const loadExecs = async () => {
    if (!draft) return
    try { setExecs((await api.etlTaskExecutions(draft.id)).rows) } catch (e) { toast.error((e as ApiError).message) }
  }

  const remove = async () => {
    if (!draft || !window.confirm(t('modeling.confirmDeleteTask', { name: draft.name }))) return
    try {
      await api.deleteEtlTask(draft.id)
      toast.ok(t('common.deleted'))
      setDraft(null)
      setActiveId('')
      await load()
    } catch (e) { toast.error((e as ApiError).message) }
  }

  return (
    <div className="row" style={{ alignItems: 'flex-start', gap: 16 }}>
      {/* 任务列表 */}
      <div className="card" style={{ width: 280, flexShrink: 0 }}>
        <div className="card-body" style={{ padding: 8 }}>
          <button className="btn primary" style={{ width: '100%', marginBottom: 8 }} onClick={() => void newTask()}>
            ＋ {t('modeling.newTask')}
          </button>
          {tasks.length === 0 && <div className="empty tiny" style={{ padding: 16 }}>{t('modeling.noTasks')}</div>}
          {tasks.map((row) => (
            <div key={row.id} className={`conv ${row.id === activeId ? 'active' : ''}`}
              style={{ padding: '8px 10px', cursor: 'pointer', borderRadius: 6 }}
              onClick={() => select(row)}>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="t truncate" style={{ fontSize: 13 }}>{row.name}</div>
                <div className="d tiny muted">
                  {row.target_table || t('modeling.noTargetTable')} · {relTime(row.updated_at, t)}
                </div>
              </div>
              <div className="col" style={{ gap: 4, alignItems: 'flex-end' }}>
                <StatusTag value={row.sched_enabled || row.status === 'pushed' ? 'connected' : 'untested'}
                  label={row.sched_enabled ? t('modeling.statusScheduled') : row.status === 'pushed' ? t('modeling.statusPushed') : t('modeling.statusDraft')} />
                <span className="tag tiny">{row.source}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* 编辑器 */}
      <div style={{ flex: 1, minWidth: 0 }}>
        {!draft && <Empty icon="🏗" text={t('modeling.selectOrCreate')} />}
        {draft && (
          <div className="col" style={{ gap: 12 }}>
            <div className="card"><div className="card-body">
              <div className="row wrap" style={{ gap: 12 }}>
                <Field label={t('modeling.taskName')}>
                  <input className="input" value={draft.name}
                    onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
                </Field>
                <Field label={t('modeling.targetTable')}>
                  <input className="input mono" value={draft.target_table} placeholder="dws_xxx_daily"
                    onChange={(e) => setDraft({ ...draft, target_table: e.target.value })} />
                </Field>
                <Field label={t('modeling.cronOptional')}>
                  <input className="input mono" value={draft.cron} placeholder="0 30 2 * * ?"
                    onChange={(e) => setDraft({ ...draft, cron: e.target.value })} />
                </Field>
              </div>
              {draft.azkaban_flow && (
                <div className="tiny muted mt8">
                  {t('modeling.azkabanLinked')} {draft.azkaban_project} / {draft.azkaban_flow}
                  {draft.pushed_at && ` · ${t('modeling.pushedAt', { time: relTime(draft.pushed_at, t) })}`}
                  <button className="btn ghost sm" style={{ marginLeft: 8 }} onClick={() => void loadExecs()}>{t('modeling.execHistory')}</button>
                </div>
              )}
              {execs && (
                <div className="mt8">
                  {execs.length === 0 && <span className="tiny muted">{t('modeling.noExecs')}</span>}
                  {execs.map((e) => (
                    <div key={e.exec_id} className="tiny">
                      <StatusTag value={e.status === 'SUCCEEDED' ? 'success' : e.status === 'FAILED' ? 'failed' : 'running'}
                        label={e.status} /> #{e.exec_id} · {e.end_time ? fmtTime(new Date(e.end_time).toISOString()) : '-'}
                    </div>
                  ))}
                </div>
              )}
            </div></div>

            {/* 内置调度平台（PRD 3.3G）：依赖任务 + 双重闸门 + 运行实例 */}
            <div className="card"><div className="card-body">
              <div className="row" style={{ marginBottom: 6 }}>
                <b>⏱ {t('modeling.schedTitle')}</b>
                {draft.sched_enabled
                  ? <span className="tag ok tiny">{t('common.enabled')}</span>
                  : <span className="tag tiny">{t('modeling.schedNotEnabled')}</span>}
                {draft.approved_by
                  ? <span className="tag ok tiny">{t('modeling.approvedTag', { name: draft.approved_by })}</span>
                  : <span className="tag warn tiny">{t('modeling.pendingApproval')}</span>}
                <span className="spacer" />
                <span className="tiny muted">{t('modeling.doubleGate')}</span>
              </div>
              <div className="row" style={{ alignItems: 'flex-start', gap: 16 }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="tiny dim" style={{ marginBottom: 4 }}>
                    {t('modeling.dependsHint')}
                  </div>
                  <div style={{ maxHeight: 110, overflow: 'auto', border: '1px solid var(--border)', borderRadius: 6, padding: 6 }}>
                    {tasks.filter((row) => row.id !== draft.id).length === 0 && (
                      <span className="tiny muted">{t('modeling.noOtherTasks')}</span>
                    )}
                    {tasks.filter((row) => row.id !== draft.id).map((row) => (
                      <label key={row.id} className="tiny row" style={{ gap: 6, cursor: 'pointer', padding: '2px 0' }}>
                        <input type="checkbox"
                          checked={draft.depends_on.includes(row.id)}
                          onChange={(e) => setDraft({
                            ...draft,
                            depends_on: e.target.checked
                              ? [...draft.depends_on, row.id]
                              : draft.depends_on.filter((x) => x !== row.id),
                          })} />
                        <span className="truncate">{row.name}</span>
                        <code className="tiny muted">{row.target_table}</code>
                        {row.layer && <span className={`tag tiny ${LAYER_CLS[row.layer] || ''}`}>{row.layer.toUpperCase()}</span>}
                      </label>
                    ))}
                  </div>
                </div>
                <div className="col" style={{ gap: 6, flexShrink: 0 }}>
                  <label className="tiny row" style={{ gap: 6, cursor: 'pointer' }}>
                    <input type="checkbox" checked={draft.sched_enabled}
                      onChange={(e) => setDraft({ ...draft, sched_enabled: e.target.checked })} />
                    {t('modeling.enableLocalSched')}
                  </label>
                  {isAdmin && !draft.approved_by && (
                    <button className="btn sm" onClick={() => void approve()}>✔ {t('modeling.approveBtn')}</button>
                  )}
                  <button className="btn ghost sm" disabled={!draft.sched_enabled}
                    onClick={() => void triggerNow()}>▶ {t('modeling.triggerNow')}</button>
                  <button className="btn ghost sm" onClick={() => void loadLocalRuns()}>{t('modeling.runHistory')}</button>
                </div>
              </div>
              {localRuns && (
                <div className="mt8">
                  {localRuns.length === 0 && <span className="tiny muted">{t('modeling.noRuns')}</span>}
                  {localRuns.length > 0 && (
                    <table className="tbl" style={{ fontSize: 12 }}>
                      <thead><tr><th>{t('modeling.colDs')}</th><th>{t('common.status')}</th><th>{t('modeling.colTrigger')}</th><th>{t('modeling.colDuration')}</th><th>{t('modeling.colNote')}</th><th></th></tr></thead>
                      <tbody>
                        {localRuns.map((r) => (
                          <tr key={r.id}>
                            <td className="mono">{r.ds}</td>
                            <td><StatusTag
                              value={r.status === 'success' ? 'success' : r.status === 'failed' ? 'failed' : 'running'}
                              label={runStatusLabel(t)[r.status]} /></td>
                            <td className="muted">{r.trigger_type}</td>
                            <td className="muted">{r.duration_ms ? `${(r.duration_ms / 1000).toFixed(1)}s` : '-'}</td>
                            <td className="muted tiny">{r.message || '-'}</td>
                            <td style={{ textAlign: 'right' }}>
                              {(r.status === 'failed' || r.status === 'blocked' || r.status === 'dep_timeout') && (
                                <button className="btn ghost sm" onClick={() => void retryRun(r.id)}>{t('modeling.rerun')}</button>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              )}
            </div></div>

            <div className="card"><div className="card-body">
              <div className="row" style={{ marginBottom: 6 }}>
                <b>{t('modeling.ddlTitle')}</b>
                <span className="tag warn">{t('modeling.ddlNoAutoExec')}</span>
              </div>
              <SqlEditor value={draft.ddl} rows={6} data={completion}
                placeholder="CREATE TABLE IF NOT EXISTS dws_xxx_daily (...) COMMENT '...' PARTITIONED BY (dt string)"
                onChange={(v) => setDraft({ ...draft, ddl: v })} />
            </div></div>

            <div className="card"><div className="card-body">
              <div className="row" style={{ marginBottom: 6, justifyContent: 'space-between' }}>
                <b>{t('modeling.etlSqlTitle')}</b>
                <div className="row" style={{ gap: 6 }}>
                  <input className="input" style={{ width: 260, padding: '4px 8px', fontSize: 12.5 }}
                    placeholder={t('modeling.aiGeneratePlaceholder')}
                    value={aiInput} onChange={(e) => setAiInput(e.target.value)} />
                  <button className="btn sm" disabled={busy === 'ai'} onClick={() => void ai('generate')}>✨ {t('modeling.aiGenerate')}</button>
                  <button className="btn sm ghost" disabled={busy === 'ai' || !draft.etl_sql}
                    onClick={() => void ai('fix')}>🔧 {t('modeling.aiFix')}</button>
                  <button className="btn sm ghost" disabled={busy === 'ai' || !draft.etl_sql}
                    onClick={() => void ai('explain')}>📖 {t('modeling.aiExplain')}</button>
                </div>
              </div>
              <SqlEditor value={draft.etl_sql} rows={10} data={completion}
                placeholder={"INSERT OVERWRITE TABLE dws_xxx_daily PARTITION(dt='${ds}')\nSELECT ..."}
                onChange={(v) => setDraft({ ...draft, etl_sql: v })} />
            </div></div>

            {/* AI 对话式修订（PRD 3.3D-3）：提修改意见 → AI 产出修订 → diff 对照 → 接受/拒绝 */}
            <div className="card"><div className="card-body">
              <div className="row" style={{ marginBottom: 6 }}>
                <b>🤖 {t('modeling.aiReviseTitle')}</b>
                {revision && (
                  <span className="tag tiny ok">{t('modeling.builtinAgentTag')}{revision.engine_version ? ` · ${revision.engine_version}` : ''}</span>
                )}
                <span className="spacer" />
                <span className="tiny muted">{t('modeling.reviseHint')}</span>
              </div>
              {reviseHistory.length > 0 && (
                <div className="col" style={{ gap: 6, marginBottom: 8 }}>
                  {reviseHistory.map((h, i) => (
                    <div key={i} className={`tiny ${h.role === 'user' ? '' : 'muted'}`}
                      style={{ padding: '4px 8px', borderRadius: 6,
                        background: h.role === 'user' ? 'var(--bg)' : 'transparent' }}>
                      {h.role === 'user' ? '🧑 ' : '🤖 '}{h.content}
                    </div>
                  ))}
                </div>
              )}
              {revising && (
                <div className="mb8">
                  <StepTimeline steps={reviseSteps} live />
                  <Spinner text={t('modeling.reviseSpinner')} />
                </div>
              )}
              {revision && (
                <div className="col" style={{ gap: 8, marginBottom: 8 }}>
                  <div className="tag ok" style={{ display: 'block', whiteSpace: 'normal' }}>
                    <b>{t('modeling.changeSummary')}</b>{revision.data.change_summary}
                    {revision.data.notes && <div className="tiny" style={{ marginTop: 2 }}>{revision.data.notes}</div>}
                  </div>
                  {revision.warnings.length > 0 && (
                    <div className="tag warn" style={{ display: 'block', whiteSpace: 'normal' }}>
                      {revision.warnings.map((w, i) => <div key={i}>⚠ {w}</div>)}
                    </div>
                  )}
                  <div className="tiny dim">{t('modeling.ddlDiff')}</div>
                  <DiffView oldText={draft.ddl} newText={revision.data.ddl} />
                  <div className="tiny dim">{t('modeling.sqlDiff')}</div>
                  <DiffView oldText={draft.etl_sql} newText={revision.data.etl_sql} />
                  <div className="row" style={{ gap: 8 }}>
                    <button className="btn primary sm" disabled={busy === 'save'}
                      onClick={() => void acceptRevision()}>✅ {t('modeling.acceptRevision')}</button>
                    <button className="btn ghost sm" onClick={() => setRevision(null)}>{t('modeling.reject')}</button>
                    <span className="tiny muted" style={{ alignSelf: 'center' }}>{t('modeling.reviseMore')}</span>
                  </div>
                </div>
              )}
              <div className="row" style={{ gap: 8 }}>
                <input className="input" style={{ flex: 1, padding: '6px 10px', fontSize: 12.5 }}
                  placeholder={t('modeling.revisePlaceholder')}
                  value={reviseInput} onChange={(e) => setReviseInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); revise() } }} />
                <button className="btn sm" disabled={revising || !reviseInput.trim()}
                  onClick={revise}>{revising ? t('modeling.revisingBtn') : `✨ ${t('modeling.submitRevision')}`}</button>
              </div>
            </div></div>

            {draft.warnings.length > 0 && (
              <div className="tag warn" style={{ display: 'block', whiteSpace: 'normal' }}>
                <b>{t('modeling.validateWarnings')}</b>
                <ul style={{ margin: '4px 0 0 16px' }}>
                  {draft.warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              </div>
            )}

            <div className="row wrap" style={{ gap: 8 }}>
              <button className="btn primary" disabled={!dirty || busy === 'save'} onClick={() => void save()}>
                {busy === 'save' ? t('modeling.savingBtn') : t('common.save')}
              </button>
              <button className="btn" disabled={busy === 'validate'} onClick={() => void validate()}>{t('modeling.validateBtn')}</button>
              <select className="input" style={{ width: 'auto', padding: '5px 8px', fontSize: 12.5 }}
                value={platform} onChange={(e) => setPlatform(e.target.value)}
                title={t('modeling.platformSelectTitle')}>
                {(schedulers.length ? schedulers : [{ key: 'azkaban', name: t('modeling.azkabanBuiltinName'), kind: 'builtin' as const }])
                  .map((p) => <option key={p.key} value={p.key}>{p.name}</option>)}
              </select>
              <button className="btn" disabled={busy === 'push' || !draft.etl_sql} onClick={() => void push()}>
                {busy === 'push' ? t('modeling.processingBtn')
                  : platform === 'local' ? `⏱ ${t('modeling.enableSchedBtn')}`
                  : platform === 'airflow' ? `📦 ${t('modeling.exportDagBtn')}`
                  : platform === 'azkaban' ? `🚀 ${t('modeling.pushBtn')}` : `📦 ${t('modeling.exportPkgBtn')}`}
              </button>
              <button className="btn ghost" onClick={() => void loadVersions()}>{t('modeling.versionsBtn')}</button>
              <span className="spacer" />
              <button className="btn danger sm" onClick={() => void remove()}>{t('modeling.deleteTask')}</button>
            </div>

            {versions && (
              <div className="card"><div className="card-body">
                <div className="tiny dim mb8">{t('modeling.versionsDesc')}</div>
                {versions.length === 0 && <span className="tiny muted">{t('modeling.noVersions')}</span>}
                {versions.map((v) => (
                  <div key={v.version} className="row tiny" style={{ padding: '3px 0' }}>
                    <span>v{v.version}</span>
                    <span className="muted">{v.saved_by} · {relTime(v.created_at, t)}</span>
                    <span className="spacer" />
                    <button className="btn ghost sm" onClick={() => void rollback(v.version)}>{t('modeling.rollbackTo')}</button>
                  </div>
                ))}
              </div></div>
            )}

            <div className="card"><div className="card-body">
              <div className="tiny dim mb8">{t('modeling.notesLabel')}</div>
              <textarea className="input" rows={3} value={draft.notes}
                onChange={(e) => setDraft({ ...draft, notes: e.target.value })} />
            </div></div>
          </div>
        )}
      </div>
    </div>
  )
}

/* ================= 数仓全景 ================= */

function OverviewPanel() {
  const { t } = useI18n()
  const [ov, setOv] = useState<ModelingOverview | null>(null)
  useEffect(() => {
    api.modelingOverview().then(setOv).catch((e) => toast.error((e as ApiError).message))
  }, [])
  if (!ov) return <div className="center" style={{ padding: 40 }}><Spinner text={t('modeling.statsLoading')} /></div>
  const layers = ['ods', 'dwd', 'dws', 'ads', 'dim', 'unknown']
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row wrap" style={{ gap: 12 }}>
        <StatCard label={t('modeling.statTableTotal')} value={ov.table_total} />
        <StatCard label={t('modeling.statUncommented')} value={ov.uncommented}
          sub={t('modeling.statRatio', { n: (ov.uncommented_ratio * 100).toFixed(0) })} warn={ov.uncommented_ratio > 0.3} />
        <StatCard label={t('modeling.statIsolated')} value={ov.isolated_tables.length} warn={ov.isolated_tables.length > 0} />
        <StatCard label={t('modeling.statTasks')} value={ov.task_total} sub={t('modeling.statPushed', { n: ov.task_pushed })} />
      </div>
      <div className="card"><div className="card-body">
        <div className="tiny dim mb8">{t('modeling.layerDist')}</div>
        <div className="row wrap" style={{ gap: 10 }}>
          {layers.map((l) => (
            <span key={l} className={`tag ${LAYER_CLS[l] || ''}`} style={{ fontSize: 13 }}>
              {l.toUpperCase()}：{ov.by_layer[l] || 0}
            </span>
          ))}
        </div>
      </div></div>
      {ov.isolated_tables.length > 0 && (
        <div className="card"><div className="card-body">
          <div className="tiny dim mb8">{t('modeling.isolatedDesc')}</div>
          <div className="chips">{ov.isolated_tables.map((tbl) => <code key={tbl} className="tag">{tbl}</code>)}</div>
        </div></div>
      )}
      {ov.no_task_tables.length > 0 && (
        <div className="card"><div className="card-body">
          <div className="tiny dim mb8">{t('modeling.noTaskTables')}</div>
          <div className="chips">{ov.no_task_tables.map((tbl) => <code key={tbl} className="tag warn">{tbl}</code>)}</div>
        </div></div>
      )}
    </div>
  )
}

function StatCard({ label, value, sub, warn }: { label: string; value: number; sub?: string; warn?: boolean }) {
  return (
    <div className="card" style={{ minWidth: 160 }}>
      <div className="card-body">
        <div className="tiny muted">{label}</div>
        <div style={{ fontSize: 26, fontWeight: 600, color: warn ? 'var(--warn)' : undefined }}>{value}</div>
        {sub && <div className="tiny muted">{sub}</div>}
      </div>
    </div>
  )
}

/* ================= AI 建模建议 ================= */

function AdvicePanel() {
  const { t } = useI18n()
  const [data, setData] = useState<ModelingAdvice | null>(null)
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    try { setData(await api.modelingAdvice()) }
    catch (e) { toast.error((e as ApiError).message) }
    finally { setBusy(false) }
  }
  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="row">
        <button className="btn primary" disabled={busy} onClick={() => void run()}>
          {busy ? t('modeling.adviceAnalyzing') : `🔍 ${t('modeling.adviceRun')}`}
        </button>
        <span className="tiny muted" style={{ alignSelf: 'center' }}>
          {t('modeling.adviceHint')}
        </span>
      </div>
      {data?.findings.map((f, i) => (
        <div key={i} className={`tag ${f.level === 'block' ? 'danger' : f.level === 'warn' ? 'warn' : f.level === 'ok' ? 'ok' : ''}`}
          style={{ display: 'block', whiteSpace: 'normal', padding: '8px 12px' }}>
          <b>{f.title}</b>
          <div className="tiny" style={{ marginTop: 2 }}>{f.detail}</div>
        </div>
      ))}
      {data?.report && (
        <div className="card"><div className="card-body">
          <div className="tiny dim mb8">{t('modeling.adviceReport')}</div>
          <Markdown text={data.report} />
        </div></div>
      )}
      {data && !data.report && (
        <div className="tiny muted">{t('modeling.adviceNoLlm')}</div>
      )}
    </div>
  )
}

/* ================= 调度中心（PRD 3.3G：内置调度平台 DAG 总览） ================= */

const RUN_STATUS_TAG: Record<string, string> = {
  success: 'success', failed: 'failed', running: 'running',
}

function SchedulerCenterPanel({ onOpenTask }: { onOpenTask: (id: string) => void }) {
  const { t } = useI18n()
  const [dag, setDag] = useState<SchedulerDag | null>(null)
  const [runs, setRuns] = useState<SchedulerRunRow[]>([])
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setBusy(true)
    try {
      const [d, r] = await Promise.all([api.schedulerDag(), api.schedulerRuns()])
      setDag(d)
      setRuns(r.runs.slice(0, 30))
    } catch (e) { toast.error((e as ApiError).message) }
    finally { setBusy(false) }
  }, [])
  useEffect(() => { void load() }, [load])

  const retry = async (id: string) => {
    try {
      const r = await api.retrySchedulerRun(id)
      toast.ok(t('modeling.retried', { status: runStatusLabel(t)[r.run.status] || r.run.status }))
      await load()
    } catch (e) { toast.error((e as ApiError).message) }
  }

  const upstreams = (id: string) =>
    (dag?.edges.filter((e) => e.to === id) || [])
      .map((e) => dag?.nodes.find((n) => n.id === e.from)?.name || '?')

  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row">
        <button className="btn" disabled={busy} onClick={() => void load()}>↻ {t('common.refresh')}</button>
        <span className="tiny muted" style={{ alignSelf: 'center' }}>
          {t('modeling.schedCenterHint')}
        </span>
      </div>

      {/* DAG 节点 */}
      <div className="card"><div className="card-body">
        <div className="tiny dim mb8">{t('modeling.dagTitle', { nodes: dag?.nodes.length ?? 0, edges: dag?.edges.length ?? 0 })}</div>
        {!dag || dag.nodes.length === 0
          ? <div className="tiny muted">{t('modeling.dagEmpty')}</div>
          : (
            <table className="tbl" style={{ fontSize: 12.5 }}>
              <thead><tr><th>{t('modeling.colTask')}</th><th>{t('modeling.targetTable')}</th><th>{t('modeling.colLayer')}</th><th>cron</th><th>{t('modeling.colUpstream')}</th><th>{t('modeling.colApproval')}</th><th>{t('modeling.colLastRun')}</th><th></th></tr></thead>
              <tbody>
                {dag.nodes.map((n) => (
                  <tr key={n.id}>
                    <td>{n.name}</td>
                    <td><code>{n.target_table}</code></td>
                    <td>{n.layer ? <span className={`tag ${LAYER_CLS[n.layer] || ''}`}>{n.layer.toUpperCase()}</span> : '-'}</td>
                    <td className="muted mono">{n.cron || '-'}</td>
                    <td className="tiny muted">{upstreams(n.id).join('、') || t('common.none')}</td>
                    <td>{n.approved ? <span className="tag ok tiny">{t('modeling.approved')}</span> : <span className="tag warn tiny">{t('modeling.pendingApproval')}</span>}</td>
                    <td>{n.last_status
                      ? <StatusTag value={RUN_STATUS_TAG[n.last_status] || 'running'}
                          label={`${runStatusLabel(t)[n.last_status] || n.last_status} · ${n.last_ds}`} />
                      : <span className="muted tiny">{t('modeling.neverRun')}</span>}</td>
                    <td style={{ textAlign: 'right' }}>
                      <button className="btn ghost sm" onClick={() => onOpenTask(n.id)}>{t('modeling.openBtn')}</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </div></div>

      {/* 最近运行实例 */}
      <div className="card"><div className="card-body">
        <div className="tiny dim mb8">{t('modeling.recentRuns')}</div>
        {runs.length === 0 && <div className="tiny muted">{t('modeling.noRuns')}</div>}
        {runs.length > 0 && (
          <table className="tbl" style={{ fontSize: 12 }}>
            <thead><tr><th>{t('modeling.colTask')}</th><th>{t('modeling.colDs')}</th><th>{t('common.status')}</th><th>{t('modeling.colTrigger')}</th><th>{t('modeling.colDuration')}</th><th>{t('modeling.colNote')}</th><th></th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td>{r.task_name}</td>
                  <td className="mono">{r.ds}</td>
                  <td><StatusTag value={RUN_STATUS_TAG[r.status] || 'running'}
                      label={runStatusLabel(t)[r.status] || r.status} /></td>
                  <td className="muted">{r.trigger_type}</td>
                  <td className="muted">{r.duration_ms ? `${(r.duration_ms / 1000).toFixed(1)}s` : '-'}</td>
                  <td className="muted tiny">{r.message || '-'}</td>
                  <td style={{ textAlign: 'right' }}>
                    {(r.status === 'failed' || r.status === 'blocked' || r.status === 'dep_timeout') && (
                      <button className="btn ghost sm" onClick={() => void retry(r.id)}>{t('modeling.rerun')}</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div></div>
    </div>
  )
}

/* ================= 任务元数据（PRD 3.3D-4：任务 + 表统一搜索） ================= */

function TaskMetaPanel({ onOpenTask }: { onOpenTask: (id: string) => void }) {
  const { t } = useI18n()
  const [q, setQ] = useState('')
  const [data, setData] = useState<TaskMetaResult | null>(null)
  const [busy, setBusy] = useState(false)

  const search = useCallback(async (kw: string) => {
    setBusy(true)
    try { setData(await api.taskMetadata(kw)) }
    catch (e) { toast.error((e as ApiError).message) }
    finally { setBusy(false) }
  }, [])

  useEffect(() => { void search('') }, [search])
  useEffect(() => {  // 输入防抖搜索
    const t = setTimeout(() => void search(q), 300)
    return () => clearTimeout(t)
  }, [q, search])

  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row">
        <input className="input" style={{ maxWidth: 420 }}
          placeholder={`🔍 ${t('modeling.metaSearchPlaceholder')}`}
          value={q} onChange={(e) => setQ(e.target.value)} />
        {busy && <Spinner />}
        <span className="tiny muted" style={{ alignSelf: 'center' }}>
          {t('modeling.metaSearchHint')}
        </span>
      </div>

      {/* 本地任务 */}
      <div className="card"><div className="card-body">
        <div className="tiny dim mb8">{t('modeling.localTasksTitle', { n: data?.tasks.length ?? 0 })}</div>
        {!data || data.tasks.length === 0
          ? <div className="tiny muted">{t('modeling.noMatchTasks')}</div>
          : (
            <table className="tbl" style={{ fontSize: 12.5 }}>
              <thead><tr><th>{t('modeling.colTask')}</th><th>{t('modeling.targetTable')}</th><th>{t('modeling.colLayer')}</th><th>cron</th><th>{t('common.status')}</th><th>{t('modeling.colSource')}</th><th>Azkaban</th><th></th></tr></thead>
              <tbody>
                {data.tasks.map((row) => (
                  <tr key={row.id} style={{ cursor: 'pointer' }} onClick={() => onOpenTask(row.id)}>
                    <td>{row.name}</td>
                    <td><code>{row.target_table || '-'}</code></td>
                    <td>{row.layer ? <span className={`tag ${LAYER_CLS[row.layer] || ''}`}>{row.layer.toUpperCase()}</span> : '-'}</td>
                    <td className="muted">{row.cron || '-'}</td>
                    <td><StatusTag value={row.sched_enabled || row.status === 'pushed' ? 'connected' : 'untested'}
                      label={row.sched_enabled ? t('modeling.statusScheduled') : row.status === 'pushed' ? t('modeling.statusPushed') : t('modeling.statusDraft')} /></td>
                    <td className="muted">{row.source}</td>
                    <td>{row.azkaban_flow
                      ? <span className="tag ok tiny">{row.azkaban_project}/{row.azkaban_flow}</span>
                      : <span className="muted">-</span>}</td>
                    <td style={{ textAlign: 'right' }}>
                      <button className="btn ghost sm" onClick={(e) => { e.stopPropagation(); onOpenTask(row.id) }}>
                        {t('modeling.openBtn')}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </div></div>

      {/* 表元数据 */}
      <div className="card"><div className="card-body">
        <div className="tiny dim mb8">{t('modeling.tablesTitle', { n: data?.tables.length ?? 0 })}</div>
        {!data || data.tables.length === 0
          ? <div className="tiny muted">{t('modeling.noMatchTables')}</div>
          : (
            <table className="tbl" style={{ fontSize: 12.5 }}>
              <thead><tr><th>{t('modeling.colTableName')}</th><th>{t('modeling.colLayer')}</th><th>{t('modeling.colComment')}</th><th>{t('modeling.colPartition')}</th><th>{t('modeling.colColCount')}</th><th>{t('modeling.colRowCount')}</th></tr></thead>
              <tbody>
                {data.tables.map((row) => (
                  <tr key={row.name}>
                    <td><code>{row.name}</code></td>
                    <td><span className={`tag ${LAYER_CLS[row.layer] || ''}`}>{row.layer.toUpperCase()}</span></td>
                    <td className="muted">{row.comment || <span className="tag warn tiny">{t('modeling.noComment')}</span>}</td>
                    <td className="muted tiny">{row.partition_cols.join(', ') || '-'}</td>
                    <td>{row.col_count}</td>
                    <td className="muted">{row.row_count ?? '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </div></div>

      {/* Azkaban 远端工程（原 Azkaban 元数据） */}
      <div className="card"><div className="card-body">
        <div className="tiny dim mb8">{t('modeling.azkabanTitle')}</div>
        <AzkabanMetaPanel />
      </div></div>
    </div>
  )
}

/* ================= Azkaban 元数据 ================= */

function AzkabanMetaPanel() {
  const { t } = useI18n()
  const [meta, setMeta] = useState<AzkabanMetadata | null>(null)
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    setBusy(true)
    try { setMeta(await api.azkabanMetadata()) }
    catch (e) { toast.error((e as ApiError).message) }
    finally { setBusy(false) }
  }, [])
  useEffect(() => { void load() }, [load])

  const importFlow = async (project: string, flow: string, cron: string) => {
    try {
      await api.importAzkabanFlow(project, flow, cron)
      toast.ok(t('modeling.importedFlow', { flow }))
      await load()
    } catch (e) { toast.error((e as ApiError).message) }
  }

  if (busy && !meta) return <div className="center" style={{ padding: 40 }}><Spinner text={t('modeling.syncAzkaban')} /></div>
  if (!meta?.configured) {
    return <Empty icon="🔗" text={t('modeling.azkabanNotConfigured')} />
  }
  if (meta.error) return <Empty icon="⚠" text={t('modeling.azkabanSyncFailed', { error: meta.error })} />
  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="row">
        <button className="btn" disabled={busy} onClick={() => void load()}>↻ {t('modeling.resync')}</button>
        <span className="tiny muted" style={{ alignSelf: 'center' }}>
          {t('modeling.projectsCount', { n: meta.projects.length })}
        </span>
      </div>
      {meta.projects.map((p) => (
        <div key={p.name} className="card"><div className="card-body">
          <b>{p.name}</b>
          <table className="tbl mt8" style={{ fontSize: 12.5 }}>
            <thead><tr><th>Flow</th><th>{t('modeling.colCronSched')}</th><th>{t('modeling.colLastExec')}</th><th>{t('modeling.colLocalTask')}</th><th></th></tr></thead>
            <tbody>
              {p.flows.map((f) => (
                <tr key={f.flow}>
                  <td><code>{f.flow}</code></td>
                  <td className="muted">{f.cron || '-'}</td>
                  <td>{f.last_status
                    ? <StatusTag value={f.last_status === 'SUCCEEDED' ? 'success' : f.last_status === 'FAILED' ? 'failed' : 'running'}
                        label={f.last_status} />
                    : <span className="muted">-</span>}</td>
                  <td>{f.linked_task_id ? <span className="tag ok">{t('modeling.linked')}</span> : <span className="tag">{t('modeling.externalTask')}</span>}</td>
                  <td style={{ textAlign: 'right' }}>
                    {!f.linked_task_id && (
                      <button className="btn sm" onClick={() => void importFlow(p.name, f.flow, f.cron)}>
                        {t('modeling.importAsTask')}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {p.flows.length === 0 && <tr><td colSpan={5} className="muted tiny">{t('modeling.noFlow')}</td></tr>}
            </tbody>
          </table>
        </div></div>
      ))}
    </div>
  )
}
