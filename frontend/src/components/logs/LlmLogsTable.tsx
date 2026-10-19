/** LLM 调用日志表：状态/场景筛选 + 分页 + 明细列共用。LogsPage 与 LlmConfigPage 复用，差异（详情弹窗、头部附加信息）走 props。 */
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { api, ApiError } from '@/api/client'
import { Empty, Pager, Spinner, StatusTag } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import { sceneLabels } from '@/labels'
import { toast } from '@/stores/app'
import type { LlmCallLog } from '@/types'

const PAGE_SIZE = 20

export function LlmLogsTable({ title, allScenesText, modelColText, errorColText, emptyText, headerExtra, onRowClick }: {
  title: string
  allScenesText: string
  modelColText: string
  errorColText: string
  emptyText: string
  /** 头部标题旁的附加内容（如本页 token 统计），按当前页行渲染 */
  headerExtra?: (rows: LlmCallLog[]) => ReactNode
  /** 传入则行可点击（如打开详情弹窗） */
  onRowClick?: (row: LlmCallLog) => void
}) {
  const { t, lang } = useI18n()
  const locale = lang === 'zh' ? 'zh-CN' : 'en-US'
  const scenes = sceneLabels(lang)
  const [rows, setRows] = useState<LlmCallLog[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [status, setStatus] = useState('')
  const [scene, setScene] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api.llmCallLogs({
        status: status || undefined, scene: scene || undefined, page, page_size: PAGE_SIZE,
      })
      setRows(r.rows)
      setTotal(r.total)
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setLoading(false)
    }
  }, [status, scene, page])

  useEffect(() => { void load() }, [load])

  return (
    <div className="card">
      <div className="card-head">
        <h2>{title}</h2>
        {headerExtra?.(rows)}
        <span className="spacer" />
        <select className="select" style={{ width: 130 }} value={status}
          onChange={(e) => { setStatus(e.target.value); setPage(1) }}>
          <option value="">{t('common.allStatus')}</option>
          {['ok', 'timeout', 'error', 'degraded'].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <select className="select" style={{ width: 150 }} value={scene}
          onChange={(e) => { setScene(e.target.value); setPage(1) }}>
          <option value="">{allScenesText}</option>
          {Object.entries(scenes).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </div>
      <div style={{ overflow: 'auto' }}>
        <table className="table">
          <thead><tr><th>{t('common.time')}</th><th>{t('admin.logs.colScene')}</th><th>{modelColText}</th>
            <th>{t('admin.logs.colInput')}</th><th>{t('admin.logs.colOutput')}</th>
            <th>{t('admin.logs.colDuration')}</th><th>{t('common.status')}</th><th>{errorColText}</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} style={onRowClick ? { cursor: 'pointer' } : undefined}
                onClick={onRowClick ? () => onRowClick(r) : undefined}>
                <td className="tiny">{new Date(r.created_at).toLocaleString(locale)}</td>
                <td>{r.scene ? (scenes[r.scene] || r.scene) : '-'}</td>
                <td className="tiny mono">{r.model || '-'}</td>
                <td>{r.input_tokens}</td>
                <td>{r.output_tokens}</td>
                <td>{r.latency_ms}ms</td>
                <td><StatusTag value={r.status} /></td>
                <td className="tiny truncate" style={{ maxWidth: 240, color: r.error_brief ? 'var(--danger)' : undefined }}>
                  {r.error_brief || '-'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {loading && <div className="center" style={{ padding: 24 }}><Spinner /></div>}
        {!loading && !rows.length && <Empty icon="📜" text={emptyText} />}
      </div>
      <Pager page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} />
    </div>
  )
}
