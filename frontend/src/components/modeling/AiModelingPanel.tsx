/** AI 建模建议会话（PRD 3.3D-1）：需求 → 方案 → 对话修订 → 接受一键落地。 */
import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError } from '@/api/client'
import { streamSse } from '@/api/sse'
import { StepTimeline, type StepState } from '@/components/chat/StepTimeline'
import { SqlText } from '@/components/common/SqlText'
import { Markdown } from '@/components/common/Markdown'
import { Confirm, Empty, Field, Modal, relTime, Spinner } from '@/components/common/ui'
import { toast } from '@/stores/app'
import { useI18n } from '@/i18n'
import type { AcceptResult, EtlDesignResult, ModelingMessageRow, ModelingProposal, ModelingSessionRow, ProposalTable , LlmChatOption } from '@/types'

interface EngineInfo { engine: string; engine_name: string; scene: string; model: string; available: boolean }

/** 待确认动作（自研 Confirm 替换 window.confirm）：确认后执行 action */
interface ConfirmReq {
  title: string
  text: ReactNode
  danger?: boolean
  okText?: string
  action: () => void
}

/** 方案表 → EtlDesignResult（与对话 ETL 面板同款动作的入参，PRD 3.3D-1） */
function toDesign(t: ProposalTable, proposal: ModelingProposal): EtlDesignResult {
  return {
    target_table: t.name, layer: t.layer, schedule: t.schedule || '',
    ddl: t.ddl, etl_sql: t.etl_sql, dialect: 'hive',
    source_tables: (t.source_tables || []).map((s) => ({ table: s, layer: '', reason: '' })),
    field_mapping: [], notes: t.rationale || '', warnings: proposal.risks || [],
  }
}

