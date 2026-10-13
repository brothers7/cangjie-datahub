/** 用户与权限（admin）：用户管理 / 数据源授权 / 敏感数据脱敏规则。 */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '@/api/client'
import { Confirm, Empty, Field, Modal, Pager, Spinner, StatusTag, Tabs, fmtTime } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import { toast, useApp } from '@/stores/app'
import { useAuth } from '@/stores/auth'
import type { Datasource, MaskingRuleRow, Role, UserRow } from '@/types'

type Tab = 'users' | 'masking'
const PAGE_SIZE = 20

const ROLES = ['admin', 'analyst', 'viewer'] as const
const USER_STATUSES = ['active', 'disabled', 'deleted'] as const
const STRATEGIES = ['mask_partial', 'mask_hash', 'mask_null'] as const

export default function UsersPage() {
  const { t } = useI18n()
  const [tab, setTab] = useState<Tab>('users')
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row wrap">
        <h1>{t('admin.users.title')}</h1>
        <span className="spacer" />
        <span className="tiny muted">{t('admin.users.subtitle')}</span>
      </div>
      <Tabs value={tab} onChange={setTab} items={[
        { key: 'users', label: t('admin.users.tabUsers') },
        { key: 'masking', label: t('admin.users.tabMasking') },
      ]} />
      {tab === 'users' && <UsersPane />}
      {tab === 'masking' && <MaskingPane />}
    </div>
  )
}

// ---------------- 用户 ----------------

