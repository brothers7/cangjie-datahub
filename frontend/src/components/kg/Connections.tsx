/** 指标-维度组合规则：按维度切换 允许/禁止/自动。 */
import { useMemo, useState } from 'react'
import { api, ApiError } from '@/api/client'
import { useI18n } from '@/i18n'
import { toast } from '@/stores/app'
import type { Dimension, KgGraph, KgNode } from '@/types'

export function Connections({ node, dims, graph }: {
  node: KgNode
  dims: Dimension[]
  graph: KgGraph
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState('')
  const [override, setOverride] = useState<Record<string, boolean | null>>({})

  const states = useMemo(() => {
    const map = new Map<string, boolean | null>()
    for (const e of graph.edges) {
      if (e.src !== node.id && e.dst !== node.id) continue
      const other = e.src === node.id ? e.dst : e.src
      if (e.edge_type === 'METRIC_DIM_ALLOWED') map.set(other, true)
      if (e.edge_type === 'METRIC_DIM_FORBIDDEN') map.set(other, false)
    }
    return map
  }, [graph.edges, node.id])

  const setRule = async (dimId: string, allowed: boolean | null) => {
    setBusy(dimId)
    try {
      await api.setMetricDimRule(node.id, dimId, allowed)
      setOverride((prev) => ({ ...prev, [dimId]: allowed }))
      toast.ok(allowed === null ? t('kg.toastAuto') : allowed ? t('kg.toastAllowed') : t('kg.toastForbidden'))
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="mt16">
      <div className="tiny muted mb8">
        {t('kg.comboRuleHint')}
      </div>
      <div style={{ maxHeight: 220, overflow: 'auto' }}>
        {dims.map((d) => {
          const state = d.id in override ? override[d.id] : (states.has(d.id) ? states.get(d.id)! : null)
          return (
            <div key={d.id} className="row" style={{ gap: 6, padding: '3px 0', fontSize: 12.5 }}>
              <span className="truncate" style={{ flex: 1 }}>{d.name}</span>
              {(['allow', 'forbid', 'auto'] as const).map((mode) => {
                const active = (mode === 'auto' && state === null)
                  || (mode === 'allow' && state === true)
                  || (mode === 'forbid' && state === false)
                return (
                  <button key={mode} disabled={busy === d.id}
                    className={`btn sm ${active ? (mode === 'forbid' ? 'danger' : mode === 'allow' ? 'primary' : '') : 'ghost'}`}
                    onClick={() => void setRule(d.id, mode === 'auto' ? null : mode === 'allow')}>
                    {mode === 'auto' ? t('kg.auto') : mode === 'allow' ? t('kg.allowed') : t('kg.forbidden')}
                  </button>
                )
              })}
            </div>
          )
        })}
      </div>
    </div>
  )
}
