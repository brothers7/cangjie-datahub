/** ECharts 容器：按需引入 + resize 自适应 + 主题色跟随 CSS 变量。 */
import { useEffect, useRef, useState } from 'react'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import {
  DataZoomComponent, GridComponent, LegendComponent, MarkLineComponent, TooltipComponent,
  TitleComponent, ToolboxComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsOption } from 'echarts'

type ChartInstance = ReturnType<typeof echarts.init>

echarts.use([
  BarChart, LineChart, PieChart,
  GridComponent, TooltipComponent, LegendComponent, TitleComponent,
  DataZoomComponent, MarkLineComponent, ToolboxComponent,
  CanvasRenderer,
])

/** 订阅系统亮/暗色切换：返回递增 tick，供 option 的 useMemo 依赖触发主题色重建 */
export function useThemeTick(): number {
  const [tick, setTick] = useState(0)
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => setTick((n) => n + 1)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  return tick
}

export function Chart({ option, height = 300 }: { option: EChartsOption; height?: number }) {
  const ref = useRef<HTMLDivElement>(null)
  const inst = useRef<ChartInstance | null>(null)
  const themeTick = useThemeTick()

  useEffect(() => {
    if (!ref.current) return
    const chart = echarts.init(ref.current, undefined, { renderer: 'canvas' })
    inst.current = chart
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(ref.current)
    return () => {
      ro.disconnect()
      chart.dispose()
      inst.current = null
    }
  }, [])

  // themeTick 进依赖：系统主题切换时强制 setOption 重建（option 内烤入的 CSS 变量色随之刷新）
  useEffect(() => {
    inst.current?.setOption(option, true)
  }, [option, themeTick])

  return <div ref={ref} style={{ width: '100%', height }} />
}

/** 读取当前主题的 CSS 变量（跟随亮/暗色） */
export function themeColors() {
  const cs = getComputedStyle(document.documentElement)
  const v = (n: string, fb: string) => cs.getPropertyValue(n).trim() || fb
  return {
    text: v('--text', '#1c1f24'),
    dim: v('--text-dim', '#5c6472'),
    mute: v('--text-mute', '#8a929f'),
    border: v('--border', '#e2e5ea'),
    elev: v('--bg-elev', '#fff'),
    accent: v('--accent', '#2f6df6'),
    ok: v('--ok', '#1f9254'),
    danger: v('--danger', '#cf3f3f'),
    warn: v('--warn', '#b7791f'),
  }
}

/** 分类色板（与主题无关，明暗均可读） */
export const CATEGORICAL = [
  '#2f6df6', '#00a3a3', '#c2410c', '#7c3aed', '#0f766e',
  '#b45309', '#be185d', '#3f6212', '#0369a1', '#6d28d9',
]

export { echarts }
