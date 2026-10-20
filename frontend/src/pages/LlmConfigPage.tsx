/** LLM 配置：多模型接入（OpenAI / Anthropic）、健康检查、默认模型切换、场景绑定、用量与调用日志。 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '@/api/client'
import { CATEGORICAL, Chart, themeColors, useThemeTick } from '@/components/charts/Chart'
import { Confirm, Empty, Field, Modal, Spinner, StatusTag, Tabs } from '@/components/common/ui'
import { LlmLogsTable } from '@/components/logs/LlmLogsTable'
import { useI18n } from '@/i18n'
import { sceneLabels } from '@/labels'
import { toast } from '@/stores/app'
import type { LlmCallLog, LlmConfig, SceneBindings, UsageRow } from '@/types'

/** 健康检查 / 连通性测试结果（文案按当前语言渲染，避免依赖中文字符串判断状态） */
interface HealthState { ok: boolean; checking?: boolean; latency?: number | null; message?: string }

/** 近 15 分钟失败率告警徽标：字段缺失、无调用或低于阈值（20%）时不显示 */
function FailRateBadge({ config }: { config: LlmConfig }) {
  const { t } = useI18n()
  const ext = config as LlmConfig & { failure_rate_15m?: number | null; calls_15m?: number | null }
  const rate = ext.failure_rate_15m
  if (rate === undefined || rate === null || rate <= 0.2) return null
  if (ext.calls_15m === 0) return null
  const pct = (rate * 100).toFixed(0)
  return (
    <span className="tag warn" title={t('admin.llm.failRateTitle', { n: pct })}>
      ⚠ {t('admin.llm.failRateBadge', { n: pct })}
    </span>
  )
}

