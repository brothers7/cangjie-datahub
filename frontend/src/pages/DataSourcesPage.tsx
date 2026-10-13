/** 数据源管理：连接配置 / 连通性测试 / 扫描任务 / 元数据浏览与人工修正。 */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '@/api/client'
import {
  Confirm, Empty, Field, fmtTime, Modal, relTime, Spinner, StatusTag, Tabs,
} from '@/components/common/ui'
import { useApp, toast } from '@/stores/app'
import { useI18n } from '@/i18n'
import type { Datasource, MetaColumnRow, MetaTableRow, ScanJob } from '@/types'

const DB_TYPES: Record<string, string> = {
  mysql: 'MySQL', postgresql: 'PostgreSQL', clickhouse: 'ClickHouse', sqlite: 'SQLite',
  hive: 'Hive', spark: 'Spark SQL', doris: 'Doris', starrocks: 'StarRocks',
}
const THRIFT_TYPES = ['hive', 'spark']  // HiveServer2 Thrift 协议，默认端口 10000
const DEFAULT_PORTS: Record<string, number> = {
  mysql: 3306, postgresql: 5432, clickhouse: 8123, hive: 10000, spark: 10000,
  doris: 9030, starrocks: 9030,
}
const LAYERS = ['ods', 'dwd', 'dws', 'ads', 'dim', 'unknown']
const ROLES = ['metric', 'dimension', 'time', 'unknown']

const blankForm = () => ({
  name: '', db_type: 'mysql', host: 'localhost', port: 3306, database_name: '',
  username: '', password: '', scan_cron: '',
  table_whitelist: '', table_blacklist: '',
  sched_write_enabled: false,
  readonly_enforced: true,
})

