/** 数仓规范管理（PRD 3.3E）：规范文档 + 结构化规则开关，管理员维护，全员可见；支持导入 Markdown/TXT 规范文件（启发式建议校验开关）。 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '@/api/client'
import { Confirm, Empty, Spinner } from '@/components/common/ui'
import { toast } from '@/stores/app'
import { useAuth } from '@/stores/auth'
import { useI18n } from '@/i18n'
import type { DwStandardRow } from '@/types'

interface Draft { name: string; content: string; rules: string[]; enabled: boolean }

const EMPTY: Draft = { name: '', content: '', rules: [], enabled: true }

export function StandardsPanel() {
  const { t } = useI18n()
  const isAdmin = useAuth((s) => s.user?.role === 'admin')
  const [rows, setRows] = useState<DwStandardRow[] | null>(null)
  const [ruleDefs, setRuleDefs] = useState<{ key: string; label: string }[]>([])
  const [editing, setEditing] = useState<string | 'new' | null>(null)
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [preview, setPreview] = useState<DwStandardRow | null>(null)
  const [importing, setImporting] = useState(false)
  const [deleting, setDeleting] = useState<DwStandardRow | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    try {
      const r = await api.listStandards()
      setRows(r.standards)
    } catch (e) { toast.error((e as ApiError).message) }
  }, [])

  useEffect(() => {
    void load()
    api.standardRuleDefs().then((r) => setRuleDefs(r.rules)).catch(() => undefined)
  }, [load])

  const startEdit = (s?: DwStandardRow) => {
    setPreview(null)
    setEditing(s ? s.id : 'new')
    setDraft(s ? { name: s.name, content: s.content, rules: s.rules, enabled: s.enabled } : EMPTY)
  }

  /** 导入规范文件（PRD 3.3E）：解析正文 + 启发式建议校验开关，预填编辑器由人工确认后保存。 */
  const importFile = async (file: File) => {
    setImporting(true)
    try {
      const r = await api.importStandard(file)
      setPreview(null)
      setEditing('new')
      setDraft({ name: r.name, content: r.content, rules: r.rules, enabled: true })
      toast.ok(r.rules.length
        ? t('modeling.importParsedWithRules', { name: r.name, n: r.rules.length })
        : t('modeling.importParsedNoRules', { name: r.name }))
    } catch (e) { toast.error((e as ApiError).message) }
    finally {
      setImporting(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const save = async () => {
    try {
      if (editing === 'new') await api.createStandard({ ...draft })
      else if (editing) await api.updateStandard(editing, { ...draft })
      toast.ok(t('modeling.standardSaved'))
      setEditing(null)
      void load()
    } catch (e) { toast.error((e as ApiError).message) }
  }

  const remove = async (s: DwStandardRow) => {
    setDeleting(null)
    try {
      await api.deleteStandard(s.id)
      toast.ok(t('common.deleted'))
      void load()
    } catch (e) { toast.error((e as ApiError).message) }
  }

  if (rows === null) return <Spinner text={t('modeling.loadingStandards')} />

  return (
    <div className="row" style={{ alignItems: 'flex-start', gap: 16 }}>
      {/* 规范列表 */}
      <div className="card" style={{ width: 300, flexShrink: 0 }}>
        <div className="card-body" style={{ padding: 10 }}>
          {isAdmin && (
            <>
              <button className="btn primary" style={{ width: '100%', marginBottom: 8 }}
                onClick={() => startEdit()}>＋ {t('modeling.newStandard')}</button>
              <button className="btn" style={{ width: '100%', marginBottom: 8 }} disabled={importing}
                onClick={() => fileRef.current?.click()}>
                {importing ? t('modeling.importingFile') : `📥 ${t('modeling.importStandardFile')}`}
              </button>
              <input ref={fileRef} type="file" accept=".md,.markdown,.txt" style={{ display: 'none' }}
                onChange={(e) => { const f = e.target.files?.[0]; if (f) void importFile(f) }} />
            </>
          )}
          {rows.length === 0 && <Empty icon="📏" text={t('modeling.standardsEmpty')} />}
          {rows.map((s) => (
            <div key={s.id} className="conv" style={{ padding: '8px 10px', borderRadius: 6, cursor: 'pointer' }}
              onClick={() => { setEditing(null); setPreview(s) }}>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="t" style={{ fontSize: 13 }}>{s.name}</div>
                <div className="d tiny muted">{t('modeling.rulesCount', { n: s.rules.length })} · {s.updated_by || '—'}</div>
              </div>
              <span className={`tag tiny ${s.enabled ? 'ok' : ''}`}>{s.enabled ? t('common.enabled') : t('common.disabled')}</span>
            </div>
          ))}
        </div>
      </div>

      {/* 详情 / 编辑器 */}
      <div className="card" style={{ flex: 1, minWidth: 0 }}>
        <div className="card-body">
          {editing === null && !preview && (
            <Empty icon="📏" text={isAdmin ? t('modeling.previewEmptyAdmin') : t('modeling.previewEmpty')} />
          )}

          {preview && !editing && (
            <div>
              <div className="row">
                <h3 style={{ margin: 0 }}>{preview.name}</h3>
                <span className={`tag ${preview.enabled ? 'ok' : ''}`}>
                  {preview.enabled ? t('common.enabled') : t('common.disabled')}</span>
                <span className="spacer" />
                {isAdmin && <>
                  <button className="btn sm" onClick={() => startEdit(preview)}>{t('common.edit')}</button>
                  <button className="btn ghost sm" onClick={() => setDeleting(preview)}>{t('common.delete')}</button>
                </>}
              </div>
              {preview.rules.length > 0 && (
                <div className="mt8 col" style={{ gap: 4 }}>
                  <div className="tiny dim">{t('modeling.machineChecks')}</div>
                  {preview.rules.map((r) => (
                    <div key={r} className="tiny">✔ {ruleDefs.find((d) => d.key === r)?.label || r}</div>
                  ))}
                </div>
              )}
              <pre className="mono mt12" style={{ fontSize: 12.5, whiteSpace: 'pre-wrap', background: 'var(--bg)', padding: 12, borderRadius: 6 }}>
                {preview.content || t('modeling.noContent')}
              </pre>
              <div className="tiny muted mt8">
                {t('modeling.effectiveScope')}
              </div>
            </div>
          )}

          {editing !== null && (
            <div className="col" style={{ gap: 10 }}>
              <h3 style={{ margin: 0 }}>{editing === 'new' ? t('modeling.newStandard') : t('modeling.editStandard')}</h3>
              <label className="tiny dim">{t('modeling.standardName')}</label>
              <input className="input" value={draft.name} placeholder={t('modeling.standardNamePlaceholder')}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
              <label className="tiny dim">{t('modeling.standardContent')}</label>
              <textarea className="input mono" rows={10} value={draft.content}
                placeholder={t('modeling.standardContentPlaceholder')}
                onChange={(e) => setDraft({ ...draft, content: e.target.value })} />
              <label className="tiny dim">{t('modeling.structuredRules')}</label>
              <div className="col" style={{ gap: 4 }}>
                {ruleDefs.map((d) => (
                  <label key={d.key} className="tiny row" style={{ gap: 6, cursor: 'pointer' }}>
                    <input type="checkbox" checked={draft.rules.includes(d.key)}
                      onChange={(e) => setDraft({
                        ...draft,
                        rules: e.target.checked
                          ? [...draft.rules, d.key]
                          : draft.rules.filter((x) => x !== d.key),
                      })} />
                    {d.label}
                  </label>
                ))}
              </div>
              <label className="tiny row" style={{ gap: 6, cursor: 'pointer' }}>
                <input type="checkbox" checked={draft.enabled}
                  onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
                {t('modeling.enableStandard')}
              </label>
              <div className="row" style={{ gap: 8 }}>
                <button className="btn primary" disabled={!draft.name.trim()}
                  onClick={() => void save()}>{t('common.save')}</button>
                <button className="btn ghost" onClick={() => setEditing(null)}>{t('common.cancel')}</button>
              </div>
            </div>
          )}
        </div>
      </div>
      {deleting && (
        <Confirm
          danger
          title={t('common.delete')}
          text={t('modeling.confirmDeleteStandard', { name: deleting.name })}
          okText={t('common.delete')}
          onCancel={() => setDeleting(null)}
          onOk={() => void remove(deleting)}
        />
      )}
    </div>
  )
}
