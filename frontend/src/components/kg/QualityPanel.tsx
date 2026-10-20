/** 质量校验面板：校验结果表格 + 重新校验。 */
import { Empty } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import type { ValidationFinding } from '@/types'

/** 校验器返回的英文 category 清单；显示文案走 kg.finding.<category> 词条。 */
const FINDING_CATEGORIES = [
  'duplicate_metric', 'missing_comment', 'metric_agg_missing', 'metric_def_placeholder',
  'conflict_dim_rule', 'unreachable_dim_combo', 'invalid_rule_config',
]

export function QualityPanel({ findings, onRevalidate }: {
  findings: ValidationFinding[]
  onRevalidate: () => void
}) {
  const { t } = useI18n()
  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="row">
        <span className="tiny muted">{t('kg.qualityHint')}</span>
        <span className="spacer" />
        <button className="btn sm" onClick={onRevalidate}>{t('kg.revalidate')}</button>
      </div>
      <div className="card">
        {!findings.length ? <Empty text={t('kg.noFindings')} icon="✓" /> : (
          <table className="table">
            <thead><tr><th>{t('kg.severity')}</th><th>{t('kg.category')}</th><th>{t('kg.object')}</th><th>{t('kg.issue')}</th><th>{t('kg.suggestion')}</th></tr></thead>
            <tbody>
              {findings.map((f, i) => (
                <tr key={`${f.category}-${f.entity_id}-${i}`}>
                  <td><span className={`tag ${f.severity === 'warn' ? 'warn' : 'danger'}`}>
                    {f.severity === 'warn' ? t('kg.warnLabel') : t('kg.critical')}</span></td>
                  <td className="tiny" title={f.category}>
                    {FINDING_CATEGORIES.includes(f.category) ? t(`kg.finding.${f.category}`) : f.category}</td>
                  <td className="tiny">
                    {f.entity_name || f.entity_id}
                    <span className="muted"> · {f.entity_type}</span>
                  </td>
                  <td className="dim">{f.message}</td>
                  <td className="tiny muted">{f.suggestion || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
