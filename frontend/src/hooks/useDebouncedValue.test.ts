/** useDebouncedValue：值变化后延迟 delay 才对外更新。 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useDebouncedValue } from './useDebouncedValue'

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useDebouncedValue', () => {
  it('初始值立即返回', () => {
    const { result } = renderHook(() => useDebouncedValue('init', 300))
    expect(result.current).toBe('init')
  })

  it('值变化后在 delay 内保持旧值，到时后更新', () => {
    const { result, rerender } = renderHook(
      ({ v }) => useDebouncedValue(v, 300),
      { initialProps: { v: 'a' } },
    )
    rerender({ v: 'b' })
    expect(result.current).toBe('a')
    act(() => { vi.advanceTimersByTime(299) })
    expect(result.current).toBe('a')
    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current).toBe('b')
  })

  it('delay 内连续变化只提交最后一次（防抖）', () => {
    const { result, rerender } = renderHook(
      ({ v }) => useDebouncedValue(v, 300),
      { initialProps: { v: 'a' } },
    )
    rerender({ v: 'b' })
    act(() => { vi.advanceTimersByTime(100) })
    rerender({ v: 'c' })
    act(() => { vi.advanceTimersByTime(100) })
    rerender({ v: 'd' })
    act(() => { vi.advanceTimersByTime(299) })
    expect(result.current).toBe('a')
    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current).toBe('d')
  })

  it('卸载时清理定时器，不产生警告/更新', () => {
    const { result, rerender, unmount } = renderHook(
      ({ v }) => useDebouncedValue(v, 300),
      { initialProps: { v: 'a' } },
    )
    rerender({ v: 'b' })
    unmount()
    act(() => { vi.advanceTimersByTime(1000) })
    expect(result.current).toBe('a')
  })

  it('默认 delay 为 300ms', () => {
    const { result, rerender } = renderHook(
      ({ v }) => useDebouncedValue(v),
      { initialProps: { v: 1 } },
    )
    rerender({ v: 2 })
    act(() => { vi.advanceTimersByTime(300) })
    expect(result.current).toBe(2)
  })
})
