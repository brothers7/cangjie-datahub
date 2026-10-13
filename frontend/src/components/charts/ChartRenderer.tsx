/** ChartSpec（后端契约）→ ECharts option + 数值卡片 + 明细表渲染。 */
import { useMemo } from 'react'
import { CATEGORICAL, Chart, themeColors } from '@/components/charts/Chart'
import type { ChartSpec } from '@/types'
import type { EChartsOption } from 'echarts'

const fmt = (v: number | null | undefined, unit = '') => {
  if (v === null || v === undefined || !Number.isFinite(v)) return '-'
  const abs = Math.abs(v)
  let s: string
  if (abs >= 1e8) s = (v / 1e8).toFixed(2) + ' 亿'
  else if (abs >= 1e4) s = (v / 1e4).toFixed(2) + ' 万'
  else if (Number.isInteger(v)) s = v.toLocaleString('zh-CN')
  else s = v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
  return unit ? `${s}${unit}` : s
}

function baseOption(spec: ChartSpec): EChartsOption {
  const c = themeColors()
  return {
    backgroundColor: 'transparent',
    textStyle: { color: c.dim, fontSize: 12 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: c.elev,
      borderColor: c.border,
      textStyle: { color: c.text, fontSize: 12 },
      axisPointer: { type: 'shadow' },
    },
    grid: { left: 8, right: 18, top: 34, bottom: 8, containLabel: true },
    legend: { top: 0, textStyle: { color: c.dim, fontSize: 11 }, itemHeight: 8, itemWidth: 14 },
  }
}

function buildOption(spec: ChartSpec): EChartsOption {
  const c = themeColors()
  const unit = spec.unit || ''

  if (spec.type === 'line') {
    const base = baseOption(spec)
    return {
      ...base,
      tooltip: { ...(base.tooltip as object), trigger: 'axis' },
      xAxis: {
        type: 'category', data: spec.x, boundaryGap: false,
        axisLine: { lineStyle: { color: c.border } },
        axisLabel: { color: c.mute, fontSize: 11, hideOverlap: true },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: c.mute, fontSize: 11, formatter: (v: number) => fmt(v) },
        splitLine: { lineStyle: { color: c.border, type: 'dashed' } },
      },
      dataZoom: spec.x.length > 20 ? [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 0 }] : undefined,
      series: spec.series.map((s, i) => ({
        name: s.name,
        type: 'line',
        data: s.data,
        smooth: true,
        symbol: 'circle',
        symbolSize: 5,
        showSymbol: spec.x.length <= 40,
        lineStyle: {
          width: s._dashed ? 1.6 : 2.2,
          type: s._dashed ? 'dashed' : 'solid',
          color: CATEGORICAL[i % CATEGORICAL.length],
        },
        itemStyle: { color: CATEGORICAL[i % CATEGORICAL.length] },
        areaStyle: i === 0 && spec.series.length <= 2
          ? { opacity: 0.12, color: CATEGORICAL[0] } : undefined,
        markLine: i === 0 && spec.anomalies.length
          ? {
            silent: true, symbol: 'none',
            label: { formatter: '异常', color: c.danger, fontSize: 10 },
            lineStyle: { color: c.danger, type: 'dotted', width: 1 },
            data: spec.anomalies.filter((a) => spec.x.includes(a.x))
              .map((a) => ({ xAxis: a.x, name: a.note })),
          }
          : undefined,
      })),
    }
  }

  if (spec.type === 'bar' || spec.type === 'hbar') {
    const base = baseOption(spec)
    const horizontal = spec.type === 'hbar'
    const cat = { type: 'category' as const, data: spec.x, axisLine: { lineStyle: { color: c.border } }, axisLabel: { color: c.dim, fontSize: 11 }, axisTick: { show: false } }
    const val = { type: 'value' as const, axisLabel: { color: c.mute, fontSize: 11, formatter: (v: number) => fmt(v) }, splitLine: { lineStyle: { color: c.border, type: 'dashed' as const } } }
    return {
      ...base,
      legend: { ...(base.legend as object), show: spec.series.length > 1 },
      grid: { left: 8, right: 24, top: spec.series.length > 1 ? 34 : 12, bottom: 8, containLabel: true },
      xAxis: horizontal ? val : cat,
      yAxis: horizontal ? { ...cat, inverse: true } : val,
      series: spec.series.map((s, i) => ({
        name: s.name,
        type: 'bar',
        data: s.data,
        barMaxWidth: 26,
        itemStyle: { color: CATEGORICAL[i % CATEGORICAL.length], borderRadius: horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0] },
        label: horizontal
          ? {
            show: spec.series.length === 1, position: 'right', color: c.dim, fontSize: 11,
            formatter: (p: unknown) => fmt((p as { value: number }).value, unit),
          }
          : undefined,
      })),
    }
  }

  if (spec.type === 'pie') {
    return {
      backgroundColor: 'transparent',
      textStyle: { color: c.dim, fontSize: 12 },
      tooltip: {
        trigger: 'item',
        backgroundColor: c.elev, borderColor: c.border, textStyle: { color: c.text, fontSize: 12 },
        formatter: (p: unknown) => {
          const d = p as { name: string; value: number; percent: number }
          return `${d.name}<br/>${fmt(d.value, unit)}（${d.percent}%）`
        },
      },
      legend: { type: 'scroll', orient: 'vertical', right: 8, top: 'middle', textStyle: { color: c.dim, fontSize: 11 } },
      color: CATEGORICAL,
      series: [{
        type: 'pie',
        radius: ['42%', '68%'],
        center: ['38%', '50%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: c.elev, borderWidth: 2 },
        label: { show: true, fontSize: 11, color: c.dim, formatter: '{b}\n{d}%' },
        data: spec.pies.map((p) => ({ name: p.name, value: p.value })),
      }],
    }
  }
  return {}
}