export default function DataSourcesPage() {
  const { t } = useI18n()
  const loadDatasources = useApp((s) => s.loadDatasources)
  const [rows, setRows] = useState<Datasource[]>([])
  const [active, setActive] = useState<Datasource | null>(null)
  const [loading, setLoading] = useState(true)
  const [form, setForm] = useState<ReturnType<typeof blankForm> | null>(null)
  const [editing, setEditing] = useState<Datasource | null>(null)
  const [confirmDel, setConfirmDel] = useState<Datasource | null>(null)
  const [testing, setTesting] = useState<string>('')
  const [tab, setTab] = useState<'tables' | 'jobs'>('tables')

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const list = await api.listDatasources()
      setRows(list)
      setActive((prev) => list.find((d) => d.id === prev?.id) || list[0] || null)
      await loadDatasources(true).catch(() => undefined)
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setLoading(false)
    }
  }, [loadDatasources])

  useEffect(() => { void refresh() }, [refresh])

  const openCreate = () => { setEditing(null); setForm(blankForm()) }
  const openEdit = (ds: Datasource) => {
    setEditing(ds)
    setForm({
      name: ds.name, db_type: ds.db_type, host: ds.host, port: ds.port,
      database_name: ds.database_name, username: ds.username, password: '',
      scan_cron: ds.scan_cron || '',
      table_whitelist: (ds.table_whitelist || []).join(','),
      table_blacklist: (ds.table_blacklist || []).join(','),
      sched_write_enabled: ds.sched_write_enabled,
      readonly_enforced: ds.readonly_enforced,
    })
  }

  const save = async () => {
    if (!form) return
    const payload: Record<string, unknown> = {
      name: form.name.trim(),
      db_type: form.db_type,
      host: form.host.trim(),
      port: Number(form.port) || 0,
      database_name: form.database_name.trim(),
      username: form.username.trim(),
      extra_params: {},
      table_whitelist: form.table_whitelist.split(',').map((s) => s.trim()).filter(Boolean),
      table_blacklist: form.table_blacklist.split(',').map((s) => s.trim()).filter(Boolean),
      scan_cron: form.scan_cron.trim() || null,
      sched_write_enabled: form.sched_write_enabled,
      readonly_enforced: form.readonly_enforced,
    }
    if (form.password) payload.password = form.password
    try {
      if (editing) {
        await api.updateDatasource(editing.id, payload)
        toast.ok(t('ds.updated'))
      } else {
        if (!form.password) delete payload.password
        await api.createDatasource(payload)
        toast.ok(t('ds.created'))
      }
      setForm(null)
      setEditing(null)
      await refresh()
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }

  const test = async (ds: Datasource) => {
    setTesting(ds.id)
    try {
      const r = await api.testDatasource(ds.id)
      r.ok ? toast.ok(t('ds.testOk', { msg: r.message })) : toast.error(t('ds.testFail', { msg: r.message }))
      await refresh()
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setTesting('')
    }
  }

  const scan = async (ds: Datasource, mode: 'full' | 'incremental') => {
    try {
      await api.scanDatasource(ds.id, mode)
      toast.ok(mode === 'full' ? t('ds.scanFullStarted') : t('ds.scanIncStarted'))
      setTab('jobs')
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }

  const remove = async (ds: Datasource) => {
    try {
      await api.deleteDatasource(ds.id)
      toast.ok(t('common.deleted'))
      setConfirmDel(null)
      await refresh()
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }

  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row">
        <h1>{t('ds.title')}</h1>
        <span className="spacer" />
        <button className="btn primary" onClick={openCreate}>{t('ds.add')}</button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '300px minmax(0, 1fr)', gap: 16, alignItems: 'start' }}>
        {/* 左：数据源列表 */}
        <div className="card" style={{ overflow: 'hidden' }}>
          <div className="card-head"><h2>{t('ds.connectedList')}</h2><span className="spacer" /><span className="tag">{rows.length}</span></div>
          {loading && <div className="center" style={{ padding: 30 }}><Spinner /></div>}
          {!loading && rows.length === 0 && <Empty text={t('ds.empty')} icon="🗄" />}
          {rows.map((ds) => (
            <div key={ds.id} className={`list-row ${active?.id === ds.id ? 'active' : ''}`}
              onClick={() => setActive(ds)}>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="row" style={{ gap: 6 }}>
                  <span className="truncate" style={{ fontWeight: 550 }}>{ds.name}</span>
                  {ds.is_builtin && <span className="tag">{t('ds.builtin')}</span>}
                </div>
                <div className="tiny muted truncate">
                  {DB_TYPES[ds.db_type] || ds.db_type} · {ds.db_type === 'sqlite' ? ds.database_name : `${ds.host}:${ds.port}`}
                </div>
              </div>
              <StatusTag value={ds.status} />
            </div>
          ))}
        </div>

        {/* 右：详情 */}
        {!active ? (
          <div className="card"><Empty text={t('ds.selectHint')} icon="←" /></div>
        ) : (
          <div className="col" style={{ gap: 16 }}>
            <div className="card">
              <div className="card-head">
                <h2>{active.name}</h2>
                <StatusTag value={active.status} />
                <span className="spacer" />
                <button className="btn sm" disabled={testing === active.id}
                  onClick={() => void test(active)}>
                  {testing === active.id ? <Spinner /> : t('ds.testConnection')}
                </button>
                <button className="btn sm" onClick={() => openEdit(active)}>{t('common.edit')}</button>
                {!active.is_builtin && (
                  <button className="btn sm danger" onClick={() => setConfirmDel(active)}>{t('common.delete')}</button>
                )}
              </div>
              <div className="card-body">
                <div className="grid3">
                  <Info label={t('ds.dbType')} value={DB_TYPES[active.db_type] || active.db_type} />
                  <Info label={t('ds.address')}
                    value={active.db_type === 'sqlite' ? active.database_name : `${active.host}:${active.port}`} />
                  <Info label={t('ds.database')} value={active.database_name || '-'} />
                  <Info label={t('ds.account')} value={active.username || '-'} />
                  <Info label={t('ds.lastTest')} value={fmtTime(active.last_test_at)} />
                  <Info label={t('ds.lastScan')} value={relTime(active.last_scan_at, t)} />
                  <Info label={t('ds.scanCron')} value={active.scan_cron || t('ds.notConfigured')} />
                  <Info label={t('ds.whitelist')} value={active.table_whitelist?.length ? active.table_whitelist.join(', ') : t('common.all')} />
                  <Info label={t('ds.blacklist')} value={active.table_blacklist?.length ? active.table_blacklist.join(', ') : t('common.none')} />
                </div>
                {active.status_message && (
                  <div className={`tag ${active.status === 'error' ? 'danger' : 'ok'}`}
                    style={{ marginTop: 12, whiteSpace: 'normal', display: 'block' }}>
                    {active.status_message}
                  </div>
                )}
                <div className="row mt12">
                  <button className="btn primary sm" onClick={() => void scan(active, 'full')}>
                    {t('ds.fullScan')}
                  </button>
                  <button className="btn sm" onClick={() => void scan(active, 'incremental')}>
                    {t('ds.incScan')}
                  </button>
                  <span className="tiny muted">
                    {t('ds.scanHint')}
                  </span>
                </div>
              </div>
            </div>

            <div className="card">
              <div className="card-head" style={{ paddingBottom: 0, borderBottom: 'none' }}>
                <Tabs value={tab} onChange={setTab} items={[
                  { key: 'tables', label: t('ds.tabMetadata') },
                  { key: 'jobs', label: t('ds.tabJobs') },
                ]} />
              </div>
              <div className="card-body" style={{ paddingTop: 0 }}>
                {tab === 'tables'
                  ? <TablesPane ds={active} />
                  : <JobsPane ds={active} />}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 新增/编辑弹窗 */}
      {form && (
        <Modal title={editing ? t('ds.editTitle') : t('ds.addTitle')} width={640}
          onClose={() => { setForm(null); setEditing(null) }}
          footer={<>
            <button className="btn" onClick={() => { setForm(null); setEditing(null) }}>{t('common.cancel')}</button>
            <button className="btn primary" disabled={!form.name}
              onClick={() => void save()}>{editing ? t('common.save') : t('common.create')}</button>
          </>}>
          <div className="col" style={{ gap: 14 }}>
            <div className="grid2">
              <Field label={`${t('common.name')} *`}>
                <input className="input" value={form.name} placeholder={t('ds.namePlaceholder')}
                  onChange={(e) => setForm({ ...form, name: e.target.value })} />
              </Field>
              <Field label={`${t('ds.dbType')} *`}>
                <select className="select" value={form.db_type}
                  onChange={(e) => setForm({
                    ...form, db_type: e.target.value,
                    port: DEFAULT_PORTS[e.target.value] ?? 0,
                  })}>
                  {Object.entries(DB_TYPES).map(([k, v]) => (
                    <option key={k} value={k}>{v}</option>
                  ))}
                </select>
              </Field>
              {form.db_type !== 'sqlite' && (
                <>
                  <Field label={t('ds.host')}>
                    <input className="input" value={form.host}
                      onChange={(e) => setForm({ ...form, host: e.target.value })} />
                  </Field>
                  <Field label={t('ds.port')}>
                    <input className="input" type="number" value={form.port}
                      onChange={(e) => setForm({ ...form, port: Number(e.target.value) })} />
                  </Field>
                  <Field label={t('ds.account')} hint={THRIFT_TYPES.includes(form.db_type) ? t('ds.hintThriftUser') : undefined}>
                    <input className="input" value={form.username}
                      onChange={(e) => setForm({ ...form, username: e.target.value })} />
                  </Field>
                  <Field label={t('ds.password')} hint={editing ? t('common.leaveEmptyToKeep') : THRIFT_TYPES.includes(form.db_type) ? t('ds.hintPasswordThrift') : t('ds.hintPassword')}>
                    <input className="input" type="password" value={form.password}
                      placeholder={editing ? '••••••' : ''}
                      onChange={(e) => setForm({ ...form, password: e.target.value })} />
                  </Field>
                </>
              )}
              <Field label={form.db_type === 'sqlite' ? t('ds.filePath') : t('ds.dbName')}>
                <input className="input" value={form.database_name}
                  placeholder={form.db_type === 'sqlite' ? '/data/warehouse.db' : 'warehouse'}
                  onChange={(e) => setForm({ ...form, database_name: e.target.value })} />
              </Field>
              <Field label={t('ds.cronLabel')} hint={t('ds.cronHint')}>
                <input className="input mono" value={form.scan_cron} placeholder="0 */6 * * *"
                  onChange={(e) => setForm({ ...form, scan_cron: e.target.value })} />
              </Field>
              <Field label={t('ds.schedWrite')} hint={t('ds.schedWriteHint')}>
                <label className="tiny row" style={{ gap: 6, cursor: 'pointer', padding: '6px 0' }}>
                  <input type="checkbox" checked={form.sched_write_enabled}
                    onChange={(e) => setForm({ ...form, sched_write_enabled: e.target.checked })} />
                  {t('ds.schedWriteLabel')}
                </label>
              </Field>
              <Field label={t('ds.readonly')} hint={t('ds.readonlyHint')}>
                <label className="tiny row" style={{ gap: 6, cursor: 'pointer', padding: '6px 0' }}>
                  <input type="checkbox" checked={form.readonly_enforced}
                    onChange={(e) => setForm({ ...form, readonly_enforced: e.target.checked })} />
                  {t('ds.readonlyLabel')}
                </label>
              </Field>
              <Field label={t('ds.whitelist')} hint={t('ds.whitelistHint')}>
                <input className="input" value={form.table_whitelist} placeholder="dwd_,dws_,ads_"
                  onChange={(e) => setForm({ ...form, table_whitelist: e.target.value })} />
              </Field>
              <Field label={t('ds.blacklist')} hint={t('ds.blacklistHint')}>
                <input className="input" value={form.table_blacklist} placeholder="tmp_,_bak"
                  onChange={(e) => setForm({ ...form, table_blacklist: e.target.value })} />
              </Field>
            </div>
            <div className="tiny muted">
              {t('ds.securityNote')}
            </div>
          </div>
        </Modal>
      )}

      {confirmDel && (
        <Confirm title={t('ds.deleteTitle')} danger okText={t('common.delete')}
          text={<>{t('ds.confirmDeletePre')}<b>{confirmDel.name}</b>{t('ds.confirmDeleteSuffix')}<br />
            {t('ds.confirmDeleteWarn')}</>}
          onCancel={() => setConfirmDel(null)} onOk={() => void remove(confirmDel)} />
      )}
    </div>
  )
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="tiny muted">{label}</div>
      <div style={{ fontSize: 13 }} className="truncate" title={value}>{value}</div>
    </div>
  )
}

