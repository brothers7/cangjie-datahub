/** 知识图谱画布：自实现力导向布局（canvas），支持拖拽 / 缩放 / 悬浮高亮 / 点选。 */
import { useEffect, useRef, useState } from 'react'
import { useI18n } from '@/i18n'
import type { KgEdge, KgNode } from '@/types'

const NODE_COLORS: Record<string, string> = {
  metric: '#2f6df6',
  dimension: '#0d9488',
  table: '#c2410c',
}
/** 边样式；labelKey 指向 kg.edge.* 词条，绘制时经 t() 解析。 */
const EDGE_STYLE: Record<string, { color: string; dash: number[]; labelKey: string }> = {
  METRIC_ON_TABLE: { color: '#2f6df6', dash: [], labelKey: 'kg.edge.METRIC_ON_TABLE' },
  DIM_ON_TABLE: { color: '#0d9488', dash: [], labelKey: 'kg.edge.DIM_ON_TABLE' },
  METRIC_DIM_ALLOWED: { color: '#8b93a1', dash: [3, 3], labelKey: 'kg.edge.METRIC_DIM_ALLOWED' },
  METRIC_DIM_FORBIDDEN: { color: '#cf3f3f', dash: [4, 3], labelKey: 'kg.edge.METRIC_DIM_FORBIDDEN' },
  TABLE_JOIN: { color: '#b45309', dash: [6, 4], labelKey: 'kg.edge.TABLE_JOIN' },
  METRIC_PARENT: { color: '#7c3aed', dash: [], labelKey: 'kg.edge.METRIC_PARENT' },
}

interface P { x: number; y: number; vx: number; vy: number; node: KgNode; fixed?: boolean }

