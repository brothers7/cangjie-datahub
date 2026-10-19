/** 运行日志：查询记录 / LLM 调用日志 / 审计日志。 */
import { SqlText } from '@/components/common/SqlText'
import { Fragment, useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '@/api/client'
import { Empty, Pager, Spinner, StatusTag, Tabs } from '@/components/common/ui'
import { LlmLogsTable } from '@/components/logs/LlmLogsTable'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'
import { useI18n } from '@/i18n'
import { auditActions, auditLabel, queryStatus } from '@/labels'
import { toast, useApp } from '@/stores/app'
import { useAuth } from '@/stores/auth'
import type { AuditLogRow, QueryRecordRow } from '@/types'

type Tab = 'queries' | 'llm' | 'audit'
const PAGE_SIZE = 20

export default function LogsPage() {
  const { t } = useI18n()
  const [tab, setTab] = useState<Tab>('queries')
  const isAdmin = useAuth((s) => s.user?.role === 'admin')
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row wrap">
        <h1>{t('admin.logs.title')}</h1>
        <span className="spacer" />
        <span className="tiny muted">{t('admin.logs.subtitle')}</span>
      </div>
      <Tabs value={tab} onChange={setTab} items={[
        { key: 'queries', label: t('admin.logs.tabQueries') },
        // LLM 调用日志与审计日志接口仅管理员可读，非管理员隐藏对应页签
        ...(isAdmin ? [
          { key: 'llm' as Tab, label: t('admin.logs.tabLlm') },
          { key: 'audit' as Tab, label: t('admin.logs.tabAudit') },
        ] : []),
      ]} />
      {tab === 'queries' && <QueriesPane />}
      {tab === 'llm' && <LlmPane />}
      {tab === 'audit' && <AuditPane />}
    </div>
  )
}

// ---------------- 查询记录 ----------------

