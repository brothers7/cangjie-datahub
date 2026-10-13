/** 对话问答页：多轮会话 + SSE 实时过程 + 图表 + 报表。 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { api, ApiError } from '@/api/client'
import { payloadToView, streamAsk, type AskView } from '@/api/sse'
import { ChartRenderer } from '@/components/charts/ChartRenderer'
import { EtlPanel } from '@/components/chat/EtlPanel'
import { MetricCards } from '@/components/chat/MetricCards'
import { StepTimeline, type StepState } from '@/components/chat/StepTimeline'
import { SqlPanel } from '@/components/chat/SqlPanel'
import { Markdown } from '@/components/common/Markdown'
import { Empty, relTime, Spinner } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import { useApp, toast } from '@/stores/app'
import type { Conversation, EtlDesignResult, LlmChatOption, MessageRow, MetricCard, ReportDetail } from '@/types'
import { Drawer } from '@/components/common/ui'

interface LiveState {
  steps: StepState[]
  metrics: { id: string; name: string; confidence: number; why: string }[]
  dimensions: { id: string; name: string }[]
  timeRange: { start: string; end: string; label: string } | null
  note: string
  sql: string
  tables: string[]
  dialect: string
  guarded: boolean
  chartSpec: AskView['chartSpec']
  answer: string
  error: string
  reportId: string
  reportSections: { key: string; title: string; content_md: string }[]
  warnings: string[]
  cacheHit: boolean
  candidates: { id: string; name: string; confidence: number; why: string }[]
  etl: EtlDesignResult | null
  metricCards: MetricCard[]
  running: boolean
}

const emptyLive = (): LiveState => ({
  steps: [], metrics: [], dimensions: [], timeRange: null, note: '', sql: '',
  tables: [], dialect: '', guarded: false, chartSpec: null, answer: '', error: '',
  reportId: '', reportSections: [], warnings: [], cacheHit: false, candidates: [],
  etl: null, metricCards: [], running: false,
})

const SUGGESTION_KEYS = [
  'chat.suggest1', 'chat.suggest2', 'chat.suggest3', 'chat.suggest4',
  'chat.suggest5', 'chat.suggest6', 'chat.suggest7',
]

export default function ChatPage() {
  const { t } = useI18n()
  const { cid } = useParams<{ cid?: string }>()
  const nav = useNavigate()
  const datasources = useApp((s) => s.datasources)
  const loadDatasources = useApp((s) => s.loadDatasources)

  const [convs, setConvs] = useState<Conversation[]>([])
  const [messages, setMessages] = useState<MessageRow[]>([])
  const [live, setLive] = useState<LiveState | null>(null)
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [report, setReport] = useState<ReportDetail | null>(null)
  const [llmOptions, setLlmOptions] = useState<LlmChatOption[]>([])
  const abortRef = useRef<(() => void) | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  const activeConv = useMemo(() => convs.find((c) => c.id === cid) || null, [convs, cid])
  const busy = live?.running === true

  // ---- 数据加载 ----
  const loadConvs = useCallback(async () => {
    try {
      setConvs(await api.listConversations())
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }, [])

  const loadMessages = useCallback(async (id: string) => {
    setLoading(true)
    try {
      setMessages(await api.listMessages(id))
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void loadDatasources().catch(() => undefined) }, [loadDatasources])
  useEffect(() => { void loadConvs() }, [loadConvs])
  useEffect(() => {
    api.listLlmChatOptions().then(setLlmOptions).catch(() => setLlmOptions([]))
  }, [])
  useEffect(() => {
    abortRef.current?.()
    setLive(null)
    if (cid) void loadMessages(cid)
    else setMessages([])
  }, [cid, loadMessages])

  // 自动滚动
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, live?.answer, live?.steps.length])

  // ---- 新建 / 删除会话 ----
  const newConversation = async () => {
    if (busy) return
    const ds = datasources[0]
    const defaultLlm = llmOptions.find((o) => o.is_default) ?? llmOptions[0]
    try {
      const c = await api.createConversation(t('chat.newConversationTitle'), ds?.id ?? null, defaultLlm?.id ?? null)
      await loadConvs()
      nav(`/chat/${c.id}`)
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }

  const changeConversationLlm = async (llmConfigId: string) => {
    if (!cid) return
    try {
      await api.updateConversation(cid, { llm_config_id: llmConfigId || null })
      setConvs((prev) => prev.map((c) => c.id === cid ? { ...c, llm_config_id: llmConfigId || null } : c))
      const opt = llmOptions.find((o) => o.id === llmConfigId)
      toast.ok(opt ? t('chat.llmSwitched', { name: opt.name }) : t('chat.llmResetDefault'))
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }

  const removeConversation = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    if (busy) return
    if (!window.confirm(t('chat.confirmDeleteConv'))) return
    try {
      await api.deleteConversation(id)
      if (id === cid) nav('/chat')
      await loadConvs()
    } catch (err) {
      toast.error((err as ApiError).message)
    }
  }

  // ---- 发送 ----
  const send = (question: string, confirm?: Record<string, unknown> | null) => {
    const q = question.trim()
    if (!q || !cid) return
    if (busy) { toast.warn(t('chat.busyWarn')); return }

    setInput('')
    setMessages((prev) => [...prev, {
      id: `local-${Date.now()}`, role: 'user', content: q, payload: null,
      created_at: new Date().toISOString(),
    }])
    setLive({ ...emptyLive(), running: true })

    abortRef.current = streamAsk(cid, q, {
      onMeta: () => undefined,
      onStep: (d) => setLive((s) => {
        if (!s) return s
        const steps = [...s.steps]
        const i = steps.findIndex((x) => x.step === d.step && x.status === 'running')
        if (d.status === 'started') steps.push({ step: d.step, status: 'running' })
        else if (i >= 0) steps[i] = { ...steps[i], status: d.status as StepState['status'], detail: d.detail, ms: d.ms }
        else steps.push({ step: d.step, status: d.status as StepState['status'], detail: d.detail, ms: d.ms })
        return { ...s, steps }
      }),
      onKgMatch: (d) => setLive((s) => s && ({
        ...s, metrics: d.metrics, dimensions: d.dimensions, timeRange: d.time_range, note: d.note || '',
      })),
      onSql: (d) => setLive((s) => s && ({ ...s, sql: d.sql, tables: d.tables, dialect: d.dialect, guarded: d.guarded })),
      onToken: (t) => setLive((s) => s && ({ ...s, answer: s.answer + t })),
      onChart: (d) => setLive((s) => s && ({ ...s, chartSpec: d.chart_spec })),
      onReport: (d) => setLive((s) => s && ({ ...s, reportId: d.report_id, reportSections: d.sections })),
      onEtl: (d) => setLive((s) => s && ({ ...s, etl: d })),
      onMetricCards: (d) => setLive((s) => s && ({ ...s, metricCards: d.metrics })),
      onConfirmRequired: (d) => setLive((s) => s && ({ ...s, candidates: d.candidates })),
      onError: (d) => setLive((s) => s && ({ ...s, error: d.message })),
      onDone: async (d) => {
        setLive((s) => s && ({ ...s, running: false, error: d.error || s.error }))
        if (d.error) toast.error(d.error)
        // 拉取落库版本（含报表 ID、完整 payload）
        await loadMessages(cid)
        await loadConvs()
        setLive(null)
      },
    }, confirm)
  }

  const stop = async () => {
    abortRef.current?.()
    if (cid) { try { await api.cancelMessage(cid) } catch { /* ignore */ } }
    setLive((s) => s && ({ ...s, running: false, error: t('chat.stopped') }))
    if (cid) await loadMessages(cid)
    setLive(null)
  }

  const openReport = async (id: string) => {
    try { setReport(await api.getReport(id)) } catch (e) { toast.error((e as ApiError).message) }
  }

  return (
    <div className="chat">
      {/* 会话列表 */}
      <div className="chat-side">
        <div className="chat-side-head">
          <button className="btn primary" style={{ width: '100%' }} onClick={() => void newConversation()}>
            {t('chat.newConversation')}
          </button>
        </div>
        <div className="conv-list">
          {convs.length === 0 && <div className="empty tiny" style={{ padding: 20 }}>{t('chat.noConvs')}</div>}
          {convs.map((c) => (
            <div key={c.id} className={`conv ${c.id === cid ? 'active' : ''}`}
              onClick={() => nav(`/chat/${c.id}`)}>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="t truncate">{c.title || t('chat.unnamedConv')}</div>
                <div className="d">{t('chat.msgCount', { n: c.message_count })} · {relTime(c.last_active_at || c.created_at, t)}</div>
              </div>
              <button className="btn ghost sm" title={t('common.delete')}
                onClick={(e) => void removeConversation(c.id, e)}>✕</button>
            </div>
          ))}
        </div>
      </div>

      {/* 主区 */}
      <div className="chat-main">
        {cid && activeConv && (
          <div className="row" style={{
            padding: '8px 16px', gap: 16, borderBottom: '1px solid var(--border)',
            alignItems: 'center', flexShrink: 0,
          }}>
            <span className="tiny muted">{t('chat.datasource')}</span>
            <span style={{ fontSize: 13 }}>
              {datasources.find((d) => d.id === activeConv.datasource_id)?.name || t('chat.defaultDatasource')}
            </span>
            <span style={{ width: 1, height: 16, background: 'var(--border)' }} />
            <span className="tiny muted">{t('chat.model')}</span>
            <select className="select" style={{ minWidth: 180, padding: '4px 8px', fontSize: 12.5 }}
              value={activeConv.llm_config_id ?? ''}
              disabled={busy}
              onChange={(e) => void changeConversationLlm(e.target.value)}>
              <option value="">{t('chat.defaultModel')}</option>
              {llmOptions.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}（{o.model}）{o.health_status === 'healthy' ? '' : ` · ${t('common.untested')}`}
                </option>
              ))}
            </select>
            {llmOptions.length === 0 && (
              <span className="tiny muted">{t('chat.noLlmConfigured')}</span>
            )}
          </div>
        )}
        <div className="msg-scroll" ref={scrollRef}>
          <div className="msg-wrap">
            {!cid && (
              <Empty icon="💬" text={t('chat.emptySelect')} />
            )}
            {cid && loading && messages.length === 0 && (
              <div className="center" style={{ padding: 40 }}><Spinner text={t('chat.loadingHistory')} /></div>
            )}

            {messages.map((m) => (
              <MessageBubble key={m.id} msg={m} onOpenReport={(id) => void openReport(id)}
                onConfirm={(q, mid) => send(q, { metric_id: mid })} />
            ))}

            {live && (
              <div className="msg assistant">
                <div className="bubble">
                  <StepTimeline steps={live.steps} live />
                  {(live.metrics.length > 0 || live.timeRange) && (
                    <div className="chips">
                      {live.timeRange && <span className="tag accent">{live.timeRange.label}</span>}
                      {live.metrics.slice(0, 5).map((m) => (
                        <span className="tag" key={m.id} title={m.why}>
                          {m.name} {(m.confidence * 100).toFixed(0)}%
                        </span>
                      ))}
                      {live.dimensions.map((d) => <span className="tag" key={d.id}>{t('chat.byDimension', { name: d.name })}</span>)}
                    </div>
                  )}
                  {live.metricCards.length > 0 && <MetricCards cards={live.metricCards} />}
                  {live.sql && <SqlPanel sql={live.sql} tables={live.tables} dialect={live.dialect} guarded={live.guarded} />}
                  {live.etl && <EtlPanel design={live.etl} conversationId={cid}
                    datasourceId={activeConv?.datasource_id} />}
                  {live.chartSpec && <div className="mt12"><ChartRenderer spec={live.chartSpec} /></div>}
                  {live.candidates.length > 0 && (
                    <ConfirmPrompt candidates={live.candidates}
                      onPick={(id, name) => send(name, { metric_id: id })} />
                  )}
                  {live.answer && <div className="mt12"><Markdown text={live.answer} /></div>}
                  {live.running && !live.answer && <Spinner text={t('chat.analyzing')} />}
                  {live.error && <div className="tag danger mt8">{live.error}</div>}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* 输入区 */}
        <div className="composer">
          {messages.length === 0 && !live && (
            <div className="suggest">
              {SUGGESTION_KEYS.map((k) => {
                const s = t(k)
                return <button key={k} onClick={() => (cid ? send(s) : void newConversation())}>{s}</button>
              })}
            </div>
          )}
          <div className="composer-inner">
            <textarea
              value={input}
              placeholder={cid ? t('chat.placeholder') : t('chat.placeholderNoConv')}
              disabled={!cid}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(input) }
              }}
            />
            {busy
              ? <button className="btn danger" onClick={() => void stop()}>{t('common.stop')}</button>
              : <button className="btn primary" disabled={!cid || !input.trim()} onClick={() => send(input)}>{t('chat.send')}</button>}
          </div>
        </div>
      </div>

      {report && (
        <Drawer title={report.title} sub={`${t('chat.reportViews', { n: report.view_count })} · ${relTime(report.created_at, t)}`}
          onClose={() => setReport(null)}>
          <Markdown className="report-md" text={report.content_md} />
        </Drawer>
      )}
    </div>
  )
}

