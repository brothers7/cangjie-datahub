/** 知识图谱：可视化 / 指标维度编辑 / 合并废弃 / 约束规则 / 指标-维度组合 / 质量校验。 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '@/api/client'
import { GraphCanvas } from '@/components/kg/GraphCanvas'
import {
  Confirm, Drawer, Empty, Field, Modal, Spinner, StatusTag, Tabs,
} from '@/components/common/ui'
import { useApp, toast } from '@/stores/app'
import { useI18n } from '@/i18n'
import type { Dimension, KgGraph, KgNode, KgRule, Metric, ValidationFinding } from '@/types'

const AGG_OPTIONS = ['', 'sum', 'count', 'count_distinct', 'avg', 'max', 'min']
const DOMAINS = ['general', 'trade_daily', 'order_detail', 'gmv_trend', 'region_rank', 'channel_share', 'product', 'user', 'date']
const DIM_TYPES = ['time', 'geo', 'channel', 'product', 'user_segment', 'dept', 'other']
/** 规则类型枚举；显示文案走 kg.ruleType.<type> 词条。 */
const RULE_TYPES = ['forbidden_dim_combo', 'metric_value_range', 'metric_agg_required', 'metric_def_required']
const SEVERITIES = ['block', 'warn']

/** 校验器返回的英文 category 清单；显示文案走 kg.finding.<category> 词条。 */
const FINDING_CATEGORIES = [
  'duplicate_metric', 'missing_comment', 'metric_agg_missing', 'metric_def_placeholder',
  'conflict_dim_rule', 'unreachable_dim_combo', 'invalid_rule_config',
]

