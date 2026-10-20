/** AI 数据治理（PRD 3.3F）：六域巡检 + AI 治理报告 + 治理项转任务/修订 + 质量探查。 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError, tokenStore } from '@/api/client'
import { Markdown } from '@/components/common/Markdown'
import { Confirm, Drawer, Empty, relTime, Spinner } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import { useApp, toast } from '@/stores/app'
import type { GovernanceFinding, GovernanceRunRow } from '@/types'

const LEVEL_CLS: Record<string, string> = { block: 'danger', warn: 'warn', info: '' }
const DOMAIN_KEYS = new Set(['metadata', 'model', 'schedule', 'quality', 'security', 'cost'])
const LEVEL_KEYS = new Set(['block', 'warn', 'info'])

// ---- 探查留档（GET /governance/probes）：client.ts 暂无 getGovernanceProbes 类方法（归另一 agent），
// 这里按其 request 封装同风格用 fetch + tokenStore 直连 REST 路径；待 client 补齐后可迁移。
interface ProbeRow {
  id: string
  created_at: string
  datasource_id?: string
  datasource_name?: string
  table: string
  username?: string
  finding_count?: number
  summary?: string
}
interface ProbeDetail extends ProbeRow {
  stats: Record<string, { distinct: number; non_null: number }>
  findings: { level: string; column: string; issue: string; suggestion: string }[]
}
async function govGet<T>(path: string): Promise<T> {
  const headers: Record<string, string> = {}
  const token = tokenStore.get()
  if (token) headers.Authorization = `Bearer ${token}`
  const resp = await fetch(`/api/v1${path}`, { headers })
  const data = await resp.json().catch(() => ({})) as { message?: string; code?: string }
  if (!resp.ok) throw new ApiError(data.message || `请求失败 (${resp.status})`, data.code || 'HTTP_ERROR', resp.status)
  return data as unknown as T
}
const listGovernanceProbes = (page: number, pageSize: number) =>
  govGet<{ total: number; page: number; rows: ProbeRow[] }>(`/governance/probes?page=${page}&page_size=${pageSize}`)
const getGovernanceProbe = (id: string) => govGet<ProbeDetail>(`/governance/probes/${id}`)

export default function GovernancePage() {
  const { t, lang } = useI18n()
  const nav = useNavigate()
  const datasources = useApp((s) => s.datasources)
  const [runs, setRuns] = useState<GovernanceRunRow[]>([])
  const [active, setActive] = useState<(GovernanceRunRow & { findings: GovernanceFinding[] }) | null>(null)
  const [running, setRunning] = useState(false)
  const [domainFilter, setDomainFilter] = useState('')
  // 质量探查
  const [probeDs, setProbeDs] = useState('')
  const [probeTable, setProbeTable] = useState('')
  const [probeResult, setProbeResult] = useState<{
    stats: Record<string, { distinct: number; non_null: number }>;
    findings: { level: string; column: string; issue: string; suggestion: string }[]
  } | null>(null)
  const [probing, setProbing] = useState(false)
  const [gotoTask, setGotoTask] = useState<string | null>(null)
  // 探查留档
  const [historyOpen, setHistoryOpen] = useState(false)
  const [probeRows, setProbeRows] = useState<ProbeRow[]>([])
  const [probeTotal, setProbeTotal] = useState(0)
  const [probePage, setProbePage] = useState(1)
  const [probeDetail, setProbeDetail] = useState<ProbeDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const PROBE_PAGE_SIZE = 10

  const domainLabel = (d: string) => (DOMAIN_KEYS.has(d) ? t(`gov.domain.${d}`) : d)
  const domainShort = (d: string) => (DOMAIN_KEYS.has(d) ? t(`gov.domainShort.${d}`) : d.slice(0, 2))
  const levelLabel = (l: string) => (LEVEL_KEYS.has(l) ? t(`gov.level.${l}`) : l)

  const loadRuns = useCallback(async () => {
    try {
      const r = await api.listGovernanceRuns()
      setRuns(r.runs)
    } catch (e) { toast.error((e as ApiError).message) }
  }, [])
  useEffect(() => { void loadRuns() }, [loadRuns])

  const run = async () => {
    setRunning(true)
    try {
      const r = await api.runGovernance()
      setActive(r)
      toast.ok(t('gov.runDone', { n: r.finding_count }))
      void loadRuns()
    } catch (e) { toast.error((e as ApiError).message) } finally { setRunning(false) }
  }

  const openRun = async (id: string) => {
    try { setActive(await api.getGovernanceRun(id)) }
    catch (e) { toast.error((e as ApiError).message) }
  }

  const toTask = async (f: GovernanceFinding) => {
    try {
      const r = await api.governanceToTask({
        title: f.title, object: f.object, suggestion: f.suggestion,
      })
      toast.ok(t('gov.taskDraftCreated'))
      setGotoTask(r.task_id)
    } catch (e) { toast.error((e as ApiError).message) }
  }

  const probe = async () => {
    if (!probeDs || !probeTable.trim()) { toast.warn(t('gov.probeNeedInput')); return }
    setProbing(true)
    setProbeResult(null)
    try {
      const r = await api.governanceProbe({ datasource_id: probeDs, table: probeTable.trim() })
      setProbeResult(r)
      if (!r.findings.length) toast.ok(t('gov.probeClean'))
      // 新探查会落留档，历史面板展开时同步刷新第一页
      if (historyOpen) { setProbePage(1); void loadProbes(1) }
    } catch (e) { toast.error((e as ApiError).message) } finally { setProbing(false) }
  }

  // 探查留档：分页加载 + 详情查看
  const loadProbes = useCallback(async (page: number) => {
    try {
      const r = await listGovernanceProbes(page, PROBE_PAGE_SIZE)
      setProbeRows(r.rows || [])
      setProbeTotal(r.total || 0)
    } catch (e) { toast.error((e as ApiError).message) }
  }, [])
  useEffect(() => { if (historyOpen) void loadProbes(probePage) }, [historyOpen, probePage, loadProbes])

  const openProbe = async (id: string) => {
    setDetailLoading(true)
    setProbeDetail(null)
    try { setProbeDetail(await getGovernanceProbe(id)) }
    catch (e) { toast.error((e as ApiError).message) } finally { setDetailLoading(false) }
  }

  const dsName = (r: ProbeRow) =>
    r.datasource_name || datasources.find((d) => d.id === r.datasource_id)?.name || '-'

  const findings = (active?.findings || []).filter((f) => !domainFilter || f.domain === domainFilter)

  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row wrap">
        <h2 style={{ margin: 0 }}>{t('gov.title')}</h2>
        <span className="spacer" />
        <span className="tiny muted">{t('gov.subtitle')}</span>
      </div>

      <div className="row" style={{ alignItems: 'flex-start', gap: 16 }}>
        {/* 巡检历史 */}
        <div className="card" style={{ width: 240, flexShrink: 0 }}>
          <div className="card-body" style={{ padding: 10 }}>
            <button className="btn primary" style={{ width: '100%', marginBottom: 8 }}
              disabled={running} onClick={() => void run()}>
              {running ? t('gov.running') : t('gov.run')}
            </button>
            {runs.length === 0 && <Empty icon="🩺" text={t('gov.noRuns')} />}
            {runs.map((r) => (
              <div key={r.id} className={`conv ${active?.id === r.id ? 'active' : ''}`}
                style={{ padding: '8px 10px', cursor: 'pointer', borderRadius: 6 }}
                onClick={() => void openRun(r.id)}>
                <div className="t tiny">{relTime(r.created_at, t)}</div>
                <div className="d tiny muted">{t('gov.findingCount', { n: r.finding_count })}
                  {Object.entries(r.stats).slice(0, 3).map(([d, c]) => (
                    <span key={d} className="tag tiny" style={{ marginLeft: 4 }}>
                      {domainShort(d)}{c}</span>))}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* 结果区 */}
        <div style={{ flex: 1, minWidth: 0 }} className="col">
          {running && <Spinner text={t('gov.spinner')} />}
          {!active && !running && (
            <div className="card"><div className="card-body">
              <Empty icon="🩺" text={t('gov.emptyIntro')} />
            </div></div>
          )}

          {active && (
            <>
              {/* 域统计 + 过滤 */}
              <div className="row wrap" style={{ gap: 6 }}>
                <button className={`btn sm ${!domainFilter ? 'primary' : 'ghost'}`}
                  onClick={() => setDomainFilter('')}>{t('common.all')} {active.finding_count}</button>
                {Object.entries(active.stats).map(([d, c]) => (
                  <button key={d} className={`btn sm ${domainFilter === d ? 'primary' : 'ghost'}`}
                    onClick={() => setDomainFilter(domainFilter === d ? '' : d)}>
                    {domainLabel(d)} {c}
                  </button>
                ))}
              </div>

              {/* AI 治理报告 */}
              {active.report && (
                <div className="card"><div className="card-body">
                  <b>{t('gov.aiReport')}</b>
                  <Markdown text={active.report} />
                </div></div>
              )}

              {/* 治理项 */}
              {findings.map((f, i) => (
                <div key={i} className="card"><div className="card-body" style={{ padding: 12 }}>
                  <div className="row">
                    <span className={`tag ${LEVEL_CLS[f.level] || ''}`}>{levelLabel(f.level)}</span>
                    <span className="tag">{domainLabel(f.domain)}</span>
                    <b style={{ fontSize: 13.5 }}>{f.title}</b>
                    {f.object && <code className="tiny">{f.object}</code>}
                    <span className="spacer" />
                    {f.action === 'new_task' && (
                      <button className="btn sm primary" onClick={() => void toTask(f)}>{t('gov.toTask')}</button>
                    )}
                    {f.action === 'revise_task' && f.task_id && (
                      <button className="btn sm"
                        onClick={() => nav(`/modeling?task=${f.task_id}&revise=${encodeURIComponent(`${f.title}${lang === 'zh' ? '：' : ': '}${f.suggestion}`)}`)}>
                        {t('gov.goRevise')}</button>
                    )}
                  </div>
                  <div className="tiny muted mt8">{t('gov.evidence')}{f.evidence}</div>
                  <div className="tiny mt8" style={{ color: 'var(--accent, #4f7cff)' }}>💡 {f.suggestion}</div>
                </div></div>
              ))}
              {active && findings.length === 0 && (
                <div className="card"><div className="card-body">
                  <Empty icon="✅" text={domainFilter ? t('gov.noDomainFindings') : t('gov.noFindings')} />
                </div></div>
              )}
            </>
          )}
        </div>

        {/* 质量探查 + 探查留档 */}
        <div className="col" style={{ width: 320, flexShrink: 0, gap: 16 }}>
          <div className="card">
            <div className="card-body">
              <b>{t('gov.probeTitle')}</b>
              <div className="tiny muted mt8">{t('gov.probeDesc')}</div>
              <select className="input mt8" value={probeDs} onChange={(e) => setProbeDs(e.target.value)}>
                <option value="">{t('gov.probeSelectDs')}</option>
                {datasources.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
              </select>
              <input className="input mt8" placeholder={t('gov.probeTablePh')} value={probeTable}
                onChange={(e) => setProbeTable(e.target.value)} />
              <button className="btn primary mt8" style={{ width: '100%' }} disabled={probing}
                onClick={() => void probe()}>{probing ? t('gov.probing') : t('gov.probeRun')}</button>

              {probeResult && (
                <div className="mt12">
                  <ProbeResultView result={probeResult} levelLabel={levelLabel} />
                </div>
              )}
            </div>
          </div>

          {/* 探查留档（GET /governance/probes 分页列表，点击查看详情） */}
          <div className="card">
            <div className="card-body" style={{ padding: 12 }}>
              <div className="row" style={{ cursor: 'pointer' }} onClick={() => setHistoryOpen((v) => !v)}>
                <b>{t('gov.probeHistory')}</b>
                <span className="spacer" />
                <span className="tiny muted">{historyOpen ? '▾' : '▸'}</span>
              </div>
              {historyOpen && (
                <div className="mt8 col" style={{ gap: 4 }}>
                  {probeRows.map((r) => (
                    <div key={r.id} className="conv" style={{ padding: '6px 8px', cursor: 'pointer', borderRadius: 6 }}
                      onClick={() => void openProbe(r.id)}>
                      <div className="t tiny">{relTime(r.created_at, t)}
                        <span className="muted"> · {r.username || '-'}</span></div>
                      <div className="d tiny muted">{dsName(r)} · <code>{r.table}</code></div>
                      <div className="d tiny dim truncate">
                        {r.summary || (typeof r.finding_count === 'number' ? t('gov.findingCount', { n: r.finding_count }) : '')}
                      </div>
                    </div>
                  ))}
                  {!probeRows.length && <Empty icon="🧪" text={t('gov.noProbes')} />}
                  <div className="row tiny mt8" style={{ gap: 6 }}>
                    <button className="btn sm" disabled={probePage <= 1}
                      onClick={() => setProbePage((p) => p - 1)}>{t('common.prevPage')}</button>
                    <span className="muted">{t('common.page', { n: probePage })}</span>
                    <button className="btn sm" disabled={probePage * PROBE_PAGE_SIZE >= probeTotal}
                      onClick={() => setProbePage((p) => p + 1)}>{t('common.nextPage')}</button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
      {(detailLoading || probeDetail) && (
        <Drawer title={t('gov.probeDetailTitle')}
          sub={probeDetail ? `${dsName(probeDetail)} · ${probeDetail.table}` : ''}
          onClose={() => { setProbeDetail(null); setDetailLoading(false) }}>
          {detailLoading && <Spinner text={t('gov.probing')} />}
          {probeDetail && (
            <>
              <div className="tiny muted mb8">
                {relTime(probeDetail.created_at, t)} · {probeDetail.username || '-'}
              </div>
              <ProbeResultView result={probeDetail} levelLabel={levelLabel} />
            </>
          )}
        </Drawer>
      )}
      {gotoTask && (
        <Confirm title={t('gov.taskDraftCreated')}
          text={t('gov.gotoModeling')}
          onCancel={() => setGotoTask(null)}
          onOk={() => { const id = gotoTask; setGotoTask(null); nav(`/modeling?task=${id}`) }} />
      )}
    </div>
  )
}

/** 探查结果展示：在线探查结果与留档详情共用（stats/findings 渲染）。 */
function ProbeResultView({ result, levelLabel }: {
  result: {
    stats: Record<string, { distinct: number; non_null: number }>
    findings: { level: string; column: string; issue: string; suggestion: string }[]
  }
  levelLabel: (l: string) => string
}) {
  const { t } = useI18n()
  return (
    <div className="col" style={{ gap: 6 }}>
      {(result.findings || []).map((f, i) => (
        <div key={i} className="tiny">
          <span className={`tag tiny ${LEVEL_CLS[f.level] || ''}`}>{levelLabel(f.level)}</span>{' '}
          {f.issue}
          <div className="muted" style={{ marginLeft: 4 }}>💡 {f.suggestion}</div>
        </div>
      ))}
      {!result.findings?.length && <div className="tiny muted">{t('gov.probeClean')}</div>}
      <div className="tiny dim mt8">{t('gov.sampleStats')}</div>
      <table className="tbl" style={{ fontSize: 12 }}>
        <thead><tr><th>{t('gov.colField')}</th><th>{t('gov.colNonNull')}</th><th>{t('gov.colDistinct')}</th></tr></thead>
        <tbody>
          {Object.entries(result.stats || {}).map(([c, s]) => (
            <tr key={c}><td><code>{c}</code></td><td>{s.non_null}</td><td>{s.distinct}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
