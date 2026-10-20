/** 指标定义编辑器（受保护字段说明 + 值域校验）。 */
import { useState } from 'react'
import { api, ApiError } from '@/api/client'
import { Field, Modal } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import { toast } from '@/stores/app'
import type { Metric } from '@/types'

const AGG_OPTIONS = ['', 'sum', 'count', 'count_distinct', 'avg', 'max', 'min']
const DOMAINS = ['general', 'trade_daily', 'order_detail', 'gmv_trend', 'region_rank', 'channel_share', 'product', 'user', 'date']

export function MetricEditor({ metric, onClose, onSaved }: {
  metric: Metric
  onClose: () => void
  onSaved: () => void
}) {
  const { t } = useI18n()
  const [f, setF] = useState({
    name: metric.name,
    aliases: (metric.aliases || []).join(','),
    business_def: metric.business_def || '',
    formula: metric.formula || '',
    agg_function: metric.agg_function || '',
    unit: metric.unit || '',
    period_grain: metric.period_grain || '',
    business_domain: metric.business_domain || 'general',
    min: metric.value_range?.[0]?.toString() ?? '',
    max: metric.value_range?.[1]?.toString() ?? '',
  })
  const [busy, setBusy] = useState(false)
  const [rangeErr, setRangeErr] = useState('')

  const save = async () => {
    // 值域校验：任一填写则两者都必须是有效数字，且 min < max，否则不提交
    const minRaw = f.min.trim()
    const maxRaw = f.max.trim()
    let valueRange: [number, number] | undefined
    if (minRaw !== '' || maxRaw !== '') {
      const min = Number(minRaw)
      const max = Number(maxRaw)
      if (!Number.isFinite(min) || !Number.isFinite(max)) {
        setRangeErr(t('kg.rangeInvalid'))
        return
      }
      if (min >= max) {
        setRangeErr(t('kg.rangeOrder'))
        return
      }
      valueRange = [min, max]
    }
    setRangeErr('')
    setBusy(true)
    try {
      const payload: Record<string, unknown> = {
        name: f.name.trim(),
        aliases: f.aliases.split(',').map((s) => s.trim()).filter(Boolean),
        business_def: f.business_def,
        formula: f.formula,
        agg_function: f.agg_function || null,
        unit: f.unit,
        period_grain: f.period_grain || null,
        business_domain: f.business_domain,
      }
      if (valueRange) payload.value_range = valueRange
      await api.updateMetric(metric.id, payload)
      toast.ok(t('kg.metricSaved'))
      onSaved()
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={`${t('kg.editMetric')} · ${metric.name}`} width={640} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>{t('common.cancel')}</button>
        <button className="btn primary" disabled={busy} onClick={() => void save()}>{t('common.save')}</button>
      </>}>
      <div className="col" style={{ gap: 13 }}>
        <div className="tag accent" style={{ display: 'block', whiteSpace: 'normal' }}>
          {t('kg.protectedNote')}
        </div>
        <div className="grid2">
          <Field label={`${t('kg.metricName')} *`}>
            <input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
          </Field>
          <Field label={t('kg.aliasesLabel')} hint={t('kg.aliasesHint')}>
            <input className="input" value={f.aliases} onChange={(e) => setF({ ...f, aliases: e.target.value })} />
          </Field>
          <Field label={t('kg.aggFunc')}>
            <select className="select" value={f.agg_function}
              onChange={(e) => setF({ ...f, agg_function: e.target.value })}>
              {AGG_OPTIONS.map((a) => <option key={a} value={a}>{a || t('kg.unset')}</option>)}
            </select>
          </Field>
          <Field label={t('kg.unit')}>
            <input className="input" value={f.unit} placeholder={t('kg.unitPlaceholder')}
              onChange={(e) => setF({ ...f, unit: e.target.value })} />
          </Field>
          <Field label={t('kg.periodGrain')}>
            <select className="select" value={f.period_grain}
              onChange={(e) => setF({ ...f, period_grain: e.target.value })}>
              {['', 'day', 'week', 'month', 'quarter', 'year'].map((g) => (
                <option key={g} value={g}>{g || t('kg.unset')}</option>
              ))}
            </select>
          </Field>
          <Field label={t('kg.domain')}>
            <input className="input" list="domains" value={f.business_domain}
              onChange={(e) => setF({ ...f, business_domain: e.target.value })} />
            <datalist id="domains">{DOMAINS.map((d) => <option key={d} value={d} />)}</datalist>
          </Field>
          <Field label={t('kg.rangeMin')} hint={t('kg.rangeHint')}>
            <input className="input" value={f.min} onChange={(e) => setF({ ...f, min: e.target.value })} />
          </Field>
          <Field label={t('kg.rangeMax')}>
            <input className="input" value={f.max} onChange={(e) => setF({ ...f, max: e.target.value })} />
            {rangeErr && <span className="hint" style={{ color: '#cf3f3f' }}>{rangeErr}</span>}
          </Field>
        </div>
        <Field label={t('kg.defLabel')} hint={t('kg.defHint')}>
          <textarea className="textarea" rows={3} value={f.business_def}
            placeholder={t('kg.defPlaceholder')}
            onChange={(e) => setF({ ...f, business_def: e.target.value })} />
        </Field>
        <Field label={t('kg.formula')}>
          <input className="input mono" value={f.formula} placeholder="sum(pay_amt) - sum(refund_amt)"
            onChange={(e) => setF({ ...f, formula: e.target.value })} />
        </Field>
      </div>
    </Modal>
  )
}