export default function KnowledgeGraphPage() {
  const { t } = useI18n()
  const datasources = useApp((s) => s.datasources)
  const loadDatasources = useApp((s) => s.loadDatasources)

  const [tab, setTab] = useState<'graph' | 'metrics' | 'dimensions' | 'rules' | 'quality'>('graph')
  const [dsId, setDsId] = useState('')
  const [graph, setGraph] = useState<KgGraph>({ nodes: [], edges: [] })
  const [metrics, setMetrics] = useState<Metric[]>([])
  const [dims, setDims] = useState<Dimension[]>([])
  const [rules, setRules] = useState<KgRule[]>([])
  const [findings, setFindings] = useState<ValidationFinding[]>([])
  const [loading, setLoading] = useState(true)
  const [types, setTypes] = useState<Set<string>>(new Set(['metric', 'dimension', 'table']))
  const [selected, setSelected] = useState<KgNode | null>(null)
  const [search, setSearch] = useState('')
  const [editMetric, setEditMetric] = useState<Metric | null>(null)
  const [editDim, setEditDim] = useState<Dimension | null>(null)
  const [createRule, setCreateRule] = useState(false)
  const [editRule, setEditRule] = useState<KgRule | null>(null)
  const [confirmDel, setConfirmDel] = useState<{ kind: 'metric' | 'dimension'; id: string; name: string } | null>(null)
  const [delRule, setDelRule] = useState<KgRule | null>(null)
  const [mergeFrom, setMergeFrom] = useState<Metric | null>(null)
  // 大表格：表内过滤 + 分批渲染
  const [metricSearch, setMetricSearch] = useState('')
  const [dimSearch, setDimSearch] = useState('')
  const [metricLimit, setMetricLimit] = useState(100)
  const [dimLimit, setDimLimit] = useState(100)

  useEffect(() => { void loadDatasources().catch(() => undefined) }, [loadDatasources])

  // 请求序号：dsId 快速切换时丢弃旧响应，避免旧数据覆盖新数据
  const refreshSeq = useRef(0)
  const refresh = useCallback(async () => {
    const seq = ++refreshSeq.current
    setLoading(true)
    try {
      const [g, m, d] = await Promise.all([
        api.kgGraph({ datasource_id: dsId || undefined, limit: 500 }),
        api.listMetrics({ datasource_id: dsId || undefined }),
        api.listDimensions(),
      ])
      if (seq !== refreshSeq.current) return // 已有更新的请求发出，放弃本次结果
      setGraph(g)
      setMetrics(m)
      setDims(d)
    } catch (e) {
      if (seq === refreshSeq.current) toast.error((e as ApiError).message)
    } finally {
      if (seq === refreshSeq.current) setLoading(false)
    }
  }, [dsId])

  useEffect(() => { void refresh() }, [refresh])

  useEffect(() => {
    if (tab === 'rules') api.listRules().then(setRules).catch((e: ApiError) => toast.error(e.message))
    if (tab === 'quality') {
      api.validationReport(dsId || undefined).then(setFindings).catch((e: ApiError) => toast.error(e.message))
    }
  }, [tab, dsId])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return {
      metrics: q ? metrics.filter((m) => m.name.toLowerCase().includes(q)
        || m.aliases?.some((a) => a.toLowerCase().includes(q))
        || (m.business_def || '').toLowerCase().includes(q)) : metrics,
      dims: q ? dims.filter((d) => d.name.toLowerCase().includes(q)
        || (d.description || '').toLowerCase().includes(q)) : dims,
    }
  }, [metrics, dims, search])

  // 表内二次过滤（名称/定义），在全局搜索基础上再收窄
  const metricRows = useMemo(() => {
    const q = metricSearch.trim().toLowerCase()
    if (!q) return filtered.metrics
    return filtered.metrics.filter((m) => m.name.toLowerCase().includes(q)
      || (m.business_def || '').toLowerCase().includes(q))
  }, [filtered.metrics, metricSearch])
  const dimRows = useMemo(() => {
    const q = dimSearch.trim().toLowerCase()
    if (!q) return filtered.dims
    return filtered.dims.filter((d) => d.name.toLowerCase().includes(q)
      || (d.description || '').toLowerCase().includes(q))
  }, [filtered.dims, dimSearch])

  const toggleType = (t: string) => {
    setTypes((prev) => {
      const next = new Set(prev)
      next.has(t) ? next.delete(t) : next.add(t)
      return next
    })
  }

  return (
    <div className="col" style={{ gap: 16, height: '100%' }}>
      <div className="row wrap">
        <h1>{t('kg.title')}</h1>
        <span className="tag accent">{t('kg.metricCount', { n: metrics.length })}</span>
        <span className="tag accent">{t('kg.dimCount', { n: dims.length })}</span>
        <span className="tag">{t('kg.tableCount', { n: graph.nodes.filter((n) => n.type === 'table').length })}</span>
        <span className="spacer" />
        <select className="select" style={{ width: 220 }} value={dsId} onChange={(e) => setDsId(e.target.value)}>
          <option value="">{t('kg.allDs')}</option>
          {datasources.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
        <input className="input" style={{ width: 200 }} placeholder={t('kg.searchPlaceholder')}
          value={search} onChange={(e) => setSearch(e.target.value)} />
        <button className="btn" onClick={() => void refresh()}>{t('common.refresh')}</button>
      </div>

      <Tabs value={tab} onChange={setTab} items={[
        { key: 'graph', label: t('kg.tabGraph') },
        { key: 'metrics', label: t('kg.tabMetrics'), badge: metrics.length },
        { key: 'dimensions', label: t('kg.tabDims'), badge: dims.length },
        { key: 'rules', label: t('kg.tabRules'), badge: rules.length },
        { key: 'quality', label: t('kg.tabQuality') },
      ]} />

      {loading && <div className="center" style={{ padding: 40 }}><Spinner text={t('kg.loading')} /></div>}

      {!loading && tab === 'graph' && (
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 320px', gap: 16, flex: 1, minHeight: 0 }}>
          <div className="card" style={{ overflow: 'hidden', minHeight: 480 }}>
            <div className="card-head">
              <h2>{t('kg.entityGraph')}</h2>
              {graph.nodes.length >= 500 && (
                <span className="tiny muted">{t('kg.graphLimitNote')}</span>
              )}
              <span className="spacer" />
              {(['metric', 'dimension', 'table'] as const).map((ty) => (
                <label key={ty} className="row tiny" style={{ gap: 4, cursor: 'pointer' }}>
                  <input type="checkbox" checked={types.has(ty)} onChange={() => toggleType(ty)} />
                  {{ metric: t('kg.metric'), dimension: t('kg.dimension'), table: t('kg.table') }[ty]}
                </label>
              ))}
            </div>
            <div style={{ height: 'calc(100% - 49px)' }}>
              <GraphCanvas graph={graph} types={types} selectedId={selected?.id || null} onSelect={setSelected} />
            </div>
          </div>

          <div className="card" style={{ overflow: 'auto' }}>
            <div className="card-head"><h2>{t('kg.entityDetail')}</h2></div>
            {!selected ? (
              <Empty text={t('kg.clickNode')} icon="◎" />
            ) : (
              <EntityPanel node={selected} metrics={metrics} dims={dims} graph={graph}
                onEditMetric={(m) => setEditMetric(m)}
                onEditDim={(d) => setEditDim(d)} />
            )}
          </div>
        </div>
      )}

      {!loading && tab === 'metrics' && (
        <div className="card" style={{ overflow: 'auto' }}>
          <div className="row" style={{ padding: '10px 12px 0' }}>
            <input className="input" style={{ width: 240 }} placeholder={t('kg.tableFilterPlaceholder')}
              value={metricSearch} onChange={(e) => { setMetricSearch(e.target.value); setMetricLimit(100) }} />
          </div>
          <table className="table">
            <thead>
              <tr>
                <th>{t('kg.metricName')}</th><th>{t('kg.aliases')}</th><th>{t('kg.businessDef')}</th><th>{t('kg.agg')}</th>
                <th>{t('kg.unit')}</th><th>{t('kg.domain')}</th><th>{t('kg.source')}</th><th>{t('common.status')}</th><th style={{ width: 190 }}>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {metricRows.slice(0, metricLimit).map((m) => (
                <tr key={m.id}>
                  <td style={{ fontWeight: 550 }}>
                    {m.name}
                    {m.confidence !== null && m.confidence < 0.7 && (
                      <span className="tag warn" style={{ marginLeft: 6 }} title={t('kg.lowConfidence')}>{t('kg.needsReview')}</span>
                    )}
                  </td>
                  <td className="tiny muted">{(m.aliases || []).slice(0, 3).join(', ') || '-'}</td>
                  <td className="dim truncate" style={{ maxWidth: 300 }} title={m.business_def}>{m.business_def}</td>
                  <td>{m.agg_function ? <span className="tag">{m.agg_function}</span> : <span className="tag warn">{t('kg.missing')}</span>}</td>
                  <td>{m.unit || '-'}</td>
                  <td className="tiny">{m.business_domain}</td>
                  <td><StatusTag value={m.source} /></td>
                  <td><StatusTag value={m.status} /></td>
                  <td>
                    <div className="row" style={{ gap: 4 }}>
                      <button className="btn sm" onClick={() => setEditMetric(m)}>{t('common.edit')}</button>
                      <button className="btn sm" onClick={() => setMergeFrom(m)}>{t('kg.merge')}</button>
                      <button className="btn sm danger"
                        onClick={() => setConfirmDel({ kind: 'metric', id: m.id, name: m.name })}>{t('kg.deprecate')}</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {metricRows.length > metricLimit && (
            <div className="center" style={{ padding: 10 }}>
              <button className="btn sm" onClick={() => setMetricLimit((n) => n + 100)}>
                {t('kg.showMore', { n: metricRows.length - metricLimit })}
              </button>
            </div>
          )}
          {!metricRows.length && <Empty text={t('kg.noMetrics')} icon="🔍" />}
        </div>
      )}

      {!loading && tab === 'dimensions' && (
        <div className="card" style={{ overflow: 'auto' }}>
          <div className="row" style={{ padding: '10px 12px 0' }}>
            <input className="input" style={{ width: 240 }} placeholder={t('kg.tableFilterPlaceholder')}
              value={dimSearch} onChange={(e) => { setDimSearch(e.target.value); setDimLimit(100) }} />
          </div>
          <table className="table">
            <thead>
              <tr>
                <th>{t('kg.dimName')}</th><th>{t('common.type')}</th><th>{t('common.description')}</th><th>{t('kg.valueExamples')}</th>
                <th>{t('kg.relatedColumns')}</th><th>{t('kg.source')}</th><th style={{ width: 130 }}>{t('common.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {dimRows.slice(0, dimLimit).map((d) => (
                <tr key={d.id}>
                  <td style={{ fontWeight: 550 }}>{d.name}</td>
                  <td><span className="tag">{d.dimension_type}</span></td>
                  <td className="dim truncate" style={{ maxWidth: 260 }} title={d.description}>{d.description}</td>
                  <td className="tiny muted">{(d.value_examples || []).slice(0, 5).join(', ') || '-'}</td>
                  <td className="tiny mono">
                    {(d.related_columns || []).slice(0, 2).map((r) => `${r.table_name ?? ''}.${r.column_name ?? ''}`).join(', ') || '-'}
                  </td>
                  <td><StatusTag value={d.source} /></td>
                  <td>
                    <div className="row" style={{ gap: 4 }}>
                      <button className="btn sm" onClick={() => setEditDim(d)}>{t('common.edit')}</button>
                      <button className="btn sm danger"
                        onClick={() => setConfirmDel({ kind: 'dimension', id: d.id, name: d.name })}>{t('kg.deprecate')}</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {dimRows.length > dimLimit && (
            <div className="center" style={{ padding: 10 }}>
              <button className="btn sm" onClick={() => setDimLimit((n) => n + 100)}>
                {t('kg.showMore', { n: dimRows.length - dimLimit })}
              </button>
            </div>
          )}
          {!dimRows.length && <Empty text={t('kg.noDims')} icon="🔍" />}
        </div>
      )}

      {!loading && tab === 'rules' && (
        <div className="card">
          <div className="card-head">
            <h2>{t('kg.ruleTitle')}</h2>
            <span className="spacer" />
            <span className="tiny muted">{t('kg.ruleHint')}</span>
            <button className="btn primary sm" onClick={() => setCreateRule(true)}>{t('kg.addRule')}</button>
          </div>
          <div style={{ overflow: 'auto' }}>
            <table className="table">
              <thead>
                <tr><th>{t('kg.ruleType')}</th><th>{t('kg.ruleTarget')}</th><th>{t('kg.ruleConfig')}</th><th>{t('kg.severity')}</th><th>{t('kg.ruleMessage')}</th><th>{t('common.enable')}</th><th>{t('common.actions')}</th></tr>
              </thead>
              <tbody>
                {rules.map((r) => (
                  <tr key={r.id}>
                    <td>{RULE_TYPES.includes(r.rule_type) ? t(`kg.ruleType.${r.rule_type}`) : r.rule_type}</td>
                    <td className="tiny">{r.target_type === 'global' ? t('kg.global') : `${r.target_type}`}</td>
                    <td className="tiny mono truncate" style={{ maxWidth: 260 }}>{JSON.stringify(r.rule_config)}</td>
                    <td><span className={`tag ${r.severity === 'block' ? 'danger' : 'warn'}`}>
                      {r.severity === 'block' ? t('kg.block') : t('kg.warnLabel')}</span></td>
                    <td className="tiny dim">{r.message}</td>
                    <td>
                      <input type="checkbox" checked={r.enabled} onChange={async (e) => {
                        try {
                          await api.updateRule(r.id, { enabled: e.target.checked })
                          setRules((prev) => prev.map((x) => x.id === r.id ? { ...x, enabled: e.target.checked } : x))
                        } catch (err) { toast.error((err as ApiError).message) }
                      }} />
                    </td>
                    <td>
                      <div className="row" style={{ gap: 4 }}>
                        <button className="btn sm" onClick={() => setEditRule(r)}>{t('common.edit')}</button>
                        <button className="btn sm danger" onClick={() => setDelRule(r)}>{t('common.delete')}</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!rules.length && <Empty text={t('kg.noRules')} icon="⚖" />}
          </div>
        </div>
      )}

      {!loading && tab === 'quality' && (
        <div className="col" style={{ gap: 12 }}>
          <div className="row">
            <span className="tiny muted">{t('kg.qualityHint')}</span>
            <span className="spacer" />
            <button className="btn sm" onClick={() => api.validationReport(dsId || undefined)
              .then(setFindings).catch((e: ApiError) => toast.error(e.message))}>{t('kg.revalidate')}</button>
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
      )}

      {editMetric && (
        <MetricEditor metric={editMetric} onClose={() => setEditMetric(null)}
          onSaved={() => { setEditMetric(null); void refresh() }} />
      )}
      {editDim && (
        <DimEditor dim={editDim} onClose={() => setEditDim(null)}
          onSaved={() => { setEditDim(null); void refresh() }} />
      )}
      {(createRule || editRule) && (
        <RuleEditor rule={editRule} onClose={() => { setCreateRule(false); setEditRule(null) }}
          onSaved={() => {
            setCreateRule(false); setEditRule(null)
            api.listRules().then(setRules).catch(() => undefined)
          }} />
      )}
      {mergeFrom && (
        <MergeModal from={mergeFrom} metrics={metrics} onClose={() => setMergeFrom(null)}
          onDone={() => { setMergeFrom(null); void refresh() }} />
      )}
      {delRule && (
        <Confirm title={t('common.delete')} danger okText={t('common.delete')}
          text={t('kg.confirmDeleteRule')}
          onCancel={() => setDelRule(null)}
          onOk={async () => {
            const r = delRule
            setDelRule(null)
            try {
              await api.deleteRule(r.id)
              setRules((prev) => prev.filter((x) => x.id !== r.id))
              toast.ok(t('kg.ruleDeleted'))
            } catch (e) { toast.error((e as ApiError).message) }
          }} />
      )}
      {confirmDel && (
        <Confirm title={confirmDel.kind === 'metric' ? t('kg.deprecateMetric') : t('kg.deprecateDimension')} danger okText={t('kg.deprecate')}
          text={<>{t('kg.confirmDeprecatePre')}<b>{confirmDel.name}</b>{t('kg.confirmDeprecateSuffix')}<br />
            {t('kg.deprecateWarn')}</>}
          onCancel={() => setConfirmDel(null)}
          onOk={async () => {
            try {
              if (confirmDel.kind === 'metric') await api.deleteMetric(confirmDel.id)
              else await api.deleteDimension(confirmDel.id)
              toast.ok(t('kg.deprecated'))
              setConfirmDel(null)
              void refresh()
            } catch (e) { toast.error((e as ApiError).message) }
          }} />
      )}
    </div>
  )
}

// ---------------- 右侧实体详情 ----------------

function EntityPanel({ node, metrics, dims, graph, onEditMetric, onEditDim }: {
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

function Connections({ node, dims, graph }: {
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

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ marginBottom: 9 }}>
      <div className="tiny muted">{label}</div>
      <div style={{ fontSize: 13, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{value}</div>
    </div>
  )
}

// ---------------- 编辑器 ----------------

function MetricEditor({ metric, onClose, onSaved }: {
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

function DimEditor({ dim, onClose, onSaved }: { dim: Dimension; onClose: () => void; onSaved: () => void }) {
  const { t } = useI18n()
  const [f, setF] = useState({
    name: dim.name,
    aliases: (dim.aliases || []).join(','),
    description: dim.description || '',
    dimension_type: dim.dimension_type,
    values: (dim.value_examples || []).join(','),
  })
  const [busy, setBusy] = useState(false)

  const save = async () => {
    setBusy(true)
    try {
      await api.updateDimension(dim.id, {
        name: f.name.trim(),
        aliases: f.aliases.split(',').map((s) => s.trim()).filter(Boolean),
        description: f.description,
        dimension_type: f.dimension_type,
        value_examples: f.values.split(',').map((s) => s.trim()).filter(Boolean),
      })
      toast.ok(t('kg.dimSaved'))
      onSaved()
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={`${t('kg.editDim')} · ${dim.name}`} width={560} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>{t('common.cancel')}</button>
        <button className="btn primary" disabled={busy} onClick={() => void save()}>{t('common.save')}</button>
      </>}>
      <div className="col" style={{ gap: 13 }}>
        <div className="grid2">
          <Field label={`${t('kg.dimName')} *`}>
            <input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
          </Field>
          <Field label={t('kg.dimType')}>
            <select className="select" value={f.dimension_type}
              onChange={(e) => setF({ ...f, dimension_type: e.target.value })}>
              {DIM_TYPES.map((dt) => <option key={dt} value={dt}>{dt}</option>)}
            </select>
          </Field>
        </div>
        <Field label={t('kg.aliasesLabel')}>
          <input className="input" value={f.aliases} onChange={(e) => setF({ ...f, aliases: e.target.value })} />
        </Field>
        <Field label={t('common.description')}>
          <textarea className="textarea" rows={2} value={f.description}
            onChange={(e) => setF({ ...f, description: e.target.value })} />
        </Field>
        <Field label={t('kg.valuesLabel')} hint={t('kg.valuesHint')}>
          <input className="input" value={f.values} onChange={(e) => setF({ ...f, values: e.target.value })} />
        </Field>
      </div>
    </Modal>
  )
}

function RuleEditor({ rule, onClose, onSaved }: {
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

function MergeModal({ from, metrics, onClose, onDone }: {
  from: Metric
  metrics: Metric[]
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)
  const candidates = metrics.filter((m) => m.id !== from.id && m.status === 'active')

  const doMerge = async () => {
    if (!target) return
    setBusy(true)
    try {
      await api.mergeMetric(from.id, target)
      toast.ok(t('kg.merged'))
      onDone()
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={t('kg.mergeTitle')} width={480} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>{t('common.cancel')}</button>
        <button className="btn primary" disabled={!target || busy} onClick={() => void doMerge()}>{t('kg.confirmMerge')}</button>
      </>}>
      <div className="col" style={{ gap: 12 }}>
        <div className="tiny dim">
          {t('kg.mergePre')}<b>{from.name}</b>{t('kg.mergeMid')}<b>{t('kg.mergeBold')}</b>{t('kg.mergePost')}
        </div>
        <Field label={t('kg.targetMetric')}>
          <select className="select" value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">{t('kg.selectPlaceholder')}</option>
            {candidates.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </Field>
      </div>
    </Modal>
  )
}
