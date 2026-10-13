/** 通用 UI 原子组件。 */
import { type ReactNode, useEffect } from 'react'
import { useI18n } from '@/i18n'
import { useApp, type Toast } from '@/stores/app'

type TFn = (key: string, vars?: Record<string, string | number>) => string

export function StatusTag({ value, label, cls }: { value: string; label?: string; cls?: string }) {
  const { t } = useI18n()
  const map: Record<string, { cls: string; key: string }> = {
    connected: { cls: 'ok', key: 'ui.status.connected' },
    error: { cls: 'danger', key: 'ui.status.error' },
    untested: { cls: '', key: 'ui.status.untested' },
    success: { cls: 'ok', key: 'common.success' },
    failed: { cls: 'danger', key: 'common.failed' },
    partial: { cls: 'warn', key: 'ui.status.partial' },
    running: { cls: 'accent', key: 'ui.status.running' },
    active: { cls: 'ok', key: 'ui.status.active' },
    disabled: { cls: 'danger', key: 'ui.status.disabled' },
    manual: { cls: 'accent', key: 'ui.status.manual' },
    auto: { cls: '', key: 'ui.status.auto' },
    ok: { cls: 'ok', key: 'ui.status.ok' },
    timeout: { cls: 'warn', key: 'ui.status.timeout' },
    degraded: { cls: 'warn', key: 'ui.status.degraded' },
    unchecked: { cls: '', key: 'common.untested' },
    healthy: { cls: 'ok', key: 'ui.status.healthy' },
    empty: { cls: 'warn', key: 'ui.status.empty' },
    blocked: { cls: 'danger', key: 'ui.status.blocked' },
    merged: { cls: '', key: 'ui.status.merged' },
    deprecated: { cls: 'danger', key: 'ui.status.deprecated' },
    archived: { cls: '', key: 'ui.status.archived' },
    deleted: { cls: 'danger', key: 'common.deleted' },
    embedding: { cls: 'accent', key: 'ui.status.embedding' },
    chat: { cls: 'accent', key: 'ui.status.chat' },
  }
  const it: { cls: string; key?: string; text?: string } =
    map[value] || { cls: '', text: value || '-' }
  return <span className={`tag ${cls || it.cls}`}>{label || (it.text ?? t(it.key!))}</span>
}

export function Spinner({ text }: { text?: string }) {
  return (
    <span className="row" style={{ gap: 7 }}>
      <span className="spin" />
      {text && <span className="muted tiny">{text}</span>}
    </span>
  )
}

export function Empty({ text, icon = '∅' }: { text: string; icon?: string }) {
  return (
    <div className="empty">
      <span className="big">{icon}</span>
      {text}
    </div>
  )
}

export function Modal({ title, onClose, children, footer, width = 560 }: {
  title: ReactNode
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  width?: number
}) {
  const { t } = useI18n()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-mask" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="modal" style={{ maxWidth: width }}>
        <div className="modal-head">
          <h2 style={{ fontSize: 15 }}>{title}</h2>
          <span className="spacer" />
          <button className="btn ghost sm" onClick={onClose} aria-label={t('common.close')}>✕</button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  )
}

export function Drawer({ title, onClose, children, sub }: {
  title: ReactNode
  sub?: ReactNode
  onClose: () => void
  children: ReactNode
}) {
  const { t } = useI18n()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <div className="drawer">
        <div className="drawer-head">
          <div style={{ minWidth: 0 }}>
            <h2 style={{ fontSize: 15 }}>{title}</h2>
            {sub && <div className="tiny muted">{sub}</div>}
          </div>
          <span className="spacer" />
          <button className="btn ghost sm" onClick={onClose} aria-label={t('common.close')}>✕</button>
        </div>
        <div className="drawer-body">{children}</div>
      </div>
    </>
  )
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {hint && <span className="hint">{hint}</span>}
    </div>
  )
}