export function GraphCanvas({ graph, selectedId, onSelect, types }: {
  graph: { nodes: KgNode[]; edges: KgEdge[] }
  selectedId?: string | null
  onSelect: (n: KgNode | null) => void
  types: Set<string>
}) {
  const { t } = useI18n()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<string | null>(null)
  const state = useRef({
    points: new Map<string, P>(),
    transform: { x: 0, y: 0, k: 1 },
    drag: null as { id: string; dx: number; dy: number } | null,
    pan: null as { x: number; y: number; tx: number; ty: number } | null,
    alpha: 1,
    raf: 0,
  })

  const visibleNodes = graph.nodes.filter((n) => types.has(n.type))
  const visibleIds = new Set(visibleNodes.map((n) => n.id))
  const visibleEdges = graph.edges.filter((e) => visibleIds.has(e.src) && visibleIds.has(e.dst))

  // 初始化/增量同步坐标
  useEffect(() => {
    const pts = state.current.points
    const seen = new Set<string>()
    visibleNodes.forEach((n, i) => {
      seen.add(n.id)
      if (!pts.has(n.id)) {
        const angle = (i / Math.max(visibleNodes.length, 1)) * Math.PI * 2
        const r = 180 + (i % 7) * 26
        const center = groupCenter(n, visibleNodes)
        pts.set(n.id, { x: center.x + Math.cos(angle) * r, y: center.y + Math.sin(angle) * r, vx: 0, vy: 0, node: n })
      } else {
        const p = pts.get(n.id)!
        p.node = n
      }
    })
    for (const id of [...pts.keys()]) if (!seen.has(id)) pts.delete(id)
    state.current.alpha = 1
  }, [graph, types])

  // ---- 物理与绘制 ----
  useEffect(() => {
    const canvas = canvasRef.current
    const wrap = wrapRef.current
    if (!canvas || !wrap) return

    const resize = () => {
      const dpr = window.devicePixelRatio || 1
      const rect = wrap.getBoundingClientRect()
      canvas.width = Math.max(1, rect.width * dpr)
      canvas.height = Math.max(1, rect.height * dpr)
      canvas.style.width = `${rect.width}px`
      canvas.style.height = `${rect.height}px`
      const ctx = canvas.getContext('2d')
      ctx?.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(wrap)

    const stepForce = () => {
      const { points, alpha } = state.current
      const list = [...points.values()]
      if (!list.length) return
      // 斥力
      for (let i = 0; i < list.length; i++) {
        for (let j = i + 1; j < list.length; j++) {
          const a = list[i], b = list[j]
          let dx = b.x - a.x, dy = b.y - a.y
          let d2 = dx * dx + dy * dy
          if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1 }
          const d = Math.sqrt(d2)
          const rep = (2600 * alpha) / d2
          const fx = (dx / d) * rep, fy = (dy / d) * rep
          a.vx -= fx; a.vy -= fy
          b.vx += fx; b.vy += fy
        }
      }
      // 弹簧
      const spring = 0.035 * alpha
      const restLen = 105
      for (const e of visibleEdges) {
        const a = points.get(e.src), b = points.get(e.dst)
        if (!a || !b) continue
        const dx = b.x - a.x, dy = b.y - a.y
        const d = Math.max(Math.hypot(dx, dy), 1)
        const f = (d - restLen) * spring
        const fx = (dx / d) * f, fy = (dy / d) * f
        a.vx += fx; a.vy += fy
        b.vx -= fx; b.vy -= fy
      }
      // 向心 + 阻尼
      for (const p of list) {
        if (p.fixed) { p.vx = 0; p.vy = 0; continue }
        p.vx -= p.x * 0.0009 * alpha
        p.vy -= p.y * 0.0009 * alpha
        p.vx *= 0.82; p.vy *= 0.82
        p.x += Math.max(-22, Math.min(22, p.vx))
        p.y += Math.max(-22, Math.min(22, p.vy))
      }
    }

    const draw = () => {
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      const cs = getComputedStyle(document.documentElement)
      const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches
      const textColor = isDark ? '#e7eaee' : '#1c1f24'
      const dimColor = isDark ? '#7b8592' : '#8a929f'
      const bg = cs.getPropertyValue('--bg-elev').trim() || '#fff'

      ctx.save()
      ctx.fillStyle = bg
      // setTransform(dpr,…) 后绘制坐标一律用 CSS 像素（canvas.width 是设备像素，会偏 2×）
      const cw = canvas.clientWidth || canvas.width
      const ch = canvas.clientHeight || canvas.height
      ctx.fillRect(0, 0, cw, ch)
      const tr = state.current.transform
      ctx.translate(cw / 2 + tr.x, ch / 2 + tr.y)
      ctx.scale(tr.k, tr.k)

      const points = state.current.points
      const focus = hover || selectedId
      const neighbors = new Set<string>()
      if (focus) {
        neighbors.add(focus)
        for (const e of visibleEdges) {
          if (e.src === focus) neighbors.add(e.dst)
          if (e.dst === focus) neighbors.add(e.src)
        }
      }

      // 边
      for (const e of visibleEdges) {
        const a = points.get(e.src), b = points.get(e.dst)
        if (!a || !b) continue
        const raw = EDGE_STYLE[e.edge_type]
        const st = raw
          ? { color: raw.color, dash: raw.dash, label: t(raw.labelKey) }
          : { color: '#9aa3b0', dash: [], label: e.edge_type }
        const faded = focus ? !(neighbors.has(e.src) && neighbors.has(e.dst)) : false
        ctx.globalAlpha = faded ? 0.12 : 0.55
        ctx.strokeStyle = st.color
        ctx.lineWidth = (e.edge_type === 'METRIC_ON_TABLE' || e.edge_type === 'DIM_ON_TABLE') ? 1.4 : 1
        ctx.setLineDash(st.dash)
        ctx.beginPath()
        ctx.moveTo(a.x, a.y)
        ctx.lineTo(b.x, b.y)
        ctx.stroke()
        ctx.setLineDash([])
      }

      // 节点
      for (const p of points.values()) {
        const n = p.node
        const isFocus = (hover || selectedId) === n.id
        const faded = focus ? !neighbors.has(n.id) : false
        const r = n.type === 'table' ? 8 : n.type === 'metric' ? 6.5 : 5.5
        ctx.globalAlpha = faded ? 0.18 : 1
        ctx.beginPath()
        ctx.arc(p.x, p.y, isFocus ? r + 2.5 : r, 0, Math.PI * 2)
        ctx.fillStyle = NODE_COLORS[n.type] || '#888'
        ctx.fill()
        if (isFocus) {
          ctx.lineWidth = 2
          ctx.strokeStyle = textColor
          ctx.stroke()
        }
        // 标签：缩放足够或悬浮/选中时显示
        if (tr.k > 0.85 || isFocus) {
          ctx.globalAlpha = faded ? 0.25 : 1
          ctx.font = `${isFocus ? '600 ' : ''}11px -apple-system, "PingFang SC", sans-serif`
          ctx.fillStyle = isFocus ? textColor : dimColor
          ctx.textAlign = 'center'
          const label = n.name.length > 12 ? n.name.slice(0, 12) + '…' : n.name
          ctx.fillText(label, p.x, p.y - r - 5)
        }
      }
      ctx.globalAlpha = 1
      ctx.restore()
    }

    const frame = () => {
      if (state.current.alpha > 0.008) {
        stepForce()
        state.current.alpha *= 0.985
      }
      draw()
      state.current.raf = requestAnimationFrame(frame)
    }
    state.current.raf = requestAnimationFrame(frame)

    return () => {
      cancelAnimationFrame(state.current.raf)
      ro.disconnect()
    }
  }, [visibleEdges, visibleNodes, hover, selectedId, t])

  // ---- 交互 ----
  const toWorld = (clientX: number, clientY: number) => {
    const canvas = canvasRef.current!
    const rect = canvas.getBoundingClientRect()
    const t = state.current.transform
    return {
      x: (clientX - rect.left - rect.width / 2 - t.x) / t.k,
      y: (clientY - rect.top - rect.height / 2 - t.y) / t.k,
    }
  }

  const pick = (cx: number, cy: number): P | null => {
    const { x, y } = toWorld(cx, cy)
    let best: P | null = null
    let bestD = 14 / state.current.transform.k
    for (const p of state.current.points.values()) {
      const d = Math.hypot(p.x - x, p.y - y)
      if (d < bestD) { bestD = d; best = p }
    }
    return best
  }

  const onDown = (e: React.MouseEvent) => {
    const hit = pick(e.clientX, e.clientY)
    if (hit) {
      const w = toWorld(e.clientX, e.clientY)
      state.current.drag = { id: hit.node.id, dx: w.x - hit.x, dy: w.y - hit.y }
      hit.fixed = true
      state.current.alpha = Math.max(state.current.alpha, 0.35)
    } else {
      state.current.pan = { x: e.clientX, y: e.clientY, tx: state.current.transform.x, ty: state.current.transform.y }
    }
  }

  const onMove = (e: React.MouseEvent) => {
    const st = state.current
    if (st.drag) {
      const p = st.points.get(st.drag.id)
      if (p) {
        const w = toWorld(e.clientX, e.clientY)
        p.x = w.x - st.drag.dx
        p.y = w.y - st.drag.dy
      }
      return
    }
    if (st.pan) {
      st.transform.x = st.pan.tx + (e.clientX - st.pan.x)
      st.transform.y = st.pan.ty + (e.clientY - st.pan.y)
      return
    }
    const hit = pick(e.clientX, e.clientY)
    const id = hit?.node.id || null
    if (id !== hover) setHover(id)
  }

  const onUp = () => {
    const st = state.current
    if (st.drag) {
      const p = st.points.get(st.drag.id)
      if (p) p.fixed = false
      const hit = st.points.get(st.drag.id)
      if (hit) onSelect(hit.node)
      st.drag = null
    }
    if (st.pan && Math.abs(st.pan.x) && st.pan.tx === st.transform.x && st.pan.ty === st.transform.y) {
      // 点击空白 → 取消选择
      onSelect(null)
    }
    st.pan = null
  }

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault()
    const st = state.current
    const k = Math.max(0.25, Math.min(3, st.transform.k * (e.deltaY < 0 ? 1.12 : 0.89)))
    st.transform.k = k
  }

  return (
    <div className="kg-canvas" ref={wrapRef}>
      <canvas ref={canvasRef} style={{ display: 'block', width: '100%', height: '100%', cursor: hover ? 'pointer' : 'grab' }}
        onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp} onMouseLeave={onUp}
        onWheel={onWheel} />
      <div className="kg-legend">
        <span><i style={{ background: NODE_COLORS.metric }} />{t('kg.metric')} {graph.nodes.filter((n) => n.type === 'metric').length}</span>
        <span><i style={{ background: NODE_COLORS.dimension }} />{t('kg.dimension')} {graph.nodes.filter((n) => n.type === 'dimension').length}</span>
        <span><i style={{ background: NODE_COLORS.table }} />{t('kg.table')} {graph.nodes.filter((n) => n.type === 'table').length}</span>
        <span className="muted">{t('kg.canvasHint')}</span>
      </div>
    </div>
  )
}

function groupCenter(n: KgNode, all: KgNode[]) {
  if (n.type === 'metric') return { x: -120, y: -60 }
  if (n.type === 'dimension') return { x: 140, y: -40 }
  const tables = all.filter((t) => t.type === 'table')
  return { x: 0, y: tables.length ? 120 : 60 }
}
