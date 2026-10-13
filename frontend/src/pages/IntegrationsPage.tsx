/** 集成与技能页（管理员）：通用自定义 Skill 托管（PRD 3.3C）+ 调度平台配置（PRD 3.3G）。 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '@/api/client'
import { Field, relTime, StatusTag, Tabs } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import { toast } from '@/stores/app'
import type { AirflowConfigOut, AzkabanConfigOut, CustomSkillRow } from '@/types'

export default function IntegrationsPage() {
  const { t } = useI18n()
  const [tab, setTab] = useState<'skills' | 'schedulers'>('skills')
  return (
    <div className="col" style={{ gap: 16 }}>
      <div className="row wrap">
        <h2 style={{ margin: 0 }}>{t('admin.integ.title')}</h2>
        <span className="spacer" />
        <span className="tiny muted">{t('admin.integ.subtitle')}</span>
      </div>
      <Tabs value={tab} onChange={setTab} items={[
        { key: 'skills', label: t('admin.integ.tabSkills') },
        { key: 'schedulers', label: t('admin.integ.tabSchedulers') },
      ]} />
      {tab === 'skills' ? <SkillsPanel /> : <SchedulerConfigPanel />}
    </div>
  )
}

/* ================= 自定义 Skill ================= */

function SkillsPanel() {
  const { t, lang } = useI18n()
  const [rows, setRows] = useState<CustomSkillRow[]>([])
  const [busy, setBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    try {
      setRows(await api.listSkills(true))
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }, [])
  useEffect(() => { void load() }, [load])

  const upload = async (f: File) => {
    setBusy(true)
    try {
      const s = await api.uploadSkill(f)
      toast.ok(t('admin.integ.uploadOk', { name: s.name, version: s.version }))
      await load()
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const setStatus = async (s: CustomSkillRow, status: 'enabled' | 'disabled') => {
    try {
      await api.setSkillStatus(s.id, status)
      toast.ok(t(status === 'enabled' ? 'admin.integ.enabledToast' : 'admin.integ.disabledToast',
        { name: s.name, version: s.version }))
      await load()
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }

  const remove = async (s: CustomSkillRow) => {
    if (!window.confirm(t('admin.integ.confirmDeleteSkill', { name: s.name, version: s.version }))) return
    try {
      await api.deleteSkill(s.id)
      toast.ok(t('common.deleted'))
      await load()
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }

  // 只放通用 Skill（prompt/python）；scheduler 型归「调度平台配置」管理（PRD 3.3G）
  const grouped = rows.filter((s) => s.skill_type !== 'scheduler')
    .reduce<Record<string, CustomSkillRow[]>>((acc, s) => {
      (acc[s.name] = acc[s.name] || []).push(s)
      return acc
    }, {})

  return (
    <div className="card">
      <div className="card-body">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div className="muted tiny" style={{ maxWidth: 640 }}>
            {t('admin.integ.skillDesc')}
          </div>
          <div>
            <input ref={fileRef} type="file" accept=".zip" style={{ display: 'none' }}
              onChange={(e) => e.target.files?.[0] && void upload(e.target.files[0])} />
            <button className="btn primary" disabled={busy}
              onClick={() => fileRef.current?.click()}>
              {busy ? t('admin.integ.uploading') : `＋ ${t('admin.integ.uploadBtn')}`}
            </button>
          </div>
        </div>

        {rows.length === 0 && <div className="empty tiny" style={{ padding: 24 }}>{t('admin.integ.emptySkills')}</div>}
        {Object.entries(grouped).map(([name, vers]) => (
          <div key={name} className="card" style={{ marginTop: 12 }}>
            <div className="card-body">
              <div className="row" style={{ alignItems: 'center' }}>
                <b>{name}</b>
                <span className="tag">{
                  vers[0].skill_type === 'python' ? 'Python'
                    : vers[0].skill_type === 'scheduler' ? t('admin.integ.typeScheduler') : 'Prompt'}</span>
                <span className="muted tiny">{vers[0].description}</span>
              </div>
              <div className="tiny muted mt8">{t('admin.integ.triggerKw')}{(vers[0].trigger_keywords || []).join(lang === 'zh' ? '、' : ', ') || '-'}</div>
              <table className="tbl mt8" style={{ fontSize: 12.5 }}>
                <thead>
                  <tr><th>{t('common.version')}</th><th>{t('common.status')}</th><th>{t('admin.integ.colInvocations')}</th>
                    <th>{t('admin.integ.colLastInvoked')}</th><th>{t('admin.integ.colUploadedBy')}</th>
                    <th>{t('admin.integ.colUploadedAt')}</th><th></th></tr>
                </thead>
                <tbody>
                  {vers.map((s) => (
                    <tr key={s.id}>
                      <td>v{s.version}</td>
                      <td><StatusTag value={s.status === 'enabled' ? 'active' : 'disabled'}
                        label={s.status === 'enabled' ? t('common.enabled') : t('common.disabled')}
                        cls={s.status === 'enabled' ? 'ok' : ''} /></td>
                      <td>{s.invoke_count}</td>
                      <td className="muted">{s.last_invoked_at ? relTime(s.last_invoked_at, t) : '-'}</td>
                      <td className="muted">{s.created_by}</td>
                      <td className="muted">{relTime(s.created_at, t)}</td>
                      <td>
                        <div className="row" style={{ gap: 6, justifyContent: 'flex-end' }}>
                          {s.status === 'enabled'
                            ? <button className="btn sm" onClick={() => void setStatus(s, 'disabled')}>{t('common.disable')}</button>
                            : <button className="btn sm primary" onClick={() => void setStatus(s, 'enabled')}>{t('common.enable')}</button>}
                          <button className="btn sm danger" onClick={() => void remove(s)}>{t('common.delete')}</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))}

        <details className="mt12">
          <summary className="tiny muted" style={{ cursor: 'pointer' }}>{t('admin.integ.sampleToggle')}</summary>
          <pre className="tiny" style={{ whiteSpace: 'pre-wrap' }}>{t('admin.integ.skillSample')}</pre>
        </details>
      </div>
    </div>
  )
}

/* ================= Azkaban ================= */

function AzkabanPanel() {
  const { t } = useI18n()
  const [cfg, setCfg] = useState<AzkabanConfigOut | null>(null)
  const [form, setForm] = useState({ url: '', username: '', password: '', default_project: 'data-agent-etl', verify_tls: false })
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const c = await api.getAzkabanConfig()
      setCfg(c)
      setForm((f) => ({ ...f, url: c.url, username: c.username, default_project: c.default_project || f.default_project, verify_tls: c.verify_tls }))
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }, [])
  useEffect(() => { void load() }, [load])

  const save = async () => {
    setBusy(true)
    try {
      const c = await api.putAzkabanConfig(form)
      setCfg(c)
      toast.ok(t('admin.integ.savedPwd'))
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
    }
  }

  const test = async () => {
    setBusy(true)
    try {
      const r = await api.testAzkaban()
      if (r.ok) toast.ok(r.message)
      else toast.error(r.message)
      await load()
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card" style={{ maxWidth: 640 }}>
      <div className="card-body">
        <div className="row" style={{ alignItems: 'center', gap: 8 }}>
          <b>{t('admin.integ.azkTitle')}</b>
          {cfg?.configured && (
            <StatusTag
              value={cfg.status === 'healthy' ? 'connected' : cfg.status === 'error' ? 'error' : 'untested'}
              label={cfg.status === 'healthy' ? t('common.connected') : cfg.status === 'error' ? t('common.connectionError') : t('common.untested')} />
          )}
        </div>
        {cfg?.status_message && <div className="tiny muted mt8">{cfg.status_message}</div>}

        <div className="mt12">
          <Field label={t('admin.integ.urlAzkLabel')}>
            <input className="input" value={form.url} placeholder="https://…"
              onChange={(e) => setForm({ ...form, url: e.target.value })} />
          </Field>
          <Field label={t('admin.integ.accountLabel')}>
            <input className="input" value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })} />
          </Field>
          <Field label={t('admin.integ.pwdLabel')}>
            <input className="input" type="password" value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })} />
          </Field>
          <Field label={t('admin.integ.defaultProject')}>
            <input className="input" value={form.default_project}
              onChange={(e) => setForm({ ...form, default_project: e.target.value })} />
          </Field>
          <label className="row tiny" style={{ gap: 5 }}>
            <input type="checkbox" checked={form.verify_tls}
              onChange={(e) => setForm({ ...form, verify_tls: e.target.checked })} />
            {t('admin.integ.verifyTls')}
          </label>
        </div>

        <div className="row mt12" style={{ gap: 8 }}>
          <button className="btn primary" disabled={busy || !form.url || !form.username}
            onClick={() => void save()}>{t('common.save')}</button>
          <button className="btn" disabled={busy || !cfg?.configured}
            onClick={() => void test()}>{t('admin.integ.testConnection')}</button>
        </div>
        <div className="tiny muted mt12">
          {t('admin.integ.azkNote')}
        </div>
      </div>
    </div>
  )
}