/** 数字/日期格式化 */
export function fmtNum(v: unknown, digits = 2): string {
  if (v === null || v === undefined || v === '') return '-'
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return String(v)
  const abs = Math.abs(n)
  if (abs >= 1e8) return (n / 1e8).toFixed(2) + ' 亿'
  if (abs >= 1e4) return (n / 1e4).toFixed(2) + ' 万'
  if (Number.isInteger(n)) return n.toLocaleString('zh-CN')
  return n.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

export function fmtRaw(v: unknown): string {
  if (v === null || v === undefined) return '-'
  if (typeof v === 'number') return String(v)
  const s = String(v)
  return s.length > 60 ? s.slice(0, 60) + '…' : s
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 19).replace('T', ' ')
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function relTime(iso: string | null | undefined, t?: TFn): string {
  if (!iso) return '-'
  const d = new Date(iso).getTime()
  if (Number.isNaN(d)) return '-'
  const diff = Date.now() - d
  if (diff < 60_000) return t ? t('ui.justNow') : '刚刚'
  if (diff < 3_600_000) {
    const n = Math.floor(diff / 60_000)
    return t ? t('ui.minutesAgo', { n }) : `${n} 分钟前`
  }
  if (diff < 86_400_000) {
    const n = Math.floor(diff / 3_600_000)
    return t ? t('ui.hoursAgo', { n }) : `${n} 小时前`
  }
  if (diff < 7 * 86_400_000) {
    const n = Math.floor(diff / 86_400_000)
    return t ? t('ui.daysAgo', { n }) : `${n} 天前`
  }
  return fmtTime(iso).slice(0, 10)
}

export function Toasts() {
  const { toasts, drop } = useApp()
  if (!toasts.length) return null
  return (
    <div style={{
      position: 'fixed', top: 16, right: 16, zIndex: 200,
      display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 380,
    }}>
      {toasts.map((t: Toast) => (
        <div
          key={t.id}
          className={`card`}
          onClick={() => drop(t.id)}
          style={{
            padding: '10px 14px', fontSize: 13, cursor: 'pointer',
            borderLeft: `3px solid ${t.kind === 'ok' ? 'var(--ok)'
              : t.kind === 'error' ? 'var(--danger)'
                : t.kind === 'warn' ? 'var(--warn)' : 'var(--accent)'}`,
            boxShadow: 'var(--shadow-lg)',
          }}
        >
          {t.text}
        </div>
      ))}
    </div>
  )
}

export function Confirm({ title, text, onCancel, onOk, danger, okText }: {
  title: string
  text: ReactNode
  onCancel: () => void
  onOk: () => void
  danger?: boolean
  okText?: string
}) {
  const { t } = useI18n()
  return (
    <Modal title={title} onClose={onCancel} width={440}
      footer={<>
        <button className="btn" onClick={onCancel}>{t('common.cancel')}</button>
        <button className={`btn ${danger ? 'danger' : 'primary'}`} onClick={onOk}>{okText ?? t('common.confirm')}</button>
      </>}>
      <div style={{ fontSize: 13.5, lineHeight: 1.7 }}>{text}</div>
    </Modal>
  )
}

/** 分页条 */
export function Pager({ page, pageSize, total, onChange }: {
  page: number
  pageSize: number
  total: number
  onChange: (p: number) => void
}) {
  const { t } = useI18n()
  const pages = Math.max(1, Math.ceil(total / pageSize))
  return (
    <div className="row" style={{ padding: '10px 16px', justifyContent: 'flex-end' }}>
      <span className="tiny muted">
        {t('common.total', { n: total })} · {t('common.page', { n: `${page}/${pages}` })}
      </span>
      <button className="btn sm" disabled={page <= 1} onClick={() => onChange(page - 1)}>{t('common.prevPage')}</button>
      <button className="btn sm" disabled={page >= pages} onClick={() => onChange(page + 1)}>{t('common.nextPage')}</button>
    </div>
  )
}

export function Tabs<T extends string>({ value, onChange, items }: {
  value: T
  onChange: (v: T) => void
  items: { key: T; label: string; badge?: number }[]
}) {
  return (
    <div className="tabs">
      {items.map((it) => (
        <button key={it.key} className={`tab ${value === it.key ? 'active' : ''}`}
          onClick={() => onChange(it.key)}>
          {it.label}
          {it.badge !== undefined && <span className="tag" style={{ marginLeft: 6 }}>{it.badge}</span>}
        </button>
      ))}
    </div>
  )
}