function UsersPane() {
  const { t } = useI18n()
  const me = useAuth((s) => s.user)
  const datasources = useApp((s) => s.datasources)
  const loadDatasources = useApp((s) => s.loadDatasources)
  const [users, setUsers] = useState<UserRow[]>([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)
  const [editing, setEditing] = useState<UserRow | null>(null)
  const [creating, setCreating] = useState(false)
  const [permUser, setPermUser] = useState<UserRow | null>(null)

  useEffect(() => { void loadDatasources().catch(() => undefined) }, [loadDatasources])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setUsers(await api.listUsers())
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const paged = users.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  const roleLabel = (r: string) => (ROLES as readonly string[]).includes(r) ? t(`admin.users.role.${r}`) : r
  const statusLabel = (s: string) => (USER_STATUSES as readonly string[]).includes(s) ? t(`admin.users.userStatus.${s}`) : s

  return (
    <div className="card">
      <div className="row" style={{ padding: '12px 16px' }}>
        <span className="tiny muted">{t('admin.users.userCount', { n: users.length })}</span>
        <span className="spacer" />
        <button className="btn primary sm" onClick={() => setCreating(true)}>＋ {t('admin.users.createUser')}</button>
      </div>
      {loading ? <Spinner text={t('common.loading')} /> : users.length === 0 ? <Empty text={t('admin.users.emptyUsers')} /> : (
        <>
          <table className="table">
            <thead>
              <tr><th>{t('admin.users.colUsername')}</th><th>{t('admin.users.colDisplayName')}</th>
                <th>{t('admin.users.colRole')}</th><th>{t('common.status')}</th>
                <th>{t('admin.users.colCreatedAt')}</th><th style={{ width: 220 }}>{t('common.actions')}</th></tr>
            </thead>
            <tbody>
              {paged.map((u) => (
                <tr key={u.id}>
                  <td>{u.username}{u.id === me?.id && <span className="tiny muted">{t('admin.users.me')}</span>}</td>
                  <td>{u.display_name}</td>
                  <td><StatusTag value={u.role} label={roleLabel(u.role)} cls={u.role === 'admin' ? 'accent' : ''} /></td>
                  <td><StatusTag value={u.status} label={statusLabel(u.status)} /></td>
                  <td className="tiny muted">{fmtTime(u.created_at)}</td>
                  <td>
                    <div className="row" style={{ gap: 6 }}>
                      <button className="btn sm" onClick={() => setEditing(u)}>{t('common.edit')}</button>
                      <button className="btn sm" onClick={() => setPermUser(u)}>{t('admin.users.permBtn')}</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pager page={page} pageSize={PAGE_SIZE} total={users.length} onChange={setPage} />
        </>
      )}
      {creating && <UserFormModal onClose={() => setCreating(false)} onSaved={() => { setCreating(false); void load() }} />}
      {editing && <UserFormModal user={editing} isMe={editing.id === me?.id}
        onClose={() => setEditing(null)} onSaved={() => { setEditing(null); void load() }} />}
      {permUser && <PermModal user={permUser} datasources={datasources}
        onClose={() => setPermUser(null)} />}
    </div>
  )
}

function UserFormModal({ user, isMe, onClose, onSaved }: {
  user?: UserRow; isMe?: boolean; onClose: () => void; onSaved: () => void
}) {
  const { t } = useI18n()
  const [username, setUsername] = useState(user?.username ?? '')
  const [displayName, setDisplayName] = useState(user?.display_name ?? '')
  const [role, setRole] = useState(user?.role ?? 'analyst')
  const [status, setStatus] = useState(user?.status ?? 'active')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)

  const save = async () => {
    setBusy(true)
    try {
      if (user) {
        const body: Record<string, unknown> = { display_name: displayName, status }
        if (!isMe) body.role = role          // 不允许把自己降级/禁用导致无人可管
        if (password) body.password = password
        await api.updateUser(user.id, body)
        toast.ok(t('common.saved'))
      } else {
        if (!username.trim() || !password) { toast.error(t('admin.users.requireUserPwd')); return }
        await api.createUser({ username: username.trim(), password, display_name: displayName || undefined, role })
        toast.ok(t('admin.users.userCreated'))
      }
      onSaved()
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={user ? t('admin.users.editTitle', { name: user.username }) : t('admin.users.newTitle')} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>{t('common.cancel')}</button>
        <button className="btn primary" disabled={busy} onClick={() => void save()}>{busy ? t('admin.users.saving') : t('common.save')}</button>
      </>}>
      <div className="col" style={{ gap: 12 }}>
        {!user && (
          <Field label={t('admin.users.colUsername')}><input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus /></Field>
        )}
        <Field label={t('admin.users.colDisplayName')}><input value={displayName} onChange={(e) => setDisplayName(e.target.value)} /></Field>
        <Field label={user ? t('admin.users.newPassword') : t('admin.users.initPassword')}
          hint={t('admin.users.pwdHint')}>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        {(!user || !isMe) && (
          <Field label={t('admin.users.colRole')}>
            <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {ROLES.map((k) => <option key={k} value={k}>{t(`admin.users.role.${k}`)}</option>)}
            </select>
          </Field>
        )}
        {user && (
          <Field label={t('common.status')} hint={isMe ? t('admin.users.statusHintSelf') : undefined}>
            <select value={status} disabled={isMe} onChange={(e) => setStatus(e.target.value)}>
              <option value="active">{t('admin.users.userStatus.active')}</option>
              <option value="disabled">{t('admin.users.userStatus.disabled')}</option>
            </select>
          </Field>
        )}
      </div>
    </Modal>
  )
}

function PermModal({ user, datasources, onClose }: {
  user: UserRow; datasources: Datasource[]; onClose: () => void
}) {
  const { t } = useI18n()
  const [grants, setGrants] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.getPermissions(user.id)
      .then((rows) => {
        const m: Record<string, boolean> = {}
        for (const r of rows) m[r.datasource_id] = r.can_query
        setGrants(m)
      })
      .catch((e) => toast.error((e as ApiError).message))
      .finally(() => setLoading(false))
  }, [user.id])

  const save = async () => {
    setBusy(true)
    try {
      await api.setPermissions(user.id,
        datasources.map((d) => ({ datasource_id: d.id, can_query: !!grants[d.id] })))
      toast.ok(t('admin.users.permSaved'))
      onClose()
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={t('admin.users.permTitle', { name: user.display_name })} onClose={onClose} width={480}
      footer={<>
        <button className="btn" onClick={onClose}>{t('common.cancel')}</button>
        <button className="btn primary" disabled={busy} onClick={() => void save()}>{busy ? t('admin.users.saving') : t('common.save')}</button>
      </>}>
      {loading ? <Spinner text={t('admin.users.loadingPerms')} /> : datasources.length === 0 ? <Empty text={t('admin.users.emptyDs')} /> : (
        <div className="col" style={{ gap: 8 }}>
          {user.role === 'admin' && <div className="tiny muted">{t('admin.users.adminAllPerm')}</div>}
          {datasources.map((d) => (
            <label key={d.id} className="row" style={{ gap: 8, cursor: 'pointer' }}>
              <input type="checkbox" disabled={user.role === 'admin'}
                checked={user.role === 'admin' ? true : !!grants[d.id]}
                onChange={(e) => setGrants((g) => ({ ...g, [d.id]: e.target.checked }))} />
              <span style={{ minWidth: 0 }}>
                <div style={{ fontSize: 13 }}>{d.name}</div>
                <div className="tiny muted">{d.db_type} · {d.database_name}</div>
              </span>
            </label>
          ))}
        </div>
      )}
    </Modal>
  )
}

// ---------------- 脱敏规则 ----------------

function MaskingPane() {
  const { t } = useI18n()
  const datasources = useApp((s) => s.datasources)
  const loadDatasources = useApp((s) => s.loadDatasources)
  const [dsId, setDsId] = useState('')
  const [rows, setRows] = useState<MaskingRuleRow[]>([])
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState<MaskingRuleRow | null>(null)
  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState<MaskingRuleRow | null>(null)

  useEffect(() => { void loadDatasources().catch(() => undefined) }, [loadDatasources])
  useEffect(() => { if (!dsId && datasources.length) setDsId(datasources[0].id) }, [datasources, dsId])

  const load = useCallback(async () => {
    if (!dsId) { setRows([]); return }
    setLoading(true)
    try {
      setRows(await api.listMaskingRules(dsId))
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setLoading(false)
    }
  }, [dsId])

  useEffect(() => { void load() }, [load])

  const strategyLabel = (s: string) => (STRATEGIES as readonly string[]).includes(s) ? t(`admin.users.strategy.${s}`) : s

  return (
    <div className="card">
      <div className="row" style={{ padding: '12px 16px', gap: 8 }}>
        <span className="tiny muted">{t('admin.users.dsLabel')}</span>
        <select value={dsId} onChange={(e) => setDsId(e.target.value)} style={{ minWidth: 220 }}>
          {datasources.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
        <span className="spacer" />
        <button className="btn primary sm" disabled={!dsId} onClick={() => setCreating(true)}>＋ {t('admin.users.addRule')}</button>
      </div>
      <div className="tiny muted" style={{ padding: '0 16px 10px' }}>
        {t('admin.users.maskingDesc1')}<code>*phone*</code>{t('admin.users.maskingDesc2')}<code>id_card</code>{t('admin.users.maskingDesc3')}
      </div>
      {loading ? <Spinner text={t('common.loading')} /> : rows.length === 0 ? <Empty text={t('admin.users.emptyRules')} /> : (
        <table className="table">
          <thead>
            <tr><th>{t('admin.users.colPattern')}</th><th>{t('admin.users.colStrategy')}</th>
              <th>{t('common.status')}</th><th>{t('admin.users.colCreatedAt')}</th>
              <th style={{ width: 160 }}>{t('common.actions')}</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td><code>{r.column_pattern}</code></td>
                <td>{strategyLabel(r.strategy)}</td>
                <td><StatusTag value={r.enabled ? 'active' : 'disabled'} label={r.enabled ? t('common.enabled') : t('common.disabled')} /></td>
                <td className="tiny muted">{fmtTime(r.created_at)}</td>
                <td>
                  <div className="row" style={{ gap: 6 }}>
                    <button className="btn sm" onClick={() => setEditing(r)}>{t('common.edit')}</button>
                    <button className="btn sm danger" onClick={() => setDeleting(r)}>{t('common.delete')}</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {creating && <MaskRuleModal dsId={dsId} onClose={() => setCreating(false)}
        onSaved={() => { setCreating(false); void load() }} />}
      {editing && <MaskRuleModal dsId={dsId} rule={editing} onClose={() => setEditing(null)}
        onSaved={() => { setEditing(null); void load() }} />}
      {deleting && (
        <Confirm title={t('admin.users.confirmDeleteRuleTitle')} danger okText={t('common.delete')}
          text={t('admin.users.confirmDeleteRule', { pattern: deleting.column_pattern })}
          onCancel={() => setDeleting(null)}
          onOk={() => {
            void api.deleteMaskingRule(deleting.id)
              .then(() => { toast.ok(t('common.deleted')); setDeleting(null); void load() })
              .catch((e) => { toast.error((e as ApiError).message); setDeleting(null) })
          }} />
      )}
    </div>
  )
}

function MaskRuleModal({ dsId, rule, onClose, onSaved }: {
  dsId: string; rule?: MaskingRuleRow; onClose: () => void; onSaved: () => void
}) {
  const { t } = useI18n()
  const [pattern, setPattern] = useState(rule?.column_pattern ?? '')
  const [strategy, setStrategy] = useState(rule?.strategy ?? 'mask_partial')
  const [enabled, setEnabled] = useState(rule?.enabled ?? true)
  const [busy, setBusy] = useState(false)

  const save = async () => {
    setBusy(true)
    try {
      if (rule) {
        await api.updateMaskingRule(rule.id, { column_pattern: pattern, strategy, enabled })
      } else {
        await api.createMaskingRule(dsId, { column_pattern: pattern, strategy, enabled })
      }
      toast.ok(t('common.saved'))
      onSaved()
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={rule ? t('admin.users.editRuleTitle') : t('admin.users.newRuleTitle')} onClose={onClose} width={480}
      footer={<>
        <button className="btn" onClick={onClose}>{t('common.cancel')}</button>
        <button className="btn primary" disabled={busy} onClick={() => void save()}>{busy ? t('admin.users.saving') : t('common.save')}</button>
      </>}>
      <div className="col" style={{ gap: 12 }}>
        <Field label={t('admin.users.colPattern')} hint={t('admin.users.patternHint')}>
          <input value={pattern} onChange={(e) => setPattern(e.target.value)} autoFocus />
        </Field>
        <Field label={t('admin.users.colStrategy')}>
          <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            {STRATEGIES.map((k) => <option key={k} value={k}>{t(`admin.users.strategy.${k}`)}</option>)}
          </select>
        </Field>
        <label className="row" style={{ gap: 8, cursor: 'pointer' }}>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          <span style={{ fontSize: 13 }}>{t('common.enable')}</span>
        </label>
      </div>
    </Modal>
  )
}
