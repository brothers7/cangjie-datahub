/** 图谱右侧实体详情面板：按节点类型展示指标/维度/表信息。 */
import { useEffect, useState } from 'react'
import { api, ApiError } from '@/api/client'
import { Drawer } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import { toast } from '@/stores/app'
import type { Dimension, KgGraph, KgNode, Metric } from '@/types'
import { Connections } from './Connections'

export function EntityPanel({ node, metrics, dims, graph, onEditMetric, onEditDim }: {
  node: KgNode
  metrics: Metric[]
  dims: Dimension[]
  graph: KgGraph
  onEditMetric: (m: Metric) => void
  onEditDim: (d: Dimension) => void
}) {
  const { t } = useI18n()
  const metric = node.type === 'metric' ? metrics.find((m) => m.id === node.id) : undefined
  const dim = node.type === 'dimension' ? dims.find((d) => d.id === node.id) : undefined
  const [ctx, setCtx] = useState('')
  const [showCtx, setShowCtx] = useState(false)

  useEffect(() => { setCtx(''); setShowCtx(false) }, [node.id])

  return (
    <div className="card-body">
      <div className="row mb8">
        <span className="tag" style={{
          background: node.type === 'metric' ? '#2f6df6' : node.type === 'dimension' ? '#0d9488' : '#c2410c',
          color: '#fff', borderColor: 'transparent',
        }}>
          {{ metric: t('kg.metric'), dimension: t('kg.dimension'), table: t('kg.table') }[node.type]}
        </span>
        <span style={{ fontWeight: 600, fontSize: 14 }}>{node.name}</span>
      </div>

      {metric && (
        <>
          <Info label={t('kg.businessDef')} value={metric.business_def || t('kg.notFilled')} />
          <Info label={t('kg.formula')} value={metric.formula || '-'} />
          <Info label={t('kg.aggFunc')} value={metric.agg_function || t('kg.notDetected')} />
          <Info label={t('kg.unit')} value={metric.unit || '-'} />
          <Info label={t('kg.domain')} value={metric.business_domain} />
          <Info label={t('kg.aliases')} value={(metric.aliases || []).join(', ') || '-'} />
          <Info label={t('kg.editedFields')} value={(metric.edited_fields || []).join(', ') || t('common.none')} />
          <div className="row mt12" style={{ gap: 6 }}>
            <button className="btn sm primary" onClick={() => onEditMetric(metric)}>{t('kg.editDef')}</button>
            <button className="btn sm" onClick={async () => {
              try {
                const r = await api.metricContext(metric.id)
                setCtx(r.context)
                setShowCtx(true)
              } catch (e) { toast.error((e as ApiError).message) }
            }}>{t('kg.viewLlmContext')}</button>
          </div>
          <Connections node={node} dims={dims} graph={graph} />
        </>
      )}

      {dim && (
        <>
          <Info label={t('common.description')} value={dim.description || '-'} />
          <Info label={t('kg.dimType')} value={dim.dimension_type} />
          <Info label={t('kg.enumExamples')} value={(dim.value_examples || []).slice(0, 12).join(', ') || '-'} />
          <Info label={t('kg.relatedColumns')}
            value={(dim.related_columns || []).map((r) => `${r.table_name ?? ''}.${r.column_name ?? ''}`).join('\n') || '-'} />
          <div className="row mt12">
            <button className="btn sm primary" onClick={() => onEditDim(dim)}>{t('kg.editDim')}</button>
          </div>
        </>
      )}

      {node.type === 'table' && (
        <>
          <Info label={t('kg.dwLayer')} value={node.layer || '-'} />
          <Info label={t('kg.tableComment')} value={node.comment || '-'} />
          <div className="tiny muted mt8">{t('kg.tableNodeNote')}</div>
        </>
      )}

      {showCtx && (
        <Drawer title={t('kg.llmContextTitle')} sub={node.name} onClose={() => setShowCtx(false)}>
          <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12.5, lineHeight: 1.6 }}>{ctx}</pre>
        </Drawer>
      )}
    </div>
  )
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ marginBottom: 9 }}>
      <div className="tiny muted">{label}</div>
      <div style={{ fontSize: 13, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{value}</div>
    </div>
  )
}
