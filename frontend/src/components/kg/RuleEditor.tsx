/** 约束规则编辑器（新建/编辑共用；类型创建后不可改）。 */
import { useState } from 'react'
import { api, ApiError } from '@/api/client'
import { Field, Modal } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import { toast } from '@/stores/app'
import type { KgRule } from '@/types'

/** 规则类型枚举；显示文案走 kg.ruleType.<type> 词条。 */
export const RULE_TYPES = ['forbidden_dim_combo', 'metric_value_range', 'metric_agg_required', 'metric_def_required']
const SEVERITIES = ['block', 'warn']

export function RuleEditor({ rule, onClose, onSaved }: {
  rule: KgRule | null
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useI18n()
  const [f, setF] = useState({
    rule_type: rule?.rule_type || 'forbidden_dim_combo',
    target_type: rule?.target_type || 'global',
    severity: rule?.severity || 'block',
    message: rule?.message || '',
    config: JSON.stringify(rule?.rule_config || { dims: [] }, null, 2),
    enabled: rule?.enabled ?? true,
  })
  const [busy, setBusy] = useState(false)

  const save = async () => {
    let config: Record<string, unknown>
    try {
      config = JSON.parse(f.config)
    } catch {
      toast.error(t('kg.invalidJson'))
      return
    }
    setBusy(true)
    try {
      if (rule) {
        await api.updateRule(rule.id, {
          rule_config: config, severity: f.severity, message: f.message, enabled: f.enabled,
        })
      } else {
        await api.createRule({
          rule_type: f.rule_type, target_type: f.target_type, rule_config: config,
          severity: f.severity, message: f.message, enabled: f.enabled,
        })
      }
      toast.ok(t('kg.ruleSaved'))
      onSaved()
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={rule ? t('kg.editRuleTitle') : t('kg.addRuleTitle')} width={560} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>{t('common.cancel')}</button>
        <button className="btn primary" disabled={busy} onClick={() => void save()}>{t('common.save')}</button>
      </>}>
      <div className="col" style={{ gap: 13 }}>
        <div className="grid2">
          <Field label={t('kg.ruleType')}>
            <select className="select" value={f.rule_type} disabled={!!rule}
              onChange={(e) => setF({ ...f, rule_type: e.target.value })}>
              {RULE_TYPES.map((k) => <option key={k} value={k}>{t(`kg.ruleType.${k}`)}</option>)}
            </select>
          </Field>
          <Field label={t('kg.severityLabel')}>
            <select className="select" value={f.severity}
              onChange={(e) => setF({ ...f, severity: e.target.value as 'block' | 'warn' })}>
              {SEVERITIES.map((s) => <option key={s} value={s}>{s === 'block' ? t('kg.blockLong') : t('kg.warnLong')}</option>)}
            </select>
          </Field>
        </div>
        <Field label={t('kg.ruleMessage')} hint={t('kg.messageHint')}>
          <input className="input" value={f.message} placeholder={t('kg.messagePlaceholder')}
            onChange={(e) => setF({ ...f, message: e.target.value })} />
        </Field>
        <Field label={t('kg.ruleConfig')}
          hint={t('kg.configHint')}>
          <textarea className="textarea mono" rows={5} value={f.config}
            onChange={(e) => setF({ ...f, config: e.target.value })} />
        </Field>
        <label className="row tiny" style={{ gap: 6 }}>
          <input type="checkbox" checked={f.enabled}
            onChange={(e) => setF({ ...f, enabled: e.target.checked })} /> {t('kg.enableRule')}
        </label>
      </div>
    </Modal>
  )
}
