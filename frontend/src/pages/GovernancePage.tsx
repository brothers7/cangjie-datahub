/** AI 数据治理（PRD 3.3F）：六域巡检 + AI 治理报告 + 治理项转任务/修订 + 质量探查。 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError } from '@/api/client'
import { Markdown } from '@/components/common/Markdown'
import { Confirm, Empty, relTime, Spinner } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import { useApp, toast } from '@/stores/app'
import type { GovernanceFinding, GovernanceRunRow } from '@/types'

const LEVEL_CLS: Record<string, string> = { block: 'danger', warn: 'warn', info: '' }
const DOMAIN_KEYS = new Set(['metadata', 'model', 'schedule', 'quality', 'security', 'cost'])
const LEVEL_KEYS = new Set(['block', 'warn', 'info'])

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
    } catch (e) { toast.error((e as ApiError).message) } finally { setProbing(false) }
  }

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

        {/* 质量探查 */}
        <div className="card" style={{ width: 320, flexShrink: 0 }}>
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
              <div className="mt12 col" style={{ gap: 6 }}>
                {probeResult.findings.map((f, i) => (
                  <div key={i} className="tiny">
                    <span className={`tag tiny ${LEVEL_CLS[f.level] || ''}`}>{LEVEL_KEYS.has(f.level) ? t(`gov.level.${f.level}`) : f.level}</span>{' '}
                    {f.issue}
                    <div className="muted" style={{ marginLeft: 4 }}>💡 {f.suggestion}</div>
                  </div>
                ))}
                <div className="tiny dim mt8">{t('gov.sampleStats')}</div>
                <table className="tbl" style={{ fontSize: 12 }}>
                  <thead><tr><th>{t('gov.colField')}</th><th>{t('gov.colNonNull')}</th><th>{t('gov.colDistinct')}</th></tr></thead>
                  <tbody>
                    {Object.entries(probeResult.stats).map(([c, s]) => (
                      <tr key={c}><td><code>{c}</code></td><td>{s.non_null}</td><td>{s.distinct}</td></tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </div>
      {gotoTask && (
        <Confirm title={t('gov.taskDraftCreated')}
          text={t('gov.gotoModeling')}
          onCancel={() => setGotoTask(null)}
          onOk={() => { const id = gotoTask; setGotoTask(null); nav(`/modeling?task=${id}`) }} />
      )}
    </div>
  )
}