// ---------------- 元数据浏览 ----------------

function TablesPane({ ds }: { ds: Datasource }) {
  const { t } = useI18n()
  const [tables, setTables] = useState<MetaTableRow[]>([])
  const [loading, setLoading] = useState(false)
  const [selected, setSelected] = useState<MetaTableRow | null>(null)
  const [columns, setColumns] = useState<MetaColumnRow[]>([])
  const [colLoading, setColLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    setSelected(null)
    api.listTables(ds.id)
      .then(setTables)
      .catch((e: ApiError) => toast.error(e.message))
      .finally(() => setLoading(false))
  }, [ds.id])

  const openTable = async (t: MetaTableRow) => {
    setSelected(t)
    setColLoading(true)
    try {
      setColumns(await api.listColumns(t.id))
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setColLoading(false)
    }
  }

  const saveColumn = async (c: MetaColumnRow, role: string, comment: string) => {
    try {
      await api.updateColumn(c.id, { semantic_role: role, comment })
      toast.ok(t('ds.columnSaved'))
      setColumns((prev) => prev.map((x) => x.id === c.id ? { ...x, semantic_role: role as MetaColumnRow['semantic_role'], comment, source: 'manual' } : x))
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }

  const setLayer = async (tb: MetaTableRow, layer: string) => {
    try {
      await api.updateTable(tb.id, { layer })
      toast.ok(t('ds.layerUpdated'))
      setTables((prev) => prev.map((x) => x.id === tb.id ? { ...x, layer, source: 'manual' } : x))
      setSelected((s) => s && s.id === tb.id ? { ...s, layer } : s)
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }

  if (loading) return <div className="center" style={{ padding: 30 }}><Spinner text={t('ds.loadingMeta')} /></div>
  if (!tables.length) {
    return <Empty icon="📋" text={t('ds.noMeta')} />
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1.2fr)', gap: 14 }}>
      <div style={{ border: '1px solid var(--border)', borderRadius: 8, overflow: 'auto', maxHeight: 460 }}>
        <table className="table">
          <thead><tr><th>{t('ds.tableName')}</th><th>{t('ds.layer')}</th><th>{t('ds.comment')}</th></tr></thead>
          <tbody>
            {tables.map((t) => (
              <tr key={t.id} style={{ cursor: 'pointer', background: selected?.id === t.id ? 'var(--accent-soft)' : undefined }}
                onClick={() => void openTable(t)}>
                <td className="mono" style={{ fontSize: 12.5 }}>{t.table_name}</td>
                <td><span className="tag">{t.layer?.toUpperCase()}</span></td>
                <td className="dim truncate" style={{ maxWidth: 180 }}>{t.comment || '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ border: '1px solid var(--border)', borderRadius: 8, overflow: 'auto', maxHeight: 460 }}>
        {!selected ? <Empty text={t('ds.selectTable')} icon="←" /> : colLoading ? (
          <div className="center" style={{ padding: 30 }}><Spinner /></div>
        ) : (
          <table className="table">
            <thead>
              <tr><th>{t('ds.column')}</th><th>{t('common.type')}</th><th>{t('ds.semanticRole')}</th><th>{t('ds.comment')}</th></tr>
            </thead>
            <tbody>
              <tr>
                <td colSpan={4} style={{ background: 'var(--bg-sunken)' }}>
                  <div className="row">
                    <span className="mono" style={{ fontSize: 12.5 }}>{selected.table_name}</span>
                    <span className="tiny muted">{t('ds.layerFix')}</span>
                    <select className="select" style={{ width: 110, padding: '2px 6px' }}
                      value={selected.layer} onChange={(e) => void setLayer(selected, e.target.value)}>
                      {LAYERS.map((l) => <option key={l} value={l}>{l.toUpperCase()}</option>)}
                    </select>
                  </div>
                </td>
              </tr>
              {columns.map((c) => (
                <ColumnRow key={c.id} col={c} onSave={saveColumn} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

function ColumnRow({ col, onSave }: {
  col: MetaColumnRow
  onSave: (c: MetaColumnRow, role: string, comment: string) => void
}) {
  const { t } = useI18n()
  const [role, setRole] = useState(col.semantic_role)
  const [comment, setComment] = useState(col.comment || '')
  const dirty = role !== col.semantic_role || comment !== (col.comment || '')

  return (
    <tr>
      <td className="mono" style={{ fontSize: 12.5 }}>
        {col.column_name}
        {col.is_primary_key && <span className="tag" style={{ marginLeft: 5 }}>PK</span>}
      </td>
      <td className="tiny muted">{col.data_type}</td>
      <td>
        <select className="select" style={{ width: 100, padding: '2px 6px', fontSize: 12 }}
          value={role} onChange={(e) => setRole(e.target.value as MetaColumnRow['semantic_role'])}>
          {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
      </td>
      <td>
        <div className="row" style={{ gap: 4 }}>
          <input className="input" style={{ padding: '2px 7px', fontSize: 12 }} value={comment}
            onChange={(e) => setComment(e.target.value)} />
          {dirty && (
            <button className="btn sm primary" onClick={() => onSave(col, role, comment)}>{t('ds.saveShort')}</button>
          )}
        </div>
      </td>
    </tr>
  )
}

// ---------------- 扫描任务 ----------------

function JobsPane({ ds }: { ds: Datasource }) {
  const { t } = useI18n()
  const [jobs, setJobs] = useState<ScanJob[]>([])
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    try {
      setJobs(await api.listScanJobs(ds.id, 30))
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setLoading(false)
    }
  }, [ds.id])

  useEffect(() => {
    setLoading(true)
    void load()
    // 有运行中的任务时轮询刷新进度
    const timer = setInterval(() => {
      setJobs((cur) => {
        if (cur.some((j) => j.status === 'running')) void load()
        return cur
      })
    }, 3000)
    return () => clearInterval(timer)
  }, [load])

  if (loading && !jobs.length) return <div className="center" style={{ padding: 30 }}><Spinner /></div>
  if (!jobs.length) return <Empty text={t('ds.noJobs')} icon="⟳" />

  return (
    <div style={{ overflow: 'auto', maxHeight: 400 }}>
      <table className="table">
        <thead>
          <tr>
            <th>{t('ds.startTime')}</th><th>{t('ds.mode')}</th><th>{t('common.status')}</th><th>{t('ds.tablesCount')}</th>
            <th>{t('ds.createdCount')}</th><th>{t('ds.updatedCount')}</th><th>{t('ds.manualProtected')}</th><th>{t('ds.triggeredBy')}</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id}>
              <td className="tiny">{fmtTime(j.started_at)}</td>
              <td>{j.mode === 'full' ? t('ds.modeFull') : t('ds.modeInc')}</td>
              <td>
                <StatusTag value={j.status} />
                {j.status === 'running' && <div className="bar mt8"><i style={{ width: `${j.progress}%` }} /></div>}
              </td>
              <td className="num">{j.tables_total}</td>
              <td className="num">{j.entities_created}</td>
              <td className="num">{j.entities_updated}</td>
              <td className="num">{j.entities_skipped_manual}</td>
              <td className="tiny muted">{j.triggered_by?.startsWith('user:') ? t('ds.manual') : j.triggered_by}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {jobs.some((j) => j.error_log?.length) && (
        <div className="card" style={{ margin: 12, borderColor: 'var(--warn)' }}>
          <div className="card-body">
            <div className="tiny dim mb8">{t('ds.scanWarnings')}</div>
            {jobs.filter((j) => j.error_log?.length).slice(0, 1).map((j) => (
              <div key={j.id} className="tiny mono" style={{ whiteSpace: 'pre-wrap' }}>
                {j.error_log.slice(0, 6).join('\n')}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
