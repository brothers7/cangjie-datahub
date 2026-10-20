/** i18n：t() 插值 / 缺 key 回退 / LangSwitch 切换与持久化。 */
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { I18nProvider, LangSwitch, useI18n } from './index'

function Probe({ k, vars }: { k: string; vars?: Record<string, string | number> }) {
  const { t, lang } = useI18n()
  return (
    <div>
      <span data-testid="out">{t(k, vars)}</span>
      <span data-testid="lang">{lang}</span>
    </div>
  )
}

function renderProbe(k: string, vars?: Record<string, string | number>) {
  return render(
    <I18nProvider>
      <Probe k={k} vars={vars} />
      <LangSwitch />
    </I18nProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
})

describe('i18n t()', () => {
  it('默认英文：known key 返回英文文案', () => {
    renderProbe('common.confirm')
    expect(screen.getByTestId('lang').textContent).toBe('en')
    expect(screen.getByTestId('out').textContent).toBe('Confirm')
  })

  it('支持 {var} 插值（英文）', () => {
    renderProbe('common.total', { n: 5 })
    expect(screen.getByTestId('out').textContent).toBe('5 total')
  })

  it('支持 {var} 插值（中文，经 localStorage 预设语言）', () => {
    localStorage.setItem('cangjie-lang', 'zh')
    renderProbe('common.total', { n: 5 })
    expect(screen.getByTestId('out').textContent).toBe('共 5 条')
  })

  it('数字 0 也能正常插值', () => {
    renderProbe('common.total', { n: 0 })
    expect(screen.getByTestId('out').textContent).toBe('0 total')
  })

  it('缺 key 时最终回退为 key 本身', () => {
    renderProbe('no.such.key.exists')
    expect(screen.getByTestId('out').textContent).toBe('no.such.key.exists')
  })

  it('localStorage 非法值回退默认英文', () => {
    localStorage.setItem('cangjie-lang', 'fr')
    renderProbe('common.confirm')
    expect(screen.getByTestId('lang').textContent).toBe('en')
  })
})

describe('LangSwitch', () => {
  it('点击切换到中文并持久化', () => {
    renderProbe('common.confirm')
    expect(screen.getByTestId('out').textContent).toBe('Confirm')
    fireEvent.click(screen.getByRole('button'))
    expect(screen.getByTestId('lang').textContent).toBe('zh')
    expect(screen.getByTestId('out').textContent).toBe('确认')
    expect(localStorage.getItem('cangjie-lang')).toBe('zh')
  })

  it('再点一次切回英文', () => {
    localStorage.setItem('cangjie-lang', 'zh')
    renderProbe('common.confirm')
    fireEvent.click(screen.getByRole('button'))
    expect(screen.getByTestId('lang').textContent).toBe('en')
    expect(screen.getByTestId('out').textContent).toBe('Confirm')
    expect(localStorage.getItem('cangjie-lang')).toBe('en')
  })
})
