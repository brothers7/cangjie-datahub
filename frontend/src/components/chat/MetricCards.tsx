/** 指标口径卡（PRD 3.3-7）：库表与指标答疑命中 KG 指标时的结构化呈现。
 * 数据全部来自知识图谱登记（business_def/formula/聚合/粒度/单位/可查表），未登记如实标注。 */
import { useI18n } from '@/i18n'
import type { MetricCard } from '@/types'

const LAYER_ORDER: Record<string, number> = { ads: 0, dws: 1, dwd: 2, dim: 3, ods: 4 }

export function MetricCards({ cards }: { cards: MetricCard[] }) {
  const { t } = useI18n()
  if (!cards.length) return null
  return (
    <div className="col" style={{ gap: 8, marginBottom: 10 }}>
      {cards.map((c, i) => {
        const registered = Boolean(c.business_def || c.formula)
        return (
          // 同名指标可能出现多张卡（不同登记条目），key 加序号防重复
          <div key={`${c.name}-${i}`} className="card"
            style={{ borderLeft: '3px solid var(--accent)', marginTop: 0 }}>
            <div className="card-body" style={{ padding: 12 }}>
              <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
                <b style={{ fontSize: 14 }}>📐 {c.name}</b>
                {c.aliases.slice(0, 4).map((a) => <span key={a} className="tag tiny">{a}</span>)}
                <span className="spacer" />
                {c.business_domain && <span className="tag tiny">{c.business_domain}</span>}
                {!registered && <span className="tag tiny warn">{t('chat.metricUnregistered')}</span>}
              </div>

              {registered ? (
                <div className="mt8 col" style={{ gap: 4 }}>
                  {c.business_def && <div style={{ fontSize: 13 }}>{c.business_def}</div>}
                  {c.formula && (
                    <div className="tiny">{t('chat.formula')}<code>{c.formula}</code></div>
                  )}
                  <div className="tiny muted">
                    {[
                      c.agg_function && t('chat.aggFunc', { v: c.agg_function }),
                      c.period_grain && t('chat.grain', { v: c.period_grain }),
                      c.unit && t('chat.unit', { v: c.unit }),
                    ].filter(Boolean).join(' · ')}
                  </div>
                </div>
              ) : (
                <div className="tiny muted mt8">
                  {t('chat.metricUnregisteredHint')}
                </div>
              )}

              {c.tables.length > 0 && (
                <div className="mt8">
                  <div className="tiny dim mb8">{t('chat.queryableTables')}</div>
                  <div className="row" style={{ flexWrap: 'wrap', gap: 6 }}>
                    {c.tables.map((t) => (
                      <span key={t.name} className="tag" title={t.comment}>
                        <code>{t.name}</code>
                        <span className="tiny muted" style={{ marginLeft: 4 }}>
                          {t.layer.toUpperCase()}
                        </span>
                        {LAYER_ORDER[t.layer] <= 1 && <span style={{ marginLeft: 4 }}>⭐</span>}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