/* ================= 调度平台配置（PRD 3.3G） ================= */

function SchedulerConfigPanel() {
  return (
    <div className="col" style={{ gap: 16 }}>
      <AzkabanPanel />
      <AirflowPanel />
      <SchedulerSkillsPanel />
    </div>
  )
}

function AirflowPanel() {
  const { t } = useI18n()
  const [cfg, setCfg] = useState<AirflowConfigOut | null>(null)
  const [form, setForm] = useState({ url: '', username: '', password: '', verify_tls: false })
  const [busy, setBusy] = useState(false)
  const [dags, setDags] = useState<{ dag_id: string; is_paused: boolean; schedule: string | null;
    last_run: { state: string; start_date: string } | null }[] | null>(null)

  const load = useCallback(async () => {
    try {
      const c = await api.getAirflowConfig()
      setCfg(c)
      setForm((f) => ({ ...f, url: c.url, username: c.username, verify_tls: c.verify_tls }))
    } catch (e) { toast.error((e as ApiError).message) }
  }, [])
  useEffect(() => { void load() }, [load])

  const save = async () => {
    setBusy(true)
    try {
      const c = await api.putAirflowConfig(form)
      setCfg(c)
      toast.ok(t('admin.integ.savedPwd'))
    } catch (e) { toast.error((e as ApiError).message) }
    finally { setBusy(false) }
  }

  const test = async () => {
    setBusy(true)
    try {
      const r = await api.testAirflow()
      if (r.ok) toast.ok(r.message)
      else toast.error(r.message)
      await load()
    } catch (e) { toast.error((e as ApiError).message) }
    finally { setBusy(false) }
  }

  const sync = async () => {
    setBusy(true)
    try {
      const m = await api.airflowMetadata()
      if (m.error) { toast.error(t('admin.integ.syncFailed', { msg: m.error })); setDags(null) }
      else setDags(m.dags)
    } catch (e) { toast.error((e as ApiError).message) }
    finally { setBusy(false) }
  }

  return (
    <div className="card" style={{ maxWidth: 640 }}>
      <div className="card-body">
        <div className="row" style={{ alignItems: 'center', gap: 8 }}>
          <b>{t('admin.integ.afTitle')}</b>
          {cfg?.configured && (
            <StatusTag
              value={cfg.status === 'healthy' ? 'connected' : cfg.status === 'error' ? 'error' : 'untested'}
              label={cfg.status === 'healthy' ? t('common.connected') : cfg.status === 'error' ? t('common.connectionError') : t('common.untested')} />
          )}
        </div>
        {cfg?.status_message && <div className="tiny muted mt8">{cfg.status_message}</div>}

        <div className="mt12">
          <Field label={t('admin.integ.urlAfLabel')}>
            <input className="input" value={form.url} placeholder="http://…"
              onChange={(e) => setForm({ ...form, url: e.target.value })} />
          </Field>
          <Field label={t('admin.integ.accountBasicLabel')}>
            <input className="input" value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })} />
          </Field>
          <Field label={t('admin.integ.pwdLabel')}>
            <input className="input" type="password" value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })} />
          </Field>
          <label className="row tiny" style={{ gap: 5 }}>
            <input type="checkbox" checked={form.verify_tls}
              onChange={(e) => setForm({ ...form, verify_tls: e.target.checked })} />
            {t('admin.integ.verifyTls')}
          </label>
        </div>

        <div className="row mt12" style={{ gap: 8 }}>
          <button className="btn primary" disabled={busy || !form.url || !form.username}
            onClick={() => void save()}>{t('common.save')}</button>
          <button className="btn" disabled={busy || !cfg?.configured}
            onClick={() => void test()}>{t('admin.integ.testConnection')}</button>
          <button className="btn ghost" disabled={busy || !cfg?.configured}
            onClick={() => void sync()}>{t('admin.integ.syncDags')}</button>
        </div>
        <div className="tiny muted mt12">
          {t('admin.integ.afNote')}
        </div>

        {dags && (
          <table className="tbl mt12" style={{ fontSize: 12.5 }}>
            <thead><tr><th>DAG</th><th>{t('admin.integ.colSchedule')}</th><th>{t('common.status')}</th><th>{t('admin.integ.colLastRun')}</th></tr></thead>
            <tbody>
              {dags.map((d) => (
                <tr key={d.dag_id}>
                  <td><code>{d.dag_id}</code></td>
                  <td className="muted">{d.schedule || '-'}</td>
                  <td>{d.is_paused ? <span className="tag tiny">{t('admin.integ.paused')}</span> : <span className="tag ok tiny">{t('admin.integ.active')}</span>}</td>
                  <td>{d.last_run
                    ? <StatusTag value={d.last_run.state === 'success' ? 'success' : d.last_run.state === 'failed' ? 'failed' : 'running'}
                        label={d.last_run.state} />
                    : <span className="muted">-</span>}</td>
                </tr>
              ))}
              {dags.length === 0 && <tr><td colSpan={4} className="muted tiny">{t('admin.integ.noDags')}</td></tr>}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

function SchedulerSkillsPanel() {
  const { t } = useI18n()
  const [rows, setRows] = useState<CustomSkillRow[]>([])
  const [busy, setBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    try {
      setRows((await api.listSkills(true)).filter((s) => s.skill_type === 'scheduler'))
    } catch (e) { toast.error((e as ApiError).message) }
  }, [])
  useEffect(() => { void load() }, [load])

  const upload = async (f: File) => {
    setBusy(true)
    try {
      const s = await api.uploadSkill(f)
      if (s.skill_type !== 'scheduler') {
        toast.warn(t('admin.integ.warnNonScheduler', { type: s.skill_type }))
      } else {
        toast.ok(t('admin.integ.uploadSchedOk', { name: s.name, version: s.version }))
      }
      await load()
    } catch (e) { toast.error((e as ApiError).message) }
    finally {
      setBusy(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const setStatus = async (s: CustomSkillRow, status: 'enabled' | 'disabled') => {
    try {
      await api.setSkillStatus(s.id, status)
      toast.ok(t(status === 'enabled' ? 'admin.integ.schedEnabledToast' : 'admin.integ.schedDisabledToast', { name: s.name }))
      await load()
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }

  const remove = async (s: CustomSkillRow) => {
    if (!window.confirm(t('admin.integ.confirmDeleteSched', { name: s.name, version: s.version }))) return
    try {
      await api.deleteSkill(s.id)
      toast.ok(t('common.deleted'))
      await load()
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }

  return (
    <div className="card" style={{ maxWidth: 760 }}>
      <div className="card-body">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div>
            <b>{t('admin.integ.schedSkillTitle')}</b>
            <div className="muted tiny mt8" style={{ maxWidth: 560 }}>
              {t('admin.integ.schedSkillDesc1')}
              <code> build_package(task) -&gt; {'{filename, content, note}'} </code>
              {t('admin.integ.schedSkillDesc2')}
            </div>
          </div>
          <div>
            <input ref={fileRef} type="file" accept=".zip" style={{ display: 'none' }}
              onChange={(e) => e.target.files?.[0] && void upload(e.target.files[0])} />
            <button className="btn primary" disabled={busy}
              onClick={() => fileRef.current?.click()}>
              {busy ? t('admin.integ.uploading') : `＋ ${t('admin.integ.uploadSchedBtn')}`}
            </button>
          </div>
        </div>

        {rows.length === 0 && <div className="empty tiny" style={{ padding: 16 }}>{t('admin.integ.emptySchedSkills')}</div>}
        {rows.length > 0 && (
          <table className="tbl mt12" style={{ fontSize: 12.5 }}>
            <thead><tr><th>{t('common.name')}</th><th>{t('common.version')}</th><th>{t('common.description')}</th>
              <th>{t('common.status')}</th><th>{t('admin.integ.colUploadedBy')}</th><th>{t('admin.integ.colUploadedAt')}</th><th></th></tr></thead>
            <tbody>
              {rows.map((s) => (
                <tr key={s.id}>
                  <td><b>{s.name}</b></td>
                  <td>v{s.version}</td>
                  <td className="muted">{s.description}</td>
                  <td><StatusTag value={s.status === 'enabled' ? 'active' : 'disabled'}
                    label={s.status === 'enabled' ? t('common.enabled') : t('common.disabled')}
                    cls={s.status === 'enabled' ? 'ok' : ''} /></td>
                  <td className="muted">{s.created_by}</td>
                  <td className="muted">{relTime(s.created_at, t)}</td>
                  <td>
                    <div className="row" style={{ gap: 6, justifyContent: 'flex-end' }}>
                      {s.status === 'enabled'
                        ? <button className="btn ghost sm" onClick={() => void setStatus(s, 'disabled')}>{t('common.disable')}</button>
                        : <button className="btn sm" onClick={() => void setStatus(s, 'enabled')}>{t('common.enable')}</button>}
                      <button className="btn danger sm" onClick={() => void remove(s)}>{t('common.delete')}</button>
                    </div>
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