const PRESETS: Record<string, { endpoint: string; model: string }[]> = {
  openai: [
    { endpoint: 'https://api.openai.com/v1', model: 'gpt-4o-mini' },
    { endpoint: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' },
    { endpoint: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
    { endpoint: 'http://127.0.0.1:11434/v1', model: 'qwen2.5:14b' },
  ],
  anthropic: [
    { endpoint: 'https://api.anthropic.com/v1', model: 'claude-sonnet-5-5' },
    { endpoint: 'https://api.anthropic.com/v1', model: 'claude-haiku-5-5' },
  ],
}

type Tab = 'configs' | 'scenes' | 'usage' | 'logs'

export default function LlmConfigPage() {
  const { t } = useI18n()
  const [tab, setTab] = useState<Tab>('configs')
  const [configs, setConfigs] = useState<LlmConfig[]>([])
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState<LlmConfig | 'new' | null>(null)
  const [confirmDel, setConfirmDel] = useState<LlmConfig | null>(null)
  const [health, setHealth] = useState<Record<string, HealthState>>({})

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setConfigs(await api.listLlmConfigs())
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const checkHealth = async (c: LlmConfig) => {
    setHealth((h) => ({ ...h, [c.id]: { ok: false, checking: true } }))
    try {
      const r = await api.healthCheckLlm(c.id)
      setHealth((h) => ({ ...h, [c.id]: { ok: r.ok, latency: r.latency_ms, message: r.message } }))
      void load()
    } catch (e) {
      setHealth((h) => ({ ...h, [c.id]: { ok: false, message: (e as ApiError).message } }))
    }
  }

  const activate = async (c: LlmConfig) => {
    try {
      const r = await api.activateLlm(c.id, c.kind === 'embedding')
      toast.ok(r.switch_policy === 'new_session'
        ? t('admin.llm.activatedNewSession')
        : t('admin.llm.activatedImmediate'))
      void load()
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }

  const chatConfigs = configs.filter((c) => c.kind === 'chat')
  const embedConfigs = configs.filter((c) => c.kind === 'embedding')

  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row wrap">
        <h1>{t('admin.llm.title')}</h1>
        <span className="spacer" />
        <button className="btn" onClick={() => void load()}>{t('common.refresh')}</button>
        <button className="btn primary" onClick={() => setEditing('new')}>＋ {t('admin.llm.addModel')}</button>
      </div>

      <Tabs value={tab} onChange={setTab} items={[
        { key: 'configs', label: t('admin.llm.tabConfigs'), badge: configs.length },
        { key: 'scenes', label: t('admin.llm.tabScenes') },
        { key: 'usage', label: t('admin.llm.tabUsage') },
        { key: 'logs', label: t('admin.llm.tabLogs') },
      ]} />

      {loading && <div className="center" style={{ padding: 40 }}><Spinner text={t('admin.llm.loadingConfigs')} /></div>}

      {!loading && tab === 'configs' && (
        <div className="col" style={{ gap: 12 }}>
          {!configs.length && (
            <div className="card"><Empty icon="🔌" text={t('admin.llm.empty')} /></div>
          )}
          {([t('admin.llm.groupChat'), t('admin.llm.groupEmbed')] as const).map((group, gi) => {
            const list = gi === 0 ? chatConfigs : embedConfigs
            if (!list.length) return null
            return (
              <div className="card" key={group}>
                <div className="card-head"><h2>{group}</h2><span className="spacer" />
                  <span className="tiny muted">{t('admin.llm.count', { n: list.length })}</span></div>
                <div className="card-body col" style={{ gap: 12 }}>
                  {list.map((c) => (
                    <div key={c.id} className="list-row" style={{ alignItems: 'flex-start' }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div className="row wrap" style={{ gap: 8 }}>
                          <span style={{ fontWeight: 600 }}>{c.name}</span>
                          <span className="tag">{c.protocol}</span>
                          <StatusTag value={c.status} />
                          {(c.is_default || c.is_default_embedding) && <span className="tag ok">{t('admin.llm.defaultTag')}</span>}
                          {c.fallback_config_id && (
                            <span className="tag warn">
                              {t('admin.llm.fallbackTag', { name: configs.find((x) => x.id === c.fallback_config_id)?.name || t('admin.llm.deletedName') })}
                            </span>
                          )}
                          <FailRateBadge config={c} />
                        </div>
                        <div className="tiny muted mt8">
                          {c.endpoint} · <span className="mono">{c.model}</span> · temp {c.temperature} ·
                          max_tokens {c.max_tokens} · {t('admin.llm.metaTimeout', { n: c.timeout_sec })} · {t('admin.llm.metaRetry', { n: c.retry_count })}
                          {c.stream_enabled ? ` · ${t('admin.llm.streaming')}` : ` · ${t('admin.llm.nonStreaming')}`}
                        </div>
                        {c.status_message && <div className="tiny" style={{ color: 'var(--danger)' }}>{c.status_message}</div>}
                        {health[c.id] && (
                          <div className="tiny mt8" style={{
                            color: health[c.id].ok ? 'var(--ok)' : 'var(--danger)',
                          }}>
                            {health[c.id].checking
                              ? t('admin.llm.checking')
                              : health[c.id].ok
                                ? t('admin.llm.healthOk', { n: health[c.id].latency ?? '-' })
                                : t('admin.llm.healthFail', { msg: health[c.id].message ?? '' })}
                          </div>
                        )}
                      </div>
                      <div className="row" style={{ gap: 6, flexShrink: 0 }}>
                        <button className="btn sm" onClick={() => void checkHealth(c)}>{t('admin.llm.healthCheck')}</button>
                        {!(c.kind === 'embedding' ? c.is_default_embedding : c.is_default) && (
                          <button className="btn sm primary" onClick={() => void activate(c)}>
                            {t('admin.llm.setDefault')}
                          </button>
                        )}
                        <button className="btn sm" onClick={() => setEditing(c)}>{t('common.edit')}</button>
                        <button className="btn sm danger" onClick={() => setConfirmDel(c)}>{t('common.delete')}</button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )
          })}

          {!!configs.length && !embedConfigs.length && (
            <div className="tiny muted">
              {t('admin.llm.embedTip')}
            </div>
          )}
        </div>
      )}

      {!loading && tab === 'scenes' && <ScenePanel configs={configs} />}
      {!loading && tab === 'usage' && <UsagePanel />}
      {!loading && tab === 'logs' && <LogsPanel />}

      {editing && (
        <ConfigEditor config={editing === 'new' ? null : editing} all={configs}
          onClose={() => setEditing(null)} onSaved={() => { setEditing(null); void load() }} />
      )}

      {confirmDel && (
        <Confirm title={t('admin.llm.confirmDeleteTitle')} danger okText={t('common.delete')}
          text={t('admin.llm.confirmDelete', { name: confirmDel.name })}
          onCancel={() => setConfirmDel(null)}
          onOk={async () => {
            try {
              await api.deleteLlmConfig(confirmDel.id)
              toast.ok(t('common.deleted'))
              setConfirmDel(null)
              void load()
            } catch (e) { toast.error((e as ApiError).message) }
          }} />
      )}
    </div>
  )
}

// ---------------- 场景绑定 ----------------

function ScenePanel({ configs }: { configs: LlmConfig[] }) {
  const { t, lang } = useI18n()
  const scenes = sceneLabels(lang)
  const [bindings, setBindings] = useState<SceneBindings | null>(null)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.getSceneBindings().then((b) => {
      setBindings(b)
      setDraft(Object.fromEntries(b.bindings.map((x) => [x.scene, x.config_id])))
    }).catch((e: ApiError) => toast.error(e.message))
  }, [])

  const chatConfigs = configs.filter((c) => c.kind === 'chat')

  const save = async () => {
    setBusy(true)
    try {
      const payload = Object.entries(draft).filter(([, v]) => v).map(([scene, config_id]) => ({ scene, config_id }))
      await api.setSceneBindings(payload)
      toast.ok(t('admin.llm.sceneSaved'))
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
    }
  }

  if (!bindings) return <div className="center" style={{ padding: 40 }}><Spinner text={t('admin.llm.loadingScenes')} /></div>

  return (
    <div className="card">
      <div className="card-head">
        <h2>{t('admin.llm.sceneTitle')}</h2>
        <span className="spacer" />
        <span className="tiny muted">{t('admin.llm.sceneHint')}</span>
        <button className="btn primary sm" disabled={busy} onClick={() => void save()}>{t('common.save')}</button>
      </div>
      <div className="card-body col" style={{ gap: 10 }}>
        {bindings.scenes.map((s) => (
          <div key={s} className="row" style={{ gap: 12 }}>
            <span style={{ width: 120, fontSize: 13 }}>{scenes[s] || s}</span>
            <span className="tiny muted mono" style={{ width: 110 }}>{s}</span>
            <select className="select" style={{ flex: 1, maxWidth: 360 }} value={draft[s] || ''}
              onChange={(e) => setDraft({ ...draft, [s]: e.target.value })}>
              <option value="">{t('admin.llm.useDefaultOpt')}</option>
              {chatConfigs.map((c) => <option key={c.id} value={c.id}>{c.name} · {c.model}</option>)}
            </select>
          </div>
        ))}
      </div>
    </div>
  )
}

// ---------------- 用量 ----------------

function UsagePanel() {
  const { t, lang } = useI18n()
  const scenes = sceneLabels(lang)
  const [groupBy, setGroupBy] = useState<'day' | 'scene' | 'model'>('day')
  const [days, setDays] = useState(14)
  const [rows, setRows] = useState<UsageRow[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    api.llmUsage(groupBy, days).then((r) => setRows(r.rows))
      .catch((e: ApiError) => toast.error(e.message))
      .finally(() => setLoading(false))
  }, [groupBy, days])

  const totals = useMemo(() => rows.reduce(
    (a, r) => ({ input: a.input + r.input_tokens, output: a.output + r.output_tokens, calls: a.calls + r.calls, errors: a.errors + r.errors }),
    { input: 0, output: 0, calls: 0, errors: 0 },
  ), [rows])

  // 主题 CSS 变量随系统亮/暗切换：tick 变化时重建 option（与 ChartRenderer 风格统一）
  const themeTick = useThemeTick()
  const option = useMemo(() => {
    const c = themeColors()
    return {
      color: [CATEGORICAL[0], CATEGORICAL[1]],
      textStyle: { color: c.dim, fontSize: 12 },
      tooltip: {
        trigger: 'axis' as const,
        backgroundColor: c.elev,
        borderColor: c.border,
        textStyle: { color: c.text, fontSize: 12 },
        axisPointer: { type: 'shadow' as const },
      },
      legend: {
        data: [t('admin.llm.inputTokens'), t('admin.llm.outputTokens')],
        top: 0, textStyle: { color: c.dim, fontSize: 11 }, itemHeight: 8, itemWidth: 14,
      },
      grid: { left: 8, right: 24, top: 34, bottom: 8, containLabel: true },
      xAxis: {
        type: 'category' as const, data: rows.map((r) => r.key),
        axisLine: { lineStyle: { color: c.border } },
        axisLabel: { color: c.mute, fontSize: 10, hideOverlap: true },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value' as const,
        axisLabel: { color: c.mute, fontSize: 11 },
        splitLine: { lineStyle: { color: c.border, type: 'dashed' as const } },
      },
      series: [
        {
          name: t('admin.llm.inputTokens'), type: 'bar' as const, stack: 't',
          data: rows.map((r) => r.input_tokens), barMaxWidth: 26,
          itemStyle: { color: CATEGORICAL[0], borderRadius: [4, 4, 0, 0] },
        },
        {
          name: t('admin.llm.outputTokens'), type: 'bar' as const, stack: 't',
          data: rows.map((r) => r.output_tokens), barMaxWidth: 26,
          itemStyle: { color: CATEGORICAL[1], borderRadius: [4, 4, 0, 0] },
        },
      ],
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- themeTick 是有意依赖：主题切换重建 option
  }, [rows, t, themeTick])

  if (loading) return <div className="center" style={{ padding: 40 }}><Spinner text={t('admin.llm.loadingUsage')} /></div>

  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="row wrap">
        <div className="row" style={{ gap: 4 }}>
          {(['day', 'scene', 'model'] as const).map((g) => (
            <button key={g} className={`btn sm ${groupBy === g ? 'primary' : ''}`} onClick={() => setGroupBy(g)}>
              {{ day: t('admin.llm.byDay'), scene: t('admin.llm.byScene'), model: t('admin.llm.byModel') }[g]}
            </button>
          ))}
        </div>
        {groupBy === 'day' && (
          <select className="select" style={{ width: 120 }} value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {[7, 14, 30, 90].map((d) => <option key={d} value={d}>{t('admin.llm.lastDays', { n: d })}</option>)}
          </select>
        )}
        <span className="spacer" />
        <span className="tag">{t('admin.llm.usageCalls', { n: totals.calls })}</span>
        <span className="tag">{t('admin.llm.usageInput', { n: totals.input.toLocaleString() })}</span>
        <span className="tag">{t('admin.llm.usageOutput', { n: totals.output.toLocaleString() })}</span>
        {totals.errors > 0 && <span className="tag danger">{t('admin.llm.usageErrors', { n: totals.errors })}</span>}
      </div>

      <div className="card">
        <div className="card-body">
          {rows.length ? <Chart option={option} height={300} /> : <Empty text={t('admin.llm.emptyUsage')} icon="📊" />}
        </div>
      </div>

      <div className="card">
        <table className="table">
          <thead><tr><th>{groupBy === 'day' ? t('admin.llm.colDate') : groupBy === 'scene' ? t('admin.llm.colScene') : t('admin.llm.colModel')}</th>
            <th>{t('admin.llm.colCalls')}</th><th>{t('admin.llm.inputTokens')}</th><th>{t('admin.llm.outputTokens')}</th>
            <th>{t('admin.llm.colFailed')}</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key}>
                <td>{groupBy === 'scene' ? (scenes[r.key] || r.key) : r.key}</td>
                <td>{r.calls}</td>
                <td>{r.input_tokens.toLocaleString()}</td>
                <td>{r.output_tokens.toLocaleString()}</td>
                <td>{r.errors > 0 ? <span className="tag danger">{r.errors}</span> : '0'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!rows.length && <Empty text={t('common.noData')} icon="📊" />}
      </div>
    </div>
  )
}

// ---------------- 调用日志 ----------------

function LogsPanel() {
  const { t, lang } = useI18n()
  const locale = lang === 'zh' ? 'zh-CN' : 'en-US'
  const [detail, setDetail] = useState<LlmCallLog | null>(null)

  return (
    <>
      <LlmLogsTable
        title={t('admin.llm.logsTitle')}
        allScenesText={t('admin.llm.allScenes')}
        modelColText={t('admin.llm.colProtocolModel')}
        errorColText={t('admin.llm.colError')}
        emptyText={t('admin.llm.emptyCalls')}
        onRowClick={setDetail}
      />

      {detail && (
        <Modal title={t('admin.llm.detailTitle')} onClose={() => setDetail(null)}
          footer={<button className="btn" onClick={() => setDetail(null)}>{t('common.close')}</button>}>
          <div className="col tiny" style={{ gap: 6 }}>
            <div>ID: <span className="mono">{detail.id}</span></div>
            <div>{t('admin.llm.detailTime')}{new Date(detail.created_at).toLocaleString(locale)}</div>
            <div>{t('admin.llm.detailScene')}{detail.scene || '-'}</div>
            <div>{t('admin.llm.detailProtocol')}{detail.protocol || '-'}</div>
            <div>{t('admin.llm.detailModel')}{detail.model || '-'}</div>
            <div>{t('admin.llm.detailConfigId')}<span className="mono">{detail.config_id || '-'}</span></div>
            <div>{t('admin.llm.detailTokens')}{detail.input_tokens} / {detail.output_tokens}</div>
            <div>{t('admin.llm.detailLatency')}{detail.latency_ms} ms</div>
            <div>{t('admin.llm.detailStatus')}{detail.status}</div>
            {detail.error_brief && (
              <div>
                <div className="muted">{t('admin.llm.detailErrorBrief')}</div>
                <pre style={{ whiteSpace: 'pre-wrap', color: 'var(--danger)' }}>{detail.error_brief}</pre>
              </div>
            )}
          </div>
        </Modal>
      )}
    </>
  )
}

// ---------------- 新增/编辑 ----------------

function ConfigEditor({ config, all, onClose, onSaved }: {
  config: LlmConfig | null
  all: LlmConfig[]
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useI18n()
  const [f, setF] = useState({
    name: config?.name || '',
    protocol: config?.protocol || 'openai',
    kind: config?.kind || 'chat',
    endpoint: config?.endpoint || 'https://api.openai.com/v1',
    model: config?.model || 'gpt-4o-mini',
    api_key: '',
    temperature: config?.temperature ?? 0.2,
    max_tokens: config?.max_tokens ?? 2048,
    top_p: config?.top_p ?? 1,
    timeout_sec: config?.timeout_sec ?? 60,
    retry_count: config?.retry_count ?? 2,
    stream_enabled: config?.stream_enabled ?? true,
    switch_policy: config?.switch_policy || 'new_session',
    fallback_config_id: config?.fallback_config_id || '',
    is_default: config?.is_default ?? false,
  })
  const [busy, setBusy] = useState(false)
  const [testing, setTesting] = useState<HealthState | null>(null)

  const upd = (patch: Partial<typeof f>) => setF((prev) => ({ ...prev, ...patch }))

  const save = async () => {
    if (!f.name.trim() || !f.endpoint.trim() || !f.model.trim()) {
      toast.warn(t('admin.llm.requireFields'))
      return
    }
    if (!config && !f.api_key.trim()) {
      toast.warn(t('admin.llm.requireApiKey'))
      return
    }
    setBusy(true)
    try {
      const body: Record<string, unknown> = {
        name: f.name.trim(), protocol: f.protocol, kind: f.kind,
        endpoint: f.endpoint.trim(), model: f.model.trim(),
        temperature: f.temperature, max_tokens: f.max_tokens, top_p: f.top_p,
        timeout_sec: f.timeout_sec, retry_count: f.retry_count,
        stream_enabled: f.stream_enabled, switch_policy: f.switch_policy,
        fallback_config_id: f.fallback_config_id || null,
        is_default: f.is_default,
      }
      if (f.api_key.trim()) body.api_key = f.api_key.trim()
      if (config) {
        await api.updateLlmConfig(config.id, body)
        toast.ok(t('admin.llm.configUpdated'))
      } else {
        const created = await api.createLlmConfig(body)
        toast.ok(t('admin.llm.configCreatedTesting'))
        try {
          const r = await api.healthCheckLlm(created.id)
          if (r.ok) toast.ok(t('admin.llm.connectivityOk', { n: r.latency_ms ?? '-' }))
          else toast.warn(t('admin.llm.connectivityFail', { msg: r.message }))
        } catch { /* 忽略 */ }
      }
      onSaved()
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
    }
  }

  const testNow = async () => {
    setTesting({ ok: false, checking: true })
    try {
      if (!config) { await save(); return }
      const r = await api.healthCheckLlm(config.id)
      setTesting(r.ok
        ? { ok: true, latency: r.latency_ms }
        : { ok: false, message: r.message })
    } catch (e) {
      setTesting({ ok: false, message: (e as ApiError).message })
    }
  }

  return (
    <Modal title={config ? t('admin.llm.editTitle', { name: config.name }) : t('admin.llm.newTitle')} width={680} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>{t('common.cancel')}</button>
        <button className="btn" disabled={busy} onClick={() => void testNow()}>
          {config ? t('admin.llm.testConnection') : t('admin.llm.saveAndTest')}
        </button>
        <button className="btn primary" disabled={busy} onClick={() => void save()}>{t('common.save')}</button>
      </>}>
      <div className="col" style={{ gap: 13 }}>
        <div className="grid2">
          <Field label={t('admin.llm.fieldName')} hint={t('admin.llm.fieldNameHint')}>
            <input className="input" value={f.name} onChange={(e) => upd({ name: e.target.value })} />
          </Field>
          <Field label={t('admin.llm.fieldKind')}>
            <select className="select" value={f.kind}
              onChange={(e) => upd({ kind: e.target.value as 'chat' | 'embedding' })}>
              <option value="chat">{t('admin.llm.kindChat')}</option>
              <option value="embedding">{t('admin.llm.kindEmbed')}</option>
            </select>
          </Field>
          <Field label={t('admin.llm.fieldProtocol')}>
            <select className="select" value={f.protocol}
              onChange={(e) => upd({ protocol: e.target.value as 'openai' | 'anthropic' })}>
              <option value="openai">{t('admin.llm.protoOpenai')}</option>
              <option value="anthropic">{t('admin.llm.protoAnthropic')}</option>
            </select>
          </Field>
          <Field label={t('admin.llm.fieldModel')}>
            <input className="input mono" value={f.model} onChange={(e) => upd({ model: e.target.value })} />
          </Field>
        </div>

        <div className="row wrap tiny muted" style={{ gap: 6 }}>
          {t('admin.llm.presets')}
          {PRESETS[f.protocol].map((p) => (
            <button key={p.model} className="btn sm" type="button"
              onClick={() => upd({ endpoint: p.endpoint, model: p.model })}>{p.model}</button>
          ))}
        </div>

        <Field label={t('admin.llm.fieldEndpoint')}
          hint={t('admin.llm.fieldEndpointHint')}>
          <input className="input mono" value={f.endpoint} onChange={(e) => upd({ endpoint: e.target.value })} />
        </Field>

        <Field label={config ? t('admin.llm.fieldApiKeyEdit') : t('admin.llm.fieldApiKeyNew')}
          hint={t('admin.llm.fieldApiKeyHint')}>
          <input className="input mono" type="password" value={f.api_key}
            placeholder={config ? t('admin.llm.apiKeyPhEdit') : 'sk-...'}
            onChange={(e) => upd({ api_key: e.target.value })} />
        </Field>

        <div className="grid4">
          <Field label="temperature">
            <input className="input" type="number" step="0.1" min="0" max="2" value={f.temperature}
              onChange={(e) => upd({ temperature: Number(e.target.value) })} />
          </Field>
          <Field label="max_tokens">
            <input className="input" type="number" min="1" value={f.max_tokens}
              onChange={(e) => upd({ max_tokens: Number(e.target.value) })} />
          </Field>
          <Field label={t('admin.llm.fieldTimeout')}>
            <input className="input" type="number" min="1" value={f.timeout_sec}
              onChange={(e) => upd({ timeout_sec: Number(e.target.value) })} />
          </Field>
          <Field label={t('admin.llm.fieldRetry')}>
            <input className="input" type="number" min="0" max="5" value={f.retry_count}
              onChange={(e) => upd({ retry_count: Number(e.target.value) })} />
          </Field>
        </div>

        <div className="grid2">
          <Field label={t('admin.llm.fieldFallback')} hint={t('admin.llm.fieldFallbackHint')}>
            <select className="select" value={f.fallback_config_id}
              onChange={(e) => upd({ fallback_config_id: e.target.value })}>
              <option value="">{t('admin.llm.noFallbackOpt')}</option>
              {all.filter((c) => c.kind === 'chat' && c.id !== config?.id && c.protocol === f.protocol)
                .map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </Field>
          <Field label={t('admin.llm.fieldSwitchPolicy')} hint={t('admin.llm.fieldSwitchPolicyHint')}>
            <select className="select" value={f.switch_policy}
              onChange={(e) => upd({ switch_policy: e.target.value as 'immediate' | 'new_session' })}>
              <option value="immediate">{t('admin.llm.policyImmediate')}</option>
              <option value="new_session">{t('admin.llm.policyNewSession')}</option>
            </select>
          </Field>
        </div>

        <div className="row wrap" style={{ gap: 18 }}>
          <label className="row tiny" style={{ gap: 6 }}>
            <input type="checkbox" checked={f.stream_enabled}
              onChange={(e) => upd({ stream_enabled: e.target.checked })} /> {t('admin.llm.streamLabel')}
          </label>
          <label className="row tiny" style={{ gap: 6 }}>
            <input type="checkbox" checked={f.is_default}
              onChange={(e) => upd({ is_default: e.target.checked })} />
            {f.kind === 'embedding' ? t('admin.llm.setDefaultEmbed') : t('admin.llm.setDefaultChat')}
          </label>
        </div>

        {testing && (
          <div className="tag" style={{
            display: 'block', whiteSpace: 'normal',
            color: testing.ok ? 'var(--ok)' : 'var(--danger)',
          }}>
            {testing.checking
              ? t('admin.llm.checking')
              : testing.ok
                ? t('admin.llm.healthOk', { n: testing.latency ?? '-' })
                : t('admin.llm.healthFail', { msg: testing.message ?? '' })}
          </div>
        )}
      </div>
    </Modal>
  )
}
