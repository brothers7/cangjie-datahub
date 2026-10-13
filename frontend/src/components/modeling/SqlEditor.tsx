/** SQL 编辑器（PRD 3.3D-2）：textarea + 实时代码提示（关键字/表名/字段名，含中文注释）。
 *
 * 轻量实现：跟踪光标前的当前词，匹配候选列表；↓/↑ 选择，Tab/Enter/点击插入。
 * 候选：SQL 关键字 + 元数据表名 + 「表名.」触发该表字段。
 */
import { useMemo, useRef, useState } from 'react'
import { SqlText } from '@/components/common/SqlText'
import { useI18n } from '@/i18n'
import type { CompletionData } from '@/types'

interface Suggestion {
  insert: string
  label: string
  detail: string
  kind: 'keyword' | 'table' | 'column'
}

export function SqlEditor({ value, onChange, data, placeholder, rows = 10 }: {
  value: string
  onChange: (v: string) => void
  data: CompletionData | null
  placeholder?: string
  rows?: number
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  const hlRef = useRef<HTMLPreElement>(null)  // 高亮底层（与 textarea 滚动同步）
  const { t: tr } = useI18n()  // 命名为 tr：useMemo 内局部变量 t 表示元数据表
  const [word, setWord] = useState('')
  const [open, setOpen] = useState(false)
  const [sel, setSel] = useState(0)

  const suggestions = useMemo<Suggestion[]>(() => {
    if (!word || word.length < 1 || !data) return []
    const w = word.toLowerCase()
    const out: Suggestion[] = []
    // 「表名.」→ 该表字段
    const dot = w.indexOf('.')
    if (dot > 0) {
      const tname = w.slice(0, dot)
      const colPrefix = w.slice(dot + 1)
      const t = data.tables.find((x) => x.name.toLowerCase() === tname)
      if (t) {
        for (const c of t.columns) {
          if (!colPrefix || c.name.toLowerCase().startsWith(colPrefix)) {
            out.push({ insert: `${t.name}.${c.name}`, label: c.name,
                       detail: `${c.type}${c.comment ? ' · ' + c.comment : ''}`, kind: 'column' })
          }
        }
        return out.slice(0, 12)
      }
    }
    for (const k of data.keywords) {
      if (k.toLowerCase().startsWith(w)) {
        out.push({ insert: k, label: k, detail: tr('modeling.keywordDetail'), kind: 'keyword' })
      }
    }
    for (const t of data.tables) {
      if (t.name.toLowerCase().includes(w)) {
        out.push({ insert: t.name, label: t.name,
                   detail: `${t.layer}${t.comment ? ' · ' + t.comment : ''}`, kind: 'table' })
      }
      if (out.length >= 14) break
    }
    // 字段直搜（无前缀表名时，输入两个字符以上）
    if (w.length >= 2 && out.length < 14) {
      const seen = new Set<string>()
      for (const t of data.tables) {
        for (const c of t.columns) {
          const key = `${t.name}.${c.name}`
          if (!seen.has(key) && c.name.toLowerCase().startsWith(w)) {
            seen.add(key)
            out.push({ insert: key, label: key,
                       detail: c.comment || c.type, kind: 'column' })
          }
          if (out.length >= 14) break
        }
        if (out.length >= 14) break
      }
    }
    return out
  }, [word, data, tr])

  const currentWord = () => {
    const el = ref.current
    if (!el) return ''
    const pos = el.selectionStart
    const before = value.slice(0, pos)
    const m = before.match(/[\w$.一-鿿]+$/)
    return m ? m[0] : ''
  }

  const accept = (s: Suggestion) => {
    const el = ref.current
    if (!el) return
    const pos = el.selectionStart
    const before = value.slice(0, pos)
    const after = value.slice(pos)
    const newBefore = before.replace(/[\w$.一-鿿]+$/, s.insert)
    onChange(newBefore + after)
    setOpen(false)
    requestAnimationFrame(() => {
      el.focus()
      el.selectionStart = el.selectionEnd = newBefore.length
    })
  }

  const syncScroll = () => {
    const el = ref.current, hl = hlRef.current
    if (el && hl) { hl.scrollTop = el.scrollTop; hl.scrollLeft = el.scrollLeft }
  }

  return (
    <div style={{ position: 'relative' }}>
      <div className="input sql-editor-box" style={{ padding: 0 }}>
        {/* 高亮底层：与 textarea 同字体同 padding，透明文字叠加实现编辑区高亮 */}
        <pre ref={hlRef} aria-hidden className="sql-editor-hl">
          <SqlText code={value || ''} />{'\n'}
        </pre>
        <textarea
        ref={ref}
        className="sql-editor-input"
        rows={rows}
        value={value}
        placeholder={placeholder}
        spellCheck={false}
        onScroll={syncScroll}
        onChange={(e) => {
          onChange(e.target.value)
          const w = currentWord()
          setWord(w)
          setOpen(!!w)
          setSel(0)
        }}
        onKeyDown={(e) => {
          if (open && suggestions.length) {
            if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(s + 1, suggestions.length - 1)) }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)) }
            else if (e.key === 'Tab' || e.key === 'Enter') { e.preventDefault(); accept(suggestions[sel]) }
            else if (e.key === 'Escape') { setOpen(false) }
          } else if (e.key === 'Tab') {
            e.preventDefault()
            const el = ref.current!
            const pos = el.selectionStart
            onChange(value.slice(0, pos) + '  ' + value.slice(pos))
            requestAnimationFrame(() => { el.selectionStart = el.selectionEnd = pos + 2 })
          }
        }}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        />
      </div>
      {open && suggestions.length > 0 && (
        <div className="card" style={{
          position: 'absolute', left: 0, right: 0, top: '100%', zIndex: 30,
          maxHeight: 240, overflow: 'auto', marginTop: 2,
        }}>
          <div className="card-body" style={{ padding: 4 }}>
            {suggestions.map((s, i) => (
              <div key={`${s.insert}-${i}`}
                className="row"
                style={{
                  padding: '4px 8px', cursor: 'pointer', borderRadius: 4,
                  background: i === sel ? 'var(--accent-soft, rgba(80,140,255,.15))' : 'transparent',
                  justifyContent: 'space-between', gap: 12,
                }}
                onMouseDown={(e) => { e.preventDefault(); accept(s) }}
                onMouseEnter={() => setSel(i)}>
                <code style={{ fontSize: 12 }}>{s.label}</code>
                <span className="tiny muted truncate" style={{ maxWidth: 320 }}>{s.detail}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
