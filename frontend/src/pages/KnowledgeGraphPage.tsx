/** 知识图谱：可视化 / 指标维度编辑 / 合并废弃 / 约束规则 / 指标-维度组合 / 质量校验。 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '@/api/client'
import { GraphCanvas } from '@/components/kg/GraphCanvas'
import { EntityPanel } from '@/components/kg/EntityPanel'
import { MetricEditor } from '@/components/kg/MetricEditor'
import { DimEditor } from '@/components/kg/DimEditor'
import { RuleEditor, RULE_TYPES } from '@/components/kg/RuleEditor'
import { MergeModal } from '@/components/kg/MergeModal'
import { QualityPanel } from '@/components/kg/QualityPanel'
import {
  Confirm, Empty, Spinner, StatusTag, Tabs,
} from '@/components/common/ui'
import { useApp, toast } from '@/stores/app'
import { useI18n } from '@/i18n'
import type { Dimension, KgGraph, KgNode, KgRule, Metric, ValidationFinding } from '@/types'

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
      if (next.has(t)) next.delete(t)
      else next.add(t)
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
        <QualityPanel findings={findings} onRevalidate={() => api.validationReport(dsId || undefined)
          .then(setFindings).catch((e: ApiError) => toast.error(e.message))} />
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

