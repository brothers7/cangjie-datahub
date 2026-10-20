/** ui.tsx 纯函数：fmtNum 数字格式化 / relTime 相对时间。 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { fmtNum, relTime } from './ui'
import { MESSAGES } from '@/i18n/registry'

/** 用真实英文词典构造 t，验证 relTime 的 i18n 分支 */
const tEn = (key: string, vars?: Record<string, string | number>): string => {
  let s = MESSAGES.en[key] ?? key
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(String(v))
  return s
}

describe('fmtNum', () => {
  it('null / undefined / 空串返回 -', () => {
    expect(fmtNum(null)).toBe('-')
    expect(fmtNum(undefined)).toBe('-')
    expect(fmtNum('')).toBe('-')
  })

  it('非数字原样返回字符串', () => {
    expect(fmtNum('abc')).toBe('abc')
  })

  it('万级数字折合为万', () => {
    expect(fmtNum(12345)).toBe('1.23 万')
  })

  it('亿级数字折合为亿', () => {
    expect(fmtNum(200_000_000)).toBe('2.00 亿')
  })

  it('普通整数千分位', () => {
    expect(fmtNum(1234)).toBe('1,234')
  })

  it('小数按 digits 截断', () => {
    expect(fmtNum(3.14159)).toBe('3.14')
    expect(fmtNum(3.14159, 4)).toBe('3.1416')
  })
})

describe('relTime', () => {
  const NOW = new Date('2026-10-20T12:00:00')

  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  const ago = (ms: number) => new Date(NOW.getTime() - ms).toISOString()

  it('空值与非法日期返回 -', () => {
    expect(relTime(null)).toBe('-')
    expect(relTime(undefined)).toBe('-')
    expect(relTime('not-a-date')).toBe('-')
  })

  it('1 分钟内：刚刚 / Just now', () => {
    expect(relTime(ago(30_000))).toBe('刚刚')
    expect(relTime(ago(30_000), tEn)).toBe('Just now')
  })

  it('1 小时内：n 分钟前 / n min ago', () => {
    expect(relTime(ago(5 * 60_000))).toBe('5 分钟前')
    expect(relTime(ago(5 * 60_000), tEn)).toBe('5 min ago')
  })

  it('24 小时内：n 小时前', () => {
    expect(relTime(ago(3 * 3_600_000))).toBe('3 小时前')
  })

  it('7 天内：n 天前', () => {
    expect(relTime(ago(2 * 86_400_000))).toBe('2 天前')
  })

  it('超过 7 天回退为日期字符串 YYYY-MM-DD', () => {
    expect(relTime(ago(10 * 86_400_000))).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })
})