function QueriesPane() {
  const { t, lang } = useI18n()
  const locale = lang === 'zh' ? 'zh-CN' : 'en-US'
  const datasources = useApp((s) => s.datasources)
  const loadDatasources = useApp((s) => s.loadDatasources)
  const [rows, setRows] = useState<QueryRecordRow[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [status, setStatus] = useState('')
  const [dsId, setDsId] = useState('')
  const [mine, setMine] = useState(false)
  const [q, setQ] = useState('')
  const dq = useDebouncedValue(q)  // 搜索防抖后再进 load 依赖
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<string | null>(null)

  useEffect(() => { void loadDatasources().catch(() => undefined) }, [loadDatasources])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api.listQueryRecords({
        mine, status: status || undefined,
        datasource_id: dsId || undefined, q: dq || undefined, page, page_size: PAGE_SIZE,
      })
      setRows(r.rows)
      setTotal(r.total)
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setLoading(false)
    }
  }, [mine, status, dsId, dq, page])

  useEffect(() => { void load() }, [load])

  return (
    <div className="card">
      <div className="card-head">
        <h2>{t('admin.logs.tabQueries')}</h2>
        <span className="spacer" />
        <input className="input" style={{ width: 200 }} placeholder={t('admin.logs.searchQuestionPh')}
          value={q} onChange={(e) => { setQ(e.target.value); setPage(1) }} />
        <select className="select" style={{ width: 130 }} value={status}
          onChange={(e) => { setStatus(e.target.value); setPage(1) }}>
          <option value="">{t('common.allStatus')}</option>
          <option value="success">{queryStatus('success', lang)?.text}</option>
          <option value="error">{queryStatus('error', lang)?.text}</option>
          <option value="blocked">{t('admin.logs.blocked')}</option>
        </select>
        <select className="select" style={{ width: 170 }} value={dsId}
          onChange={(e) => { setDsId(e.target.value); setPage(1) }}>
          <option value="">{t('admin.logs.allDs')}</option>
          {datasources.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
        <label className="row tiny" style={{ gap: 5 }}>
          <input type="checkbox" checked={mine} onChange={(e) => { setMine(e.target.checked); setPage(1) }} />
          {t('admin.logs.mineOnly')}
        </label>
      </div>

      <div style={{ overflow: 'auto' }}>
        <table className="table">
          <thead>
            <tr><th>{t('common.time')}</th><th>{t('admin.logs.colQuestion')}</th><th>{t('admin.logs.colDatasource')}</th>
              <th>{t('common.status')}</th><th>{t('admin.logs.colRows')}</th>
              <th>{t('admin.logs.colDuration')}</th><th>{t('admin.logs.colCache')}</th><th>{t('admin.logs.colChart')}</th>
              <th style={{ width: 90 }}>{t('common.actions')}</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <Fragment key={r.id}>
                <tr>
                  <td className="tiny">{new Date(r.created_at).toLocaleString(locale)}</td>
                  <td className="truncate" style={{ maxWidth: 320 }} title={r.question}>{r.question}</td>
                  <td className="tiny">
                    {datasources.find((d) => d.id === r.datasource_id)?.name || '-'}
                  </td>
                  <td><StatusTag value={r.status} label={queryStatus(r.status, lang)?.text} cls={queryStatus(r.status, lang)?.cls} /></td>
                  <td>{r.row_count}</td>
                  <td>{r.duration_ms}ms</td>
                  <td>{r.cache_hit ? <span className="tag ok">{t('admin.logs.cacheHit')}</span> : <span className="muted tiny">-</span>}</td>
                  <td className="tiny">{r.chart_type || '-'}</td>
                  <td>
                    <button className="btn sm ghost"
                      onClick={() => setExpanded(expanded === r.id ? null : r.id)}>
                      {expanded === r.id ? t('common.collapse') : t('common.detail')}
                    </button>
                  </td>
                </tr>
                {expanded === r.id && (
                  <tr>
                    <td colSpan={9} style={{ background: 'var(--bg-sunken)' }}>
                      <div className="col tiny" style={{ gap: 8, padding: '4px 0' }}>
                        {r.rewritten_question && r.rewritten_question !== r.question && (
                          <div><span className="muted">{t('admin.logs.rewritten')}</span>{r.rewritten_question}</div>
                        )}
                        {r.error_brief && (
                          <div style={{ color: 'var(--danger)' }}>
                            <span className="muted">{t('admin.logs.errorPrefix')}</span>{r.error_brief}
                          </div>
                        )}
                        {r.sql_text && (
                          <div>
                            <div className="muted mb8">{t('admin.logs.execSql')}</div>
                            <div className="sqlbox">
                              <pre><SqlText code={r.sql_text} /></pre>
                            </div>
                          </div>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
        {loading && <div className="center" style={{ padding: 24 }}><Spinner /></div>}
        {!loading && !rows.length && <Empty icon="🔎" text={t('admin.logs.emptyQueries')} />}
      </div>
      <Pager page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} />
    </div>
  )
}

// ---------------- LLM 调用 ----------------

function LlmPane() {
  const { t } = useI18n()
  return (
    <LlmLogsTable
      title={t('admin.logs.llmTitle')}
      allScenesText={t('admin.logs.allScenes')}
      modelColText={t('admin.logs.colModel')}
      errorColText={t('admin.logs.colErrorBrief')}
      emptyText={t('admin.logs.emptyLlm')}
      headerExtra={(rows) => (
        <span className="tag">
          {t('admin.logs.pageTokens', {
            n: rows.reduce((a, r) => a + r.input_tokens + r.output_tokens, 0).toLocaleString(),
          })}
        </span>
      )}
    />
  )
}

// ---------------- 审计日志 ----------------

function AuditPane() {
  const { t, lang } = useI18n()
  const locale = lang === 'zh' ? 'zh-CN' : 'en-US'
  const actions = auditActions(lang)
  const [rows, setRows] = useState<AuditLogRow[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [action, setAction] = useState('')
  const [username, setUsername] = useState('')
  const dUsername = useDebouncedValue(username)  // 搜索防抖后再进 load 依赖
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api.listAuditLogs({
        action: action || undefined, username: dUsername || undefined, page, page_size: PAGE_SIZE,
      })
      setRows(r.rows)
      setTotal(r.total)
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setLoading(false)
    }
  }, [action, dUsername, page])

  useEffect(() => { void load() }, [load])

  return (
    <div className="card">
      <div className="card-head">
        <h2>{t('admin.logs.auditTitle')}</h2>
        <span className="spacer" />
        <input className="input" style={{ width: 160 }} placeholder={t('admin.logs.usernamePh')}
          value={username} onChange={(e) => { setUsername(e.target.value); setPage(1) }} />
        <select className="select" style={{ width: 140 }} value={action}
          onChange={(e) => { setAction(e.target.value); setPage(1) }}>
          <option value="">{t('admin.logs.allActions')}</option>
          {Object.entries(actions).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </div>
      <div style={{ overflow: 'auto' }}>
        <table className="table">
          <thead><tr><th>{t('common.time')}</th><th>{t('common.user')}</th><th>{t('common.actions')}</th>
            <th>{t('admin.logs.colObject')}</th><th>{t('common.detail')}</th><th>IP</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="tiny">{new Date(r.created_at).toLocaleString(locale)}</td>
                <td>{r.username}</td>
                <td><span className="tag">{auditLabel(r.action, lang)}</span></td>
                <td className="tiny">{r.resource_type}{r.resource_id ? ` · ${r.resource_id.slice(0, 8)}` : ''}</td>
                <td className="tiny muted truncate" style={{ maxWidth: 320 }}>
                  {Object.keys(r.detail || {}).length ? JSON.stringify(r.detail) : '-'}
                </td>
                <td className="tiny mono">{r.ip || '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {loading && <div className="center" style={{ padding: 24 }}><Spinner /></div>}
        {!loading && !rows.length && <Empty icon="🛡" text={t('admin.logs.emptyAudit')} />}
      </div>
      <Pager page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} />
    </div>
  )
}
