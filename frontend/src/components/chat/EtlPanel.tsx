/** ETL 方案展示（PRD 3.3B）：源表推荐 / DDL / 加工 SQL / 字段映射 / 规范提示 / Azkaban 推送。
 * 「转为建模任务」把草稿转入自动化建模模块（PRD 3.3D）做任务级编辑与调度。 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError } from '@/api/client'
import { SqlText } from '@/components/common/SqlText'
import { Confirm } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import { toast } from '@/stores/app'
import type { EtlDesignResult } from '@/types'

function CodeBlock({ title, code, tag }: { title: string; code: string; tag?: string }) {
  const { t } = useI18n()
  const [open, setOpen] = useState(true)
  if (!code) return null
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      toast.ok(t('chat.codeCopied', { title }))
    } catch {
      toast.error(t('chat.copyFailed'))
    }
  }
  return (
    <div className="sqlbox">
      <div className="sqlbox-head" onClick={() => setOpen(!open)}>
        <span>{open ? '▾' : '▸'}</span>
        <span style={{ fontWeight: 500 }}>{title}</span>
        {tag && <span className="tag">{tag}</span>}
        <span className="spacer" />
        <button className="btn ghost sm" onClick={(e) => { e.stopPropagation(); void copy() }}>{t('common.copy')}</button>
      </div>
      {open && <pre><SqlText code={code} /></pre>}
    </div>
  )
}

export function EtlPanel({ design, conversationId, datasourceId }: {
  design: EtlDesignResult
  conversationId?: string
  datasourceId?: string | null
}) {
  const { t } = useI18n()
  const nav = useNavigate()
  const [pushing, setPushing] = useState(false)
  const [pushMsg, setPushMsg] = useState('')
  const [confirmPush, setConfirmPush] = useState(false)

  const saveAsTask = async () => {
    try {
      const r = await api.createEtlTask({
        name: design.target_table || t('chat.etlTaskDefault'), target_table: design.target_table,
        layer: design.layer, dialect: design.dialect, schedule: design.schedule,
        ddl: design.ddl, etl_sql: design.etl_sql,
        source_tables: design.source_tables, field_mapping: design.field_mapping,
        notes: design.notes, warnings: design.warnings,
        source: 'chat', conversation_id: conversationId || null,
        datasource_id: datasourceId || null,
      })
      toast.ok(t('chat.etlSaved'))
      nav(`/modeling?task=${r.id}`)
    } catch (e) {
      toast.error((e as ApiError).message)
    }
  }

  // 人工二次确认（PRD 安全边界）：确认弹窗点确定后执行
  const push = async () => {
    setConfirmPush(false)
    setPushing(true)
    setPushMsg('')
    try {
      const r = await api.pushEtlToAzkaban(design)
      setPushMsg(r.message)
      if (r.ok) toast.ok(t('chat.pushed'))
      else toast.warn(r.message)
    } catch (e) {
      const msg = (e as ApiError).message
      setPushMsg(msg)
      toast.error(msg)
    } finally {
      setPushing(false)
    }
  }

  return (
    <div className="card" style={{ marginTop: 10 }}>
      <div className="card-body">
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ fontWeight: 600 }}>
            🛠 {t('chat.etlPlan')}{design.target_table && <>：<code>{design.target_table}</code></>}
            {design.layer && <span className="tag accent" style={{ marginLeft: 6 }}>{design.layer.toUpperCase()}</span>}
          </div>
          {(design.ddl || design.etl_sql) && (
            <div className="row" style={{ gap: 6 }}>
              <button className="btn sm primary" onClick={() => void saveAsTask()}>
                {t('chat.toModelingTask')}
              </button>
              <button className="btn sm" disabled={pushing} onClick={() => setConfirmPush(true)}>
                {pushing ? t('chat.pushing') : t('chat.pushToAzkaban')}
              </button>
              <button className="btn ghost sm" disabled={pushing}
                onClick={() => void api.downloadEtlPackage(design).then(() => toast.ok(t('chat.zipDownloaded')))
                  .catch((e: ApiError) => toast.error(e.message))}>
                {t('chat.downloadZip')}
              </button>
            </div>
          )}
        </div>
        {design.schedule && <div className="tiny muted mt8">{t('chat.scheduleAdvice')}{design.schedule}</div>}
        {pushMsg && <div className="tiny muted mt8">{pushMsg}</div>}
        {confirmPush && (
          <Confirm
            title={t('chat.pushToAzkaban')}
            text={t('chat.confirmPush', { name: design.target_table || t('chat.thisEtlPlan') })}
            onCancel={() => setConfirmPush(false)}
            onOk={() => void push()}
          />
        )}

        {design.source_tables.length > 0 && (
          <div className="mt12">
            <div className="tiny dim mb8">{t('chat.sourceTables')}</div>
            {design.source_tables.map((s) => (
              <div key={s.table} style={{ fontSize: 13, marginBottom: 4 }}>
                <code>{s.table}</code> <span className="tag">{s.layer}</span>
                <span className="muted tiny" style={{ marginLeft: 6 }}>{s.reason}</span>
              </div>
            ))}
          </div>
        )}

        {design.ddl && <div className="mt12"><CodeBlock title={t('chat.ddlTitle')} code={design.ddl} tag={t('chat.ddlTag')} /></div>}
        {design.etl_sql && <div className="mt8"><CodeBlock title={t('chat.etlSqlTitle')} code={design.etl_sql} tag={t('chat.etlSqlTag')} /></div>}

        {design.field_mapping.length > 0 && (
          <div className="mt12">
            <div className="tiny dim mb8">{t('chat.fieldMapping')}</div>
            <table className="tbl" style={{ fontSize: 12.5 }}>
              <thead><tr><th>{t('chat.colTarget')}</th><th>{t('chat.colSource')}</th><th>{t('chat.colRule')}</th></tr></thead>
              <tbody>
                {design.field_mapping.map((fm, i) => (
                  <tr key={i}>
                    <td><code>{fm.target}</code></td>
                    <td className="muted">{fm.source}</td>
                    <td className="muted">{fm.rule}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {design.warnings.length > 0 && (
          <div className="tag warn mt12" style={{ display: 'block', whiteSpace: 'normal' }}>
            <b>{t('chat.lintWarnings')}</b>
            <ul style={{ margin: '4px 0 0 16px' }}>
              {design.warnings.map((w, i) => <li key={i}>{w}</li>)}
            </ul>
          </div>
        )}
        <div className="tiny muted mt12">
          {t('chat.sqlDisclaimer')}
        </div>
      </div>
    </div>
  )
}
