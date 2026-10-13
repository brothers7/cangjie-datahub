/** SSE 客户端：fetch + ReadableStream 手写解析（支持 POST body 与 Authorization 头）。 */
import { ApiError, tokenStore } from '@/api/client'
import type { AnomalyReport, ChartSpec, EtlDesignResult, MessagePayload, MetricCard } from '@/types'

export interface SseHandlers {
  onMeta?: (d: { conversation_id: string; datasource: { id: string; name: string; db_type: string } }) => void
  onStep?: (d: { step: string; status: string; detail?: string; ms?: number }) => void
  onKgMatch?: (d: {
    metrics: { id: string; name: string; confidence: number; why: string }[]
    dimensions: { id: string; name: string }[]
    time_range: { start: string; end: string; label: string } | null
    note?: string
  }) => void
  onSql?: (d: { sql: string; tables: string[]; dialect: string; guarded: boolean }) => void
  onToken?: (text: string) => void
  onChart?: (d: { chart_spec: ChartSpec }) => void
  onReport?: (d: { report_id: string; sections: { key: string; title: string; content_md: string }[] }) => void
  onEtl?: (d: EtlDesignResult) => void
  onMetricCards?: (d: { metrics: MetricCard[] }) => void
  onConfirmRequired?: (d: { candidates: { id: string; name: string; confidence: number; why: string }[] }) => void
  onAnomaly?: (d: AnomalyReport) => void
  onError?: (d: { code: string; message: string; recoverable?: boolean }) => void
  onDone?: (d: { duration_ms: number; error: string }) => void
}

const EVENT_MAP: Record<string, keyof SseHandlers> = {
  meta: 'onMeta',
  step: 'onStep',
  kg_match: 'onKgMatch',
  sql: 'onSql',
  chart: 'onChart',
  report: 'onReport',
  etl: 'onEtl',
  metric_cards: 'onMetricCards',
  confirm_required: 'onConfirmRequired',
  anomaly: 'onAnomaly',
  error: 'onError',
  done: 'onDone',
}

/**
 * 发起一次问答流。返回 abort 函数。
 * 事件顺序：meta → step* → kg_match → sql → … → chart → token* → report → done
 */
export function streamAsk(
  conversationId: string,
  question: string,
  handlers: SseHandlers,
  confirm?: Record<string, unknown> | null,
): () => void {
  const ctrl = new AbortController()

  const run = async () => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    const token = tokenStore.get()
    if (token) headers.Authorization = `Bearer ${token}`

    let resp: Response
    try {
      resp = await fetch(`/api/v1/chat/conversations/${conversationId}/messages`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ question, confirm: confirm ?? null }),
        signal: ctrl.signal,
      })
    } catch (e) {
      if ((e as Error).name === 'AbortError') return
      handlers.onError?.({ code: 'NETWORK', message: '网络请求失败，请检查服务状态' })
      handlers.onDone?.({ duration_ms: 0, error: 'network' })
      return
    }

    if (!resp.ok || !resp.body) {
      let message = `请求失败 (${resp.status})`
      try {
        const j = await resp.json()
        message = j.message || message
      } catch { /* ignore */ }
      handlers.onError?.({ code: 'HTTP_ERROR', message, recoverable: true })
      handlers.onDone?.({ duration_ms: 0, error: message })
      return
    }

    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buf = ''
    let eventName = ''
    let dataLines: string[] = []

    const flush = () => {
      if (!dataLines.length) { eventName = ''; return }
      const raw = dataLines.join('\n')
      const name = eventName
      eventName = ''
      dataLines = []
      if (!name) return

      if (name === 'token') {
        try {
          const d = JSON.parse(raw)
          if (d.text) handlers.onToken?.(d.text)
        } catch { /* ignore */ }
        return
      }
      const key = EVENT_MAP[name]
      if (!key) return
      try {
        const d = JSON.parse(raw)
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        ;(handlers[key] as ((x: any) => void) | undefined)?.(d)
      } catch { /* 忽略无法解析的事件 */ }
    }

    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, idx).replace(/\r$/, '')
          buf = buf.slice(idx + 1)
          if (line === '') { flush(); continue }
          if (line.startsWith(':')) continue      // heartbeat
          if (line.startsWith('event:')) eventName = line.slice(6).trim()
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
        }
      }
      flush()
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        handlers.onError?.({ code: 'STREAM_ERROR', message: '数据流中断，请重试' })
        handlers.onDone?.({ duration_ms: 0, error: 'stream' })
      }
    }
  }

  void run()
  return () => ctrl.abort()
}

