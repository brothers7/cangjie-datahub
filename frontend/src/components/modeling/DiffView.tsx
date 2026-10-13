/** 行级 diff 对照（AI 修订预览，PRD 3.3D-3）：新增绿 / 删除红 / 相同灰 + SQL 关键字高亮。 */
import { useMemo } from 'react'
import { SqlText } from '@/components/common/SqlText'
import { useI18n } from '@/i18n'

interface DiffLine { kind: 'same' | 'add' | 'del'; text: string }

/** 经典 LCS 行 diff（DDL/SQL 量级足够）。 */
export function diffLines(oldText: string, newText: string): DiffLine[] {
  const a = oldText.split('\n')
  const b = newText.split('\n')
  const n = a.length, m = b.length
  // dp[i][j] = a[i:] 与 b[j:] 的 LCS 长度
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
  const out: DiffLine[] = []
  let i = 0, j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ kind: 'same', text: a[i] }); i++; j++ }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ kind: 'del', text: a[i] }); i++ }
    else { out.push({ kind: 'add', text: b[j] }); j++ }
  }
  while (i < n) out.push({ kind: 'del', text: a[i++] })
  while (j < m) out.push({ kind: 'add', text: b[j++] })
  return out
}

const STYLE: Record<DiffLine['kind'], React.CSSProperties> = {
  same: { color: 'var(--muted)' },
  add: { background: 'rgba(52, 199, 123, .14)', color: 'var(--ok)' },
  del: { background: 'rgba(255, 92, 92, .12)', color: 'var(--danger)', textDecoration: 'line-through' },
}
const PREFIX: Record<DiffLine['kind'], string> = { same: '  ', add: '+ ', del: '- ' }

export function DiffView({ oldText, newText, maxLines = 60 }: {
  oldText: string; newText: string; maxLines?: number
}) {
  const { t } = useI18n()
  const lines = useMemo(() => diffLines(oldText, newText), [oldText, newText])
  const changed = lines.filter((l) => l.kind !== 'same').length
  if (changed === 0) return <div className="tiny muted">{t('modeling.noDiff')}</div>
  const shown = lines.slice(0, maxLines)
  return (
    <pre className="mono" style={{
      fontSize: 12, lineHeight: 1.6, margin: 0, padding: 8, overflow: 'auto',
      background: 'var(--bg)', borderRadius: 6, border: '1px solid var(--border)',
    }}>
      {shown.map((l, i) => (
        <div key={i} style={STYLE[l.kind]}>{PREFIX[l.kind]}<SqlText code={l.text || ' '} /></div>
      ))}
      {lines.length > maxLines && (
        <div className="tiny muted">{t('modeling.diffOmitted', { n: lines.length - maxLines })}</div>
      )}
    </pre>
  )
}
