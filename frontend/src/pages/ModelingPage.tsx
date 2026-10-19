/** 自动化建模模块（PRD 3.3D）：建模任务（SQL 编辑器 + AI 修订）/ AI 建模建议会话 / 数仓全景 / 任务元数据（任务+表统一搜索，Azkaban 远端工程）。 */
import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, ApiError } from '@/api/client'
import { Markdown } from '@/components/common/Markdown'
import { Empty, Spinner, StatusTag, Tabs } from '@/components/common/ui'
import { StandardsPanel } from '@/components/modeling/StandardsPanel'
import { AiModelingPanel } from '@/components/modeling/AiModelingPanel'
import { LAYER_CLS, runStatusLabel, TasksPanel } from '@/components/modeling/TasksPanel'
import { toast } from '@/stores/app'
import { useI18n } from '@/i18n'
import type { AzkabanMetadata, ModelingAdvice, ModelingOverview, SchedulerDag, SchedulerRunRow, TaskMetaResult } from '@/types'

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