/** 从落库的历史消息 payload 还原一次问答的展示状态 */
export function payloadToView(payload: MessagePayload | null) {
  if (!payload) return null
  return {
    steps: payload.steps || [],
    intent: payload.intent,
    rewritten: payload.rewritten,
    metrics: payload.matched_metrics || [],
    dimensions: payload.dimensions || [],
    timeRange: payload.time_range,
    sql: payload.sql,
    chartSpec: payload.chart_spec,
    anomalies: payload.anomalies,
    warnings: payload.validation?.warnings || [],
    webEvidences: payload.web_evidences || [],
    cacheHit: payload.cache_hit,
    error: payload.error,
    report: payload.report,
    etl: payload.etl ?? null,
    metricCards: payload.metric_cards ?? [],
    pendingConfirm: payload.pending_confirm,
  }
}

export type AskView = NonNullable<ReturnType<typeof payloadToView>>
export { ApiError }

/* ================= 通用 SSE（建模会话 / 任务 AI 修订） ================= */

export interface GenericSseHandlers {
  onStep?: (d: { step: string; status: string; detail?: string; ms?: number }) => void
  onEvent?: (name: string, data: Record<string, unknown>) => void
  onError?: (d: { code: string; message: string }) => void
  onDone?: (d: { error?: string }) => void
}

/** 通用 POST SSE 流。返回 abort 函数。step 事件走 onStep，其余事件走 onEvent。 */
export function streamSse(url: string, body: unknown, handlers: GenericSseHandlers): () => void {
  const ctrl = new AbortController()
  const run = async () => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    const token = tokenStore.get()
    if (token) headers.Authorization = `Bearer ${token}`
    let resp: Response
    try {
      resp = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body), signal: ctrl.signal })
    } catch (e) {
      if ((e as Error).name === 'AbortError') return
      handlers.onError?.({ code: 'NETWORK', message: '网络请求失败' })
      handlers.onDone?.({ error: 'network' })
      return
    }
    if (!resp.ok || !resp.body) {
      let message = `请求失败 (${resp.status})`
      try { message = (await resp.json()).message || message } catch { /* ignore */ }
      handlers.onError?.({ code: 'HTTP_ERROR', message })
      handlers.onDone?.({ error: message })
      return
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buf = '', eventName = '', dataLines: string[] = []
    const flush = () => {
      if (!dataLines.length) { eventName = ''; return }
      const raw = dataLines.join('\n'), name = eventName
      eventName = ''; dataLines = []
      if (!name) return
      try {
        const d = JSON.parse(raw)
        if (name === 'step') handlers.onStep?.(d)
        else if (name === 'error') handlers.onError?.(d)
        else if (name === 'done') handlers.onDone?.(d)
        else handlers.onEvent?.(name, d)
      } catch { /* ignore */ }
    }
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        let idx: number
        while ((idx = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, idx).replace(/\r$/, '')
          buf = buf.slice(idx + 1)
          if (line === '') { flush(); continue }
          if (line.startsWith(':')) continue
          if (line.startsWith('event:')) eventName = line.slice(6).trim()
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
        }
      }
      flush()
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        handlers.onError?.({ code: 'STREAM_ERROR', message: '数据流中断，请重试' })
        handlers.onDone?.({ error: 'stream' })
      }
    }
  }
  void run()
  return () => ctrl.abort()
}
