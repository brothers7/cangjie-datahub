/** 仓颉 Cangjie 品牌 Logo（纯 SVG，无外部图片素材）。
 * 设计：「仓」字解构——屋顶（人字头）+ 四层渐窄层板 = 数仓分层（ODS→DWD→DWS→ADS）；
 * 右上角星火代表 AI。渐变：靛蓝 → 青，寓意从数据到洞察。 */
import { useId } from 'react'

export function Logo({ size = 28, glow = false }: { size?: number; glow?: boolean }) {
  const gid = useId().replace(/:/g, '')
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" role="img" aria-label="仓颉 Cangjie"
      style={glow ? { filter: 'drop-shadow(0 4px 14px rgba(99,102,241,.45))' } : undefined}>
      <defs>
        <linearGradient id={`cj-${gid}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#6366f1" />
          <stop offset="55%" stopColor="#4f7cff" />
          <stop offset="100%" stopColor="#22d3ee" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="44" height="44" rx="12" fill={`url(#cj-${gid})`} />
      {/* 屋顶：「仓」的人字头 */}
      <path d="M15 17.5 L24 10.5 L33 17.5" fill="none" stroke="#fff" strokeWidth="3.2"
        strokeLinecap="round" strokeLinejoin="round" />
      {/* 分层：ODS → DWD → DWS → ADS 渐窄 */}
      <rect x="13.5" y="21.5" width="21" height="3.4" rx="1.7" fill="#fff" opacity="0.95" />
      <rect x="16" y="28" width="16" height="3.4" rx="1.7" fill="#fff" opacity="0.8" />
      <rect x="18.5" y="34.5" width="11" height="3.4" rx="1.7" fill="#fff" opacity="0.65" />
      {/* AI 星火 */}
      <path d="M37.5 8.6 l1.15 2.45 2.45 1.15 -2.45 1.15 -1.15 2.45 -1.15 -2.45 -2.45 -1.15 2.45 -1.15 z"
        fill="#fef08a" />
    </svg>
  )
}
