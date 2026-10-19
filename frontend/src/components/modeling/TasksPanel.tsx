/** 建模任务面板（PRD 3.3D）：任务列表 + SQL 编辑器 + AI 修订 + 调度推送（从 ModelingPage 拆分）。 */
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, ApiError } from '@/api/client'
import { streamSse } from '@/api/sse'
import { StepTimeline, type StepState } from '@/components/chat/StepTimeline'
import { Confirm, Empty, Field, fmtTime, relTime, Spinner, StatusTag } from '@/components/common/ui'
import { DiffView } from '@/components/modeling/DiffView'
import { SqlEditor } from '@/components/modeling/SqlEditor'
import { toast } from '@/stores/app'
import { useAuth } from '@/stores/auth'
import { useI18n } from '@/i18n'
import type { CompletionData, EtlTaskRow, SchedulerPlatform, SchedulerRunRow, TaskRevision } from '@/types'

/** 数仓分层标签配色（本模块多处复用，故导出） */
export const LAYER_CLS: Record<string, string> = { ods: '', dwd: '', dws: 'accent', ads: 'ok', dim: 'warn' }

type TFunc = (key: string, vars?: Record<string, string | number>) => string

/** 运行实例状态文案（模块级中文常量改为随语言计算的函数，见 i18n 规则 5） */
export const runStatusLabel = (t: TFunc): Record<string, string> => ({
  waiting: t('modeling.runStatusWaiting'), running: t('modeling.runStatusRunning'),
  success: t('common.success'), failed: t('common.failed'),
  blocked: t('modeling.runStatusBlocked'), dep_timeout: t('modeling.runStatusDepTimeout'),
})

/** 待确认动作（自研 Confirm 替换 window.confirm）：确认后执行 action */
interface ConfirmReq {
  title: string
  text: ReactNode
  danger?: boolean
  okText?: string
  action: () => void
}

