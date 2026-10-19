/** 轻量 markdown 渲染：标题 / 列表 / 表格 / 引用 / 行内强调。仅用于可信来源（后端 LLM 报表）。 */
import { useMemo } from 'react'

const esc = (s: string) => s
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

/** 行内：粗体、行内代码、http/https 链接（不渲染任意 HTML/脚本） */
function inline(s: string): string {
  return esc(s)
    // 链接必须在 esc 之后处理：文本已转义，URL 白名单限 http/https，
    // 且 URL 内引号统一转 %22，避免从 href 属性逃逸
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_m, text: string, url: string) =>
        `<a href="${url.split('"').join('%22')}" target="_blank" rel="noreferrer">${text}</a>`)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
}

function renderBlocks(md: string): string {
  const lines = md.replace(/\r\n/g, '\n').split('\n')
  const out: string[] = []
  let i = 0

  const flushList = (tag: 'ul' | 'ol', items: string[]) => {
    if (items.length) out.push(`<${tag}>${items.map((t) => `<li>${inline(t)}</li>`).join('')}</${tag}>`)
  }

  while (i < lines.length) {
    const line = lines[i]

    if (!line.trim()) { i++; continue }

    // 表格
    if (line.includes('|') && /^\s*\|?.+\|.+\|?\s*$/.test(line)
        && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
      const cells = (l: string) => l.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim())
      const head = cells(line)
      i += 2
      const body: string[][] = []
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        body.push(cells(lines[i]))
        i++
      }
      out.push(`<table><thead><tr>${head.map((h) => `<th>${inline(h)}</th>`).join('')}</tr></thead>`
        + `<tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table>`)
      continue
    }

    const h = /^(#{1,4})\s+(.*)$/.exec(line)
    if (h) {
      const lvl = h[1].length
      out.push(`<h${lvl}>${inline(h[2])}</h${lvl}>`)
      i++
      continue
    }

    if (/^\s*>\s?/.test(line)) {
      const buf: string[] = []
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ''))
        i++
      }
      out.push(`<blockquote>${buf.map(inline).join('<br/>')}</blockquote>`)
      continue
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*+]\s+/, ''))
        i++
      }
      flushList('ul', items)
      continue
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: string[] = []
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+[.)]\s+/, ''))
        i++
      }
      flushList('ol', items)
      continue
    }

    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { out.push('<hr/>'); i++; continue }

    const para: string[] = []
    while (i < lines.length && lines[i].trim()
           && !/^(#{1,4})\s|^\s*[-*+]\s|^\s*\d+[.)]\s|^\s*>/.test(lines[i])) {
      para.push(lines[i])
      i++
    }
    out.push(`<p>${para.map(inline).join('<br/>')}</p>`)
  }
  return out.join('\n')
}

export function Markdown({ text, className }: { text: string; className?: string }) {
  const html = useMemo(() => renderBlocks(text || ''), [text])
  // 内容已做 HTML 转义；仅保留本组件生成的受控标签
  return <div className={className} dangerouslySetInnerHTML={{ __html: html }} />
}
