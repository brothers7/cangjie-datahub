/** 分析报表：列表 / 检索 / 阅读 / 导出 / 删除，以及报表模板。 */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '@/api/client'
import { Markdown } from '@/components/common/Markdown'
import { Confirm, Drawer, Empty, Pager, relTime, Spinner, Tabs } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import { toast } from '@/stores/app'
import type { ReportDetail, ReportRow } from '@/types'

export default function ReportsPage() {
  const { t, lang } = useI18n()
  const locale = lang === 'zh' ? 'zh-CN' : 'en-US'
  const [tab, setTab] = useState<'reports' | 'templates'>('reports')
  const [rows, setRows] = useState<ReportRow[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [q, setQ] = useState('')
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<ReportDetail | null>(null)
  const [confirmDel, setConfirmDel] = useState<ReportRow | null>(null)
  const pageSize = 20

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api.listReports({ q: q || undefined, page, page_size: pageSize })
      setRows(r.rows)
      setTotal(r.total)
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setLoading(false)
    }
  }, [q, page])

  useEffect(() => { if (tab === 'reports') void load() }, [tab, load])

  const open = async (id: string) => {
    try { setDetail(await api.getReport(id)) } catch (e) { toast.error((e as ApiError).message) }
  }

  const download = (r: ReportDetail) => {
    const blob = new Blob([r.content_md], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${r.title.replace(/[\\/:*?"<>|]/g, '_')}.md`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row wrap">
        <h1>{t('admin.reports.title')}</h1>
        <span className="spacer" />
        {tab === 'reports' && (
          <>
            <input className="input" style={{ width: 260 }} placeholder={t('admin.reports.searchPh')}
              value={q} onChange={(e) => { setQ(e.target.value); setPage(1) }} />
            <button className="btn" onClick={() => void load()}>{t('common.refresh')}</button>
          </>
        )}
      </div>

      <Tabs value={tab} onChange={setTab} items={[
        { key: 'reports', label: t('admin.reports.tabReports'), badge: total },
        { key: 'templates', label: t('admin.reports.tabTemplates') },
      ]} />

      {tab === 'templates' && <TemplatesPane />}

      {tab === 'reports' && (
        <div className="card">
          {loading && <div className="center" style={{ padding: 30 }}><Spinner text={t('admin.reports.loadingReports')} /></div>}
          {!loading && !rows.length && (
            <Empty icon="📄" text={q ? t('admin.reports.emptyNoMatch') : t('admin.reports.emptyNone')} />
          )}
          {!loading && !!rows.length && (
            <>
              <table className="table">
                <thead>
                  <tr><th>{t('admin.reports.colTitle')}</th><th>{t('admin.reports.colSections')}</th>
                    <th>{t('admin.reports.colViews')}</th><th>{t('admin.reports.colCreated')}</th>
                    <th style={{ width: 190 }}>{t('common.actions')}</th></tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id}>
                      <td style={{ fontWeight: 550, cursor: 'pointer' }} onClick={() => void open(r.id)}>{r.title}</td>
                      <td>
                        <div className="chips">
                          {(r.sections || []).slice(0, 4).map((s) => <span className="tag" key={s}>{s}</span>)}
                          {(r.sections || []).length > 4 && <span className="tag">+{r.sections.length - 4}</span>}
                        </div>
                      </td>
                      <td>{r.view_count}</td>
                      <td className="tiny muted">{relTime(r.created_at, t)}</td>
                      <td>
                        <div className="row" style={{ gap: 4 }}>
                          <button className="btn sm primary" onClick={() => void open(r.id)}>{t('admin.reports.read')}</button>
                          <button className="btn sm danger" onClick={() => setConfirmDel(r)}>{t('common.delete')}</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <Pager page={page} pageSize={pageSize} total={total} onChange={setPage} />
            </>
          )}
        </div>
      )}

      {detail && (
        <Drawer title={detail.title}
          sub={`${t('admin.reports.viewCount', { n: detail.view_count })} · ${new Date(detail.created_at).toLocaleString(locale)}`}
          onClose={() => setDetail(null)}>
          <div className="row mb12">
            <button className="btn sm" onClick={() => download(detail)}>⬇ {t('admin.reports.exportMd')}</button>
            <button className="btn sm" onClick={() => {
              void navigator.clipboard.writeText(detail.content_md)
                .then(() => toast.ok(t('common.copied')))
                .catch(() => toast.error(t('admin.reports.copyFailed')))
            }}>{t('admin.reports.copyAll')}</button>
          </div>
          <Markdown className="report-md" text={detail.content_md} />
        </Drawer>
      )}

      {confirmDel && (
        <Confirm title={t('admin.reports.confirmDeleteTitle')} danger okText={t('common.delete')}
          text={t('admin.reports.confirmDelete', { title: confirmDel.title })}
          onCancel={() => setConfirmDel(null)}
          onOk={async () => {
            try {
              await api.deleteReport(confirmDel.id)
              toast.ok(t('common.deleted'))
              setConfirmDel(null)
              void load()
            } catch (e) { toast.error((e as ApiError).message) }
          }} />
      )}
    </div>
  )
}

function TemplatesPane() {
  const { t } = useI18n()
  const [rows, setRows] = useState<{ id: string; name: string; source: string; usage_count: number; structure: unknown }[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.listReportTemplates().then(setRows)
      .catch((e: ApiError) => toast.error(e.message))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <div className="center" style={{ padding: 30 }}><Spinner text={t('admin.reports.loadingTemplates')} /></div>

  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="tiny muted">
        {t('admin.reports.templatesDesc')}
      </div>
      <div className="card">
        {!rows.length ? <Empty icon="🧩" text={t('admin.reports.emptyTemplates')} /> : (
          <table className="table">
            <thead><tr><th>{t('admin.reports.colTplName')}</th><th>{t('admin.reports.colSource')}</th>
              <th>{t('admin.reports.colUsage')}</th><th>{t('admin.reports.colStructure')}</th></tr></thead>
            <tbody>
              {rows.map((t2) => (
                <tr key={t2.id}>
                  <td style={{ fontWeight: 550 }}>{t2.name}</td>
                  <td>
                    <span className={`tag ${t2.source === 'manual' ? 'accent' : ''}`}>
                      {t2.source === 'manual' ? t('admin.reports.sourceManual') : t('admin.reports.sourceAuto')}
                    </span>
                  </td>
                  <td>{t2.usage_count}</td>
                  <td className="tiny muted mono truncate" style={{ maxWidth: 420 }}>
                    {Array.isArray((t2.structure as { sections?: string[] })?.sections)
                      ? ((t2.structure as { sections: string[] }).sections.join(' / '))
                      : JSON.stringify(t2.structure).slice(0, 160)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