export function TasksPanel() {
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
  // 自研确认弹窗（替换 window.confirm）
  const [confirm, setConfirm] = useState<ConfirmReq | null>(null)
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

  /** 含换行的确认文案：Confirm 正文是 div，需 pre-line 才能保留 \n */
  const preline = (text: string) => <span style={{ whiteSpace: 'pre-line' }}>{text}</span>

  /** 未保存草稿拦截：切换任务/新建前先确认放弃修改 */
  const guardDirty = (action: () => void) => {
    if (dirty) {
      setConfirm({
        title: t('modeling.discardDraftTitle'),
        text: t('modeling.discardDraftText'),
        danger: true,
        okText: t('modeling.discardDraftOk'),
        action,
      })
      return
    }
    action()
  }

  const doSelect = (t: EtlTaskRow) => {
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

  const select = (row: EtlTaskRow) => guardDirty(() => doSelect(row))

  const doNewTask = async () => {
    try {
      const row = await api.createEtlTask({ name: t('modeling.newTaskDefaultName'), source: 'manual' })
      await load(row.id)
      toast.ok(t('modeling.taskDraftCreated'))
    } catch (e) { toast.error((e as ApiError).message) }
  }

  const newTask = () => guardDirty(() => void doNewTask())

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

  const push = () => {
    if (!draft) return
    const p = schedulers.find((x) => x.key === platform)
    if (p && (p.kind === 'local' || p.key === 'local')) { enableLocal(); return }
    if (p && (p.kind === 'builtin' && p.key === 'airflow')) { exportAirflow(); return }
    if (p && p.kind === 'scheduler_skill') { exportPackage(p); return }
    setConfirm({
      title: t('modeling.confirmTitle'),
      text: preline(t('modeling.confirmPushAzkaban', { name: draft.name })),
      action: () => void doPush(),
    })
  }

  const doPush = async () => {
    if (!draft) return
    if (!(await save())) return
    setBusy('push')
    try {
      const r = await api.pushEtlTask(draft.id, draft.cron)
      if (r.ok) { toast.ok(r.message); await load(draft.id) }
      else toast.warn(r.message)
    } catch (e) { toast.error((e as ApiError).message) } finally { setBusy('') }
  }

  const exportPackage = (p: SchedulerPlatform) => {
    if (!draft) return
    setConfirm({
      title: t('modeling.confirmTitle'),
      text: preline(t('modeling.confirmExportPackage', { name: p.name })),
      action: () => void doExportPackage(p),
    })
  }

  const doExportPackage = async (p: SchedulerPlatform) => {
    if (!draft) return
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
  const enableLocal = () => {
    if (!draft) return
    if (!draft.cron.trim()) { toast.warn(t('modeling.cronRequired')); return }
    setConfirm({
      title: t('modeling.confirmTitle'),
      text: preline(t('modeling.confirmEnableLocal')),
      action: () => void doEnableLocal(),
    })
  }

  const doEnableLocal = async () => {
    if (!draft) return
    if (!(await save())) return
    try {
      const row = await api.updateEtlTask(draft.id, { sched_enabled: true })
      setTasks((prev) => prev.map((x) => (x.id === row.id ? row : x)))
      setDraft({ ...row })
      toast.ok(t('modeling.localEnabled'))
    } catch (e) { toast.error((e as ApiError).message) }
  }

  /** Airflow 内置模板导出 DAG（PRD 3.3G） */
  const exportAirflow = () => {
    if (!draft) return
    setConfirm({
      title: t('modeling.confirmTitle'),
      text: preline(t('modeling.confirmExportAirflow')),
      action: () => void doExportAirflow(),
    })
  }

  const doExportAirflow = async () => {
    if (!draft) return
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

  const approve = () => {
    if (!draft) return
    setConfirm({
      title: t('modeling.confirmTitle'),
      text: preline(t('modeling.confirmApprove', { name: draft.name })),
      action: () => void doApprove(),
    })
  }

  const doApprove = async () => {
    if (!draft) return
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

  const rollback = (v: number) => {
    if (!draft) return
    setConfirm({
      title: t('modeling.confirmTitle'),
      text: t('modeling.confirmRollback', { v }),
      action: () => void doRollback(v),
    })
  }

  const doRollback = async (v: number) => {
    if (!draft) return
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

  const remove = () => {
    if (!draft) return
    setConfirm({
      title: t('modeling.confirmTitle'),
      text: t('modeling.confirmDeleteTask', { name: draft.name }),
      danger: true,
      okText: t('common.delete'),
      action: () => void doRemove(),
    })
  }

  const doRemove = async () => {
    if (!draft) return
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
          <button className="btn primary" style={{ width: '100%', marginBottom: 8 }} onClick={newTask}>
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
                    <button className="btn sm" onClick={approve}>✔ {t('modeling.approveBtn')}</button>
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
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); revise() } }} />
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
              <button className="btn" disabled={busy === 'push' || !draft.etl_sql} onClick={push}>
                {busy === 'push' ? t('modeling.processingBtn')
                  : platform === 'local' ? `⏱ ${t('modeling.enableSchedBtn')}`
                  : platform === 'airflow' ? `📦 ${t('modeling.exportDagBtn')}`
                  : platform === 'azkaban' ? `🚀 ${t('modeling.pushBtn')}` : `📦 ${t('modeling.exportPkgBtn')}`}
              </button>
              <button className="btn ghost" onClick={() => void loadVersions()}>{t('modeling.versionsBtn')}</button>
              <span className="spacer" />
              <button className="btn danger sm" onClick={remove}>{t('modeling.deleteTask')}</button>
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
                    <button className="btn ghost sm" onClick={() => rollback(v.version)}>{t('modeling.rollbackTo')}</button>
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

      {/* 自研确认弹窗（替换 window.confirm） */}
      {confirm && (
        <Confirm title={confirm.title} text={confirm.text} danger={confirm.danger} okText={confirm.okText}
          onCancel={() => setConfirm(null)}
          onOk={() => { const a = confirm.action; setConfirm(null); a() }} />
      )}
    </div>
  )
}
