/** SQL 展示（折叠 + 复制 + 语法高亮）。 */
import { useState } from 'react'
import { SqlText } from '@/components/common/SqlText'
import { useI18n } from '@/i18n'
import { toast } from '@/stores/app'

export function SqlPanel({ sql, tables, dialect, guarded }: {
  sql: string
  tables?: string[]
  dialect?: string
  guarded?: boolean
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  if (!sql) return null

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(sql)
      toast.ok(t('chat.sqlCopied'))
    } catch {
      toast.error(t('chat.copyFailed'))
    }
  }

  return (
    <div className="sqlbox">
      <div className="sqlbox-head" onClick={() => setOpen(!open)}>
        <span>{open ? '▾' : '▸'}</span>
        <span style={{ fontWeight: 500 }}>{t('chat.sqlTitle')}</span>
        {dialect && <span className="tag">{dialect}</span>}
        {guarded && <span className="tag ok">{t('chat.sqlGuarded')}</span>}
        {tables && tables.length > 0 && (
          <span className="tiny muted truncate" style={{ maxWidth: 260 }}>
            {tables.join(', ')}
          </span>
        )}
        <span className="spacer" />
        <button className="btn ghost sm" onClick={(e) => { e.stopPropagation(); void copy() }}>{t('common.copy')}</button>
      </div>
      {open && <pre><SqlText code={sql} /></pre>}
    </div>
  )
}