export function AiModelingPanel({ onAccepted }: { onAccepted: () => void }) {
  const { t } = useI18n()
  const [sessions, setSessions] = useState<ModelingSessionRow[]>([])
  const [activeId, setActiveId] = useState('')
  const [messages, setMessages] = useState<ModelingMessageRow[]>([])
  const [input, setInput] = useState('')
  const [running, setRunning] = useState(false)
  const [liveSteps, setLiveSteps] = useState<StepState[]>([])
  const [engine, setEngine] = useState<EngineInfo | null>(null)
  const [chatOptions, setChatOptions] = useState<LlmChatOption[]>([])
  const [accepting, setAccepting] = useState('')
  const abortRef = useRef<(() => void) | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  // 自研确认弹窗（替换 window.confirm）
  const [confirm, setConfirm] = useState<ConfirmReq | null>(null)
  // 新建建模需求（替换 window.prompt 的 Modal 表单）
  const [newOpen, setNewOpen] = useState(false)
  const [newReq, setNewReq] = useState('')
  // 当前会话 id 的 ref：SSE 回调等闭包内判断会话是否已切换
  const activeIdRef = useRef(activeId)
  useEffect(() => { activeIdRef.current = activeId }, [activeId])

  const loadSessions = useCallback(async (keep?: string) => {
    try {
      const rows = await api.listModelingSessions()
      setSessions(rows)
      const target = rows.find((s) => s.id === (keep || activeId)) || rows[0]
      if (target) setActiveId(target.id)
    } catch (e) { toast.error((e as ApiError).message) }
  }, [activeId])

  useEffect(() => { void loadSessions() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { api.modelingEngine().then(setEngine).catch(() => undefined) }, [])
  useEffect(() => { api.listLlmChatOptions().then(setChatOptions).catch(() => undefined) }, [])

  /** 会话级模型切换（PRD 3.3D-1）：空 = 跟随 modeling_agent 场景绑定 */
  const switchModel = async (configId: string) => {
    if (!activeId) return
    try {
      const s = await api.updateModelingSession(activeId, { llm_config_id: configId || null })
      setSessions((prev) => prev.map((x) => (x.id === s.id ? { ...x, ...s } : x)))
      toast.ok(configId ? t('modeling.switchModelToast') : t('modeling.restoreModelToast'))
    } catch (e) { toast.error((e as ApiError).message) }
  }
  useEffect(() => {
    abortRef.current?.()
    setRunning(false)
    setLiveSteps([])
    if (!activeId) { setMessages([]); return }
    // 快速切换会话的顺序守卫：过期响应直接丢弃，避免旧会话数据覆盖新会话
    let cancelled = false
    api.getModelingSession(activeId)
      .then((d) => { if (!cancelled) setMessages(d.messages) })
      .catch((e) => { if (!cancelled) toast.error((e as ApiError).message) })
    return () => { cancelled = true }
  }, [activeId])
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, liveSteps.length])

  /** 创建会话并直接发起首轮建模（Modal 表单提交） */
  const createSession = async () => {
    const requirement = newReq.trim()
    if (!requirement) return
    try {
      const s = await api.createModelingSession({ requirement })
      setNewOpen(false)
      setNewReq('')
      await loadSessions(s.id)
      setActiveId(s.id)
      // 首条需求直接发起建模
      setTimeout(() => void send(s.id, requirement), 50)
    } catch (e) { toast.error((e as ApiError).message) }
  }

  const send = (sid: string, content: string) => {
    if (running) { toast.warn(t('modeling.aiBusy')); return }
    setInput('')
    setRunning(true)
    setLiveSteps([])
    setMessages((prev) => [...prev, {
      id: `local-${Date.now()}`, seq: prev.length, role: 'user', content,
      proposal: null, steps: [], created_at: new Date().toISOString(),
    }])
    abortRef.current = streamSse(`/api/v1/modeling/sessions/${sid}/messages`, { content }, {
      onStep: (d) => setLiveSteps((prev) => {
        const steps = [...prev]
        const i = steps.findIndex((x) => x.step === d.step && x.status === 'running')
        if (d.status === 'started') steps.push({ step: d.step, status: 'running' })
        else if (i >= 0) steps[i] = { ...steps[i], status: d.status as StepState['status'], detail: d.detail, ms: d.ms }
        else steps.push({ step: d.step, status: d.status as StepState['status'], detail: d.detail, ms: d.ms })
        return steps
      }),
      onEvent: () => undefined,
      onError: (d) => toast.error(d.message),
      onDone: async () => {
        setRunning(false)
        // 流结束时若已切换到其他会话，不再回写消息列表（避免竞态覆盖）
        if (activeIdRef.current === sid) {
          try {
            const d = await api.getModelingSession(sid)
            if (activeIdRef.current === sid) setMessages(d.messages)
          } catch { /* ignore */ }
        }
        void loadSessions(sid)
      },
    })
  }

  const accept = (messageId: string) => {
    if (!activeId) return
    setConfirm({
      title: t('modeling.confirmTitle'),
      text: <span style={{ whiteSpace: 'pre-line' }}>{t('modeling.confirmAcceptProposal')}</span>,
      action: () => void doAccept(messageId),
    })
  }

  const doAccept = async (messageId: string) => {
    if (!activeId) return
    setAccepting(messageId)
    try {
      const r: AcceptResult = await api.acceptModelingProposal(activeId, messageId)
      toast.ok(r.message)
      const failed = r.results.filter((x) => !x.ok)
      if (failed.length) toast.warn(failed.map((f) => `${f.table}: ${f.error}`).join('；'))
      if (r.created > 0) {
        setConfirm({
          title: t('modeling.confirmTitle'),
          text: t('modeling.confirmGotoTasks'),
          action: () => onAccepted(),
        })
      }
      void loadSessions(activeId)
    } catch (e) { toast.error((e as ApiError).message) } finally { setAccepting('') }
  }

  const remove = (id: string) => {
    setConfirm({
      title: t('modeling.confirmTitle'),
      text: t('modeling.confirmDeleteSession'),
      danger: true,
      okText: t('common.delete'),
      action: () => void doRemove(id),
    })
  }

  const doRemove = async (id: string) => {
    try {
      await api.deleteModelingSession(id)
      if (id === activeId) setActiveId('')
      await loadSessions()
    } catch (e) { toast.error((e as ApiError).message) }
  }

  const active = sessions.find((s) => s.id === activeId) || null

  return (
    <div className="row" style={{ alignItems: 'stretch', gap: 16, minHeight: 480 }}>
      {/* 会话列表 */}
      <div className="card" style={{ width: 260, flexShrink: 0 }}>
        <div className="card-body" style={{ padding: 8 }}>
          <button className="btn primary" style={{ width: '100%', marginBottom: 8 }}
            onClick={() => { setNewReq(''); setNewOpen(true) }}>＋ {t('modeling.newSessionBtn')}</button>
          {sessions.length === 0 && <div className="empty tiny" style={{ padding: 16 }}>{t('modeling.noSessions')}</div>}
          {sessions.map((s) => (
            <div key={s.id} className={`conv ${s.id === activeId ? 'active' : ''}`}
              style={{ padding: '8px 10px', cursor: 'pointer', borderRadius: 6 }}
              onClick={() => setActiveId(s.id)}>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="t truncate" style={{ fontSize: 13 }}>{s.title}</div>
                <div className="d tiny muted">{t('modeling.messagesCount', { n: s.message_count })} · {relTime(s.updated_at, t)}</div>
              </div>
              <div className="col" style={{ gap: 4, alignItems: 'flex-end' }}>
                <span className={`tag tiny ${s.status === 'accepted' ? 'ok' : ''}`}>
                  {s.status === 'accepted' ? t('modeling.sessionAccepted') : t('modeling.sessionActive')}
                </span>
                <button className="btn ghost sm" onClick={(e) => { e.stopPropagation(); void remove(s.id) }}>✕</button>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* 对话区 */}
      <div className="card" style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
        <div className="row" style={{ padding: '8px 14px', borderBottom: '1px solid var(--border)' }}>
          <span className="tiny muted">
            {t('modeling.aiEngineerLabel')}
            {engine?.available
              ? <span className="tag ok">{engine.engine_name} · {engine.model}</span>
              : <span className="tag warn">{t('modeling.agentUnbound')}</span>}
          </span>
          {active && chatOptions.length > 0 && (
            <select className="input" style={{ width: 'auto', padding: '2px 6px', fontSize: 12 }}
              title={t('modeling.modelSelectTitle')}
              value={active.llm_config_id || ''}
              onChange={(e) => void switchModel(e.target.value)}>
              <option value="">{t('modeling.sceneBound')}{engine?.model ? `（${engine.model}）` : ''}</option>
              {chatOptions.map((o) => (
                <option key={o.id} value={o.id}>{o.name} · {o.model}</option>
              ))}
            </select>
          )}
          <span className="spacer" />
          <span className="tiny muted">{t('modeling.safetyHint')}</span>
        </div>
        <div ref={scrollRef} style={{ flex: 1, overflow: 'auto', padding: 14, maxHeight: 560 }}>
          {!active && <Empty icon="🤖" text={t('modeling.aiEmptyState')} />}
          {messages.map((m) => (
            <MessageBubble key={m.id} msg={m} sessionId={activeId} sessionStatus={active?.status || 'active'}
              accepting={accepting === m.id} onAccept={() => void accept(m.id)} />
          ))}
          {running && (
            <div className="msg assistant"><div className="bubble">
              <StepTimeline steps={liveSteps} live />
              <Spinner text={t('modeling.aiWorkingSpinner')} />
            </div></div>
          )}
        </div>
        {active && (
          <div style={{ borderTop: '1px solid var(--border)', padding: 10 }}>
            <div className="row" style={{ gap: 8 }}>
              <textarea className="input" rows={2} style={{ flex: 1 }}
                placeholder={t('modeling.chatPlaceholder')}
                value={input} onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); if (input.trim()) send(active.id, input.trim()) }
                }} />
              <button className="btn primary" disabled={running || !input.trim()}
                onClick={() => send(active.id, input.trim())}>{t('modeling.sendBtn')}</button>
            </div>
          </div>
        )}
      </div>

      {/* 新建建模需求（替换 window.prompt 的 Modal 表单） */}
      {newOpen && (
        <Modal title={t('modeling.newSessionTitle')} onClose={() => setNewOpen(false)} width={560}
          footer={<>
            <button className="btn" onClick={() => setNewOpen(false)}>{t('common.cancel')}</button>
            <button className="btn primary" disabled={!newReq.trim()}
              onClick={() => void createSession()}>{t('modeling.startModelingBtn')}</button>
          </>}>
          <Field label={t('modeling.newSessionLabel')}>
            <textarea className="input" rows={4} autoFocus
              placeholder={t('modeling.newSessionPrompt')}
              value={newReq} onChange={(e) => setNewReq(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault()
                  if (newReq.trim()) void createSession()
                }
              }} />
          </Field>
        </Modal>
      )}

      {/* 自研确认弹窗（替换 window.confirm） */}
      {confirm && (
        <Confirm title={confirm.title} text={confirm.text} danger={confirm.danger} okText={confirm.okText}
          onCancel={() => setConfirm(null)}
          onOk={() => { const a = confirm.action; setConfirm(null); a() }} />
      )}
    </div>
  )
}

