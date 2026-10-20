/** Markdown 轻量渲染：行内语法 + XSS/协议安全。 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { Markdown } from './Markdown'

function renderMd(text: string) {
  const { container } = render(<Markdown text={text} />)
  return container
}

describe('Markdown 行内渲染', () => {
  it('粗体渲染为 <strong>', () => {
    const c = renderMd('this is **bold** text')
    expect(c.querySelector('strong')?.textContent).toBe('bold')
  })

  it('行内代码渲染为 <code>', () => {
    const c = renderMd('run `npm test` now')
    expect(c.querySelector('code')?.textContent).toBe('npm test')
  })

  it('http/https 链接渲染为带安全属性的 <a>', () => {
    const c = renderMd('see [docs](https://example.com/page)')
    const a = c.querySelector('a')
    expect(a).not.toBeNull()
    expect(a!.getAttribute('href')).toBe('https://example.com/page')
    expect(a!.getAttribute('target')).toBe('_blank')
    expect(a!.getAttribute('rel')).toBe('noreferrer')
    expect(a!.textContent).toBe('docs')
  })

  it('标题渲染', () => {
    const c = renderMd('## Section Title')
    expect(c.querySelector('h2')?.textContent).toBe('Section Title')
  })
})

describe('Markdown 安全', () => {
  it('javascript: 协议链接被拒绝，不生成 <a>', () => {
    const c = renderMd('[click](javascript:alert(1))')
    expect(c.querySelector('a')).toBeNull()
  })

  it('data: 协议链接同样不生成 <a>', () => {
    const c = renderMd('[click](data:text/html,<script>alert(1)</script>)')
    expect(c.querySelector('a')).toBeNull()
  })

  it('原始 HTML 被转义，不产生真实元素', () => {
    const c = renderMd('<img src=x onerror=alert(1)>')
    expect(c.querySelector('img')).toBeNull()
    expect(c.innerHTML).toContain('&lt;img')
  })

  it('script 标签作为文本展示', () => {
    const c = renderMd('<script>alert(1)</script>')
    expect(c.querySelector('script')).toBeNull()
  })

  it('链接 URL 中的双引号被转义为 %22，无法逃逸 href 属性', () => {
    const c = renderMd('[x](https://example.com/a"onclick="alert(1))')
    const a = c.querySelector('a')
    expect(a).not.toBeNull()
    expect(a!.getAttribute('href')).not.toContain('"')
    expect(a!.getAttribute('href')).toContain('%22')
    expect(a!.getAttribute('onclick')).toBeNull()
  })
})