export function ChartRenderer({ spec }: { spec: ChartSpec }) {
  const option = useMemo(() => buildOption(spec), [spec])
  const unit = spec.unit || ''
  const c = themeColors()

  if (spec.type === 'stat_card') {
    return (
      <div className="stat-cards">
        {spec.cards.map((card) => (
          <div className="stat-card" key={card.label}>
            <div className="label">{card.label}</div>
            <div className="value">{fmt(card.value, card.unit ?? unit)}</div>
            {card.pct !== undefined && card.pct !== null && (
              <div className={`sub ${card.pct >= 0 ? 'delta-up' : 'delta-down'}`}>
                {card.pct >= 0 ? '▲' : '▼'} {Math.abs(card.pct)}%
                <span className="muted"> vs 上期</span>
              </div>
            )}
            {card.sub && <div className="sub">{card.sub}</div>}
          </div>
        ))}
      </div>
    )
  }

  if (spec.type === 'table' || (!option.series && spec.type !== 'pie')) {
    return (
      <div style={{ overflow: 'auto', maxHeight: 420 }}>
        <table className="table">
          <thead>
            <tr>{spec.columns.map((col) => <th key={col}>{col}</th>)}</tr>
          </thead>
          <tbody>
            {spec.rows.map((row, i) => (
              <tr key={i}>
                {row.map((v, j) => (
                  <td key={j} className={typeof v === 'number' ? 'num' : ''}>
                    {v === null || v === undefined ? '-' : typeof v === 'number' ? fmt(v) : String(v)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  return (
    <div>
      <Chart option={option} height={spec.type === 'pie' ? 320 : 300} />
      {spec.anomalies.length > 0 && (
        <div className="row wrap mt8" style={{ gap: 6 }}>
          <span className="tiny" style={{ color: c.danger }}>⚠ 异常点</span>
          {spec.anomalies.slice(0, 6).map((a, i) => (
            <span className="tag danger" key={i} title={a.note}>
              {a.x} {a.direction === 'up' ? '↑' : '↓'} {fmt(a.value)}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