function MessageBubble({ msg, sessionId, sessionStatus, accepting, onAccept }: {
  msg: ModelingMessageRow; sessionId: string; sessionStatus: string; accepting: boolean; onAccept: () => void
}) {
  const { t } = useI18n()
  if (msg.role === 'user') {
    return <div className="msg user"><div className="bubble">{msg.content}</div></div>
  }
  return (
    <div className="msg assistant"><div className="bubble" style={{ maxWidth: '100%' }}>
      {msg.steps.length > 0 && (
        <StepTimeline steps={msg.steps.map((s) => ({
          step: s.step, status: s.status as StepState['status'], detail: s.detail, ms: s.ms,
        }))} />
      )}
      {msg.content && <Markdown text={msg.content} />}
      {msg.proposal && <ProposalCard proposal={msg.proposal} sessionId={sessionId} locked={sessionStatus === 'accepted'} />}
      {msg.proposal && sessionStatus === 'accepted' && (
        <div className="row mt12" style={{ gap: 8 }}>
          <span className="tag ok">✅ {t('modeling.acceptedTag')}</span>
        </div>
      )}
      {msg.proposal && sessionStatus === 'active' && (
        <div className="row mt12" style={{ gap: 8 }}>
          <button className="btn primary sm" disabled={accepting} onClick={onAccept}>
            {accepting ? t('modeling.acceptCreating') : `✅ ${t('modeling.acceptBtn')}`}
          </button>
          <span className="tiny muted" style={{ alignSelf: 'center' }}>
            {t('modeling.keepRevising')}
          </span>
        </div>
      )}
    </div></div>
  )
}