function ConfirmPrompt({ candidates, onPick }: {
  candidates: { id: string; name: string; confidence: number; why: string }[]
  onPick: (id: string, name: string) => void
}) {
  const { t } = useI18n()
  return (
    <div className="card" style={{ marginTop: 10, borderColor: 'var(--warn)' }}>
      <div className="card-body">
        <div style={{ fontWeight: 600, marginBottom: 6 }}>{t('chat.confirmTitle')}</div>
        <div className="chips">
          {candidates.map((c) => (
            <button key={c.id} className="btn sm" onClick={() => onPick(c.id, c.name)}>
              {c.name} <span className="muted tiny">{(c.confidence * 100).toFixed(0)}%</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}

function MessageBubble({ msg, onOpenReport, onConfirm }: {
  msg: MessageRow
  onOpenReport: (id: string) => void
  onConfirm: (q: string, metricId: string) => void
}) {
  const { t, lang } = useI18n()
  const view = payloadToView(msg.payload)

  if (msg.role === 'user') {
    return <div className="msg user"><div className="bubble">{msg.content}</div></div>
  }

  if (!view) {
    return (
      <div className="msg assistant">
        <div className="bubble">{msg.content || <span className="muted">{t('chat.noContent')}</span>}</div>
      </div>
    )
  }

  const anomalies = (view.anomalies as { points?: unknown[] })?.points?.length || 0

  return (
    <div className="msg assistant">
      <div className="bubble">
        <StepTimeline steps={view.steps.map((s) => ({
          step: s.step, status: s.status as StepState['status'], detail: s.detail, ms: s.ms,
        }))} />

        {(view.metrics.length > 0 || view.timeRange) && (
          <div className="chips">
            {view.timeRange && <span className="tag accent">{view.timeRange.label}</span>}
            {view.metrics.slice(0, 6).map((m) => <span className="tag" key={m.id}>{m.name}</span>)}
            {view.dimensions.map((d) => <span className="tag" key={d.id}>{t('chat.byDimension', { name: d.name })}</span>)}
            {view.cacheHit && <span className="tag ok">{t('chat.cacheHit')}</span>}
            {anomalies > 0 && <span className="tag danger">{t('chat.anomalyCount', { n: anomalies })}</span>}
          </div>
        )}

        {view.metricCards.length > 0 && <MetricCards cards={view.metricCards} />}
        {view.sql && <SqlPanel sql={view.sql} />}
        {view.etl && <EtlPanel design={view.etl} />}
        {view.chartSpec && <div className="mt12"><ChartRenderer spec={view.chartSpec} /></div>}

        {view.webEvidences.length > 0 && (
          <div className="mt12 card">
            <div className="card-body">
              <div className="tiny dim mb8">{t('chat.webEvidence')}</div>
              {view.webEvidences.slice(0, 5).map((w, i) => (
                <div key={i} style={{ marginBottom: 6 }}>
                  <a href={w.url} target="_blank" rel="noreferrer" style={{ fontSize: 13 }}>{w.title}</a>
                  <div className="tiny muted">{w.snippet?.slice(0, 160)}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        {view.warnings.length > 0 && (
          <div className="tag warn mt8" style={{ display: 'block', whiteSpace: 'normal' }}>
            {t('chat.dataWarning')}{view.warnings.join(lang === 'zh' ? '；' : '; ')}
          </div>
        )}

        {view.error && <div className="tag danger mt8" style={{ display: 'block', whiteSpace: 'normal' }}>{view.error}</div>}

        {view.pendingConfirm && view.pendingConfirm.length > 0 && (
          <ConfirmPrompt candidates={view.pendingConfirm}
            onPick={(id, name) => {
              const q = msg.payload?.rewritten || ''
              if (!q.trim()) { toast.warn(t('chat.confirmExpired')); return }
              onConfirm(q, id)
            }} />
        )}

        {msg.content && <div className="mt12"><Markdown text={msg.content} /></div>}

        {view.report && (
          <div className="row mt12">
            <button className="btn" onClick={() => onOpenReport(view.report!.id)}>
              {t('chat.viewReport')}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