function ProposalCard({ proposal, sessionId, locked }: { proposal: ModelingProposal; sessionId: string; locked?: boolean }) {
  const nav = useNavigate()
  const { t: tr } = useI18n()
  const [open, setOpen] = useState<string>('')
  const [busy, setBusy] = useState('')
  // 自研确认弹窗（替换 window.confirm）
  const [confirm, setConfirm] = useState<ConfirmReq | null>(null)

  // 与对话 ETL 面板（§3.3B）一致的三个动作：转任务 / 推送 Azkaban / 下载 zip
  const saveAsTask = async (t: ProposalTable) => {
    setBusy(t.name)
    try {
      const d = toDesign(t, proposal)
      const task = await api.createEtlTask({
        name: t.name, target_table: t.name, layer: t.layer, dialect: d.dialect,
        schedule: t.schedule || '', cron: t.cron || '', ddl: t.ddl, etl_sql: t.etl_sql,
        source_tables: d.source_tables, field_mapping: [], notes: t.rationale || '',
        warnings: proposal.risks || [], source: 'ai', conversation_id: null,
        modeling_session_id: sessionId || null, datasource_id: null,
      })
      toast.ok(tr('modeling.taskCreatedToast', { name: task.name }))
      setConfirm({
        title: tr('modeling.confirmTitle'),
        text: tr('modeling.confirmGotoTasks'),
        action: () => nav(`/modeling?task=${task.id}`),
      })
    } catch (e) { toast.error((e as ApiError).message) } finally { setBusy('') }
  }

  const push = (t: ProposalTable) => {
    setConfirm({
      title: tr('modeling.confirmTitle'),
      text: <span style={{ whiteSpace: 'pre-line' }}>{tr('modeling.confirmPushProposal', { name: t.name })}</span>,
      action: () => void doPush(t),
    })
  }

  const doPush = async (t: ProposalTable) => {
    setBusy(t.name)
    try {
      const r = await api.pushEtlToAzkaban(toDesign(t, proposal), t.cron || '')
      if (r.ok) toast.ok(tr('modeling.pushedToAzkaban'))
      else toast.warn(r.message)
    } catch (e) { toast.error((e as ApiError).message) } finally { setBusy('') }
  }

  const download = (t: ProposalTable) => {
    void api.downloadEtlPackage(toDesign(t, proposal)).then(() => toast.ok(tr('modeling.zipDownloaded')))
      .catch((e: ApiError) => toast.error(e.message))
  }

  return (
    <div className="mt12 col" style={{ gap: 10 }}>
      {(proposal.reuse_notes || (proposal.risks || []).length > 0) && (
        <div className="tiny muted">
          {proposal.reuse_notes && <div>♻ {tr('modeling.reuseNotes')}{proposal.reuse_notes}</div>}
          {(proposal.risks || []).map((r, i) => <div key={i}>⚠ {r}</div>)}
        </div>
      )}
      {proposal.tables.map((t) => (
        <div key={t.name} className="card" style={{ border: '1px solid var(--border)' }}>
          <div className="card-body" style={{ padding: 10 }}>
            <div className="row" style={{ cursor: 'pointer' }}
              onClick={() => setOpen(open === t.name ? '' : t.name)}>
              <span>{open === t.name ? '▾' : '▸'}</span>
              <code style={{ fontWeight: 600 }}>{t.name}</code>
              <span className="tag accent">{t.layer?.toUpperCase()}</span>
              {t.grain && <span className="tag">{t.grain}</span>}
              <span className="tiny muted">{t.comment}</span>
              <span className="spacer" />
              {t.schedule && <span className="tiny muted">{t.schedule}</span>}
              {(t.ddl || t.etl_sql) && !locked && (
                <div className="row" style={{ gap: 6 }} onClick={(e) => e.stopPropagation()}>
                  <button className="btn sm primary" disabled={busy === t.name}
                    onClick={() => void saveAsTask(t)}>🏗 {tr('modeling.saveAsTaskBtn')}</button>
                  <button className="btn sm" disabled={busy === t.name}
                    onClick={() => void push(t)}>🚀 {tr('modeling.pushAzkabanBtn')}</button>
                  <button className="btn ghost sm" disabled={busy === t.name}
                    onClick={() => download(t)}>⬇ {tr('modeling.downloadZipBtn')}</button>
                </div>
              )}
              {locked && <span className="tag tiny ok">{tr('modeling.sessionAccepted')}</span>}
            </div>
            {open === t.name && (
              <div className="mt8 col" style={{ gap: 8 }}>
                {t.rationale && <div className="tiny muted">{tr('modeling.rationaleLabel')}{t.rationale}</div>}
                {(t.source_tables || []).length > 0 && (
                  <div className="tiny">{tr('modeling.sourceTablesLabel')}{(t.source_tables || []).map((s) => <code key={s} className="tag" style={{ marginRight: 4 }}>{s}</code>)}</div>
                )}
                {t.ddl && <pre className="mono" style={{ fontSize: 12, background: 'var(--bg)', padding: 8, borderRadius: 6, overflow: 'auto' }}><SqlText code={t.ddl} /></pre>}
                {t.etl_sql && <pre className="mono" style={{ fontSize: 12, background: 'var(--bg)', padding: 8, borderRadius: 6, overflow: 'auto' }}><SqlText code={t.etl_sql} /></pre>}
              </div>
            )}
          </div>
        </div>
      ))}

      {/* 自研确认弹窗（替换 window.confirm） */}
      {confirm && (
        <Confirm title={confirm.title} text={confirm.text} danger={confirm.danger} okText={confirm.okText}
          onCancel={() => setConfirm(null)}
          onOk={() => { const a = confirm.action; setConfirm(null); a() }} />
      )}
    </div>
  )
}
