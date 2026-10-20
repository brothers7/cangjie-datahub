/** zustand 全局 store：toast 的添加 / 手动移除 / 定时过期。 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { useApp, toast } from './app'

function resetStore() {
  useApp.setState({ toasts: [], datasources: [], datasourcesLoaded: false })
}

beforeEach(() => {
  vi.useFakeTimers()
  resetStore()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useApp toasts', () => {
  it('push 追加一条 toast，带自增 id 与 kind/text', () => {
    useApp.getState().push('ok', 'saved')
    const { toasts } = useApp.getState()
    expect(toasts).toHaveLength(1)
    expect(toasts[0].kind).toBe('ok')
    expect(toasts[0].text).toBe('saved')
    expect(typeof toasts[0].id).toBe('number')
  })

  it('连续 push id 不重复', () => {
    useApp.getState().push('info', 'a')
    useApp.getState().push('warn', 'b')
    const [a, b] = useApp.getState().toasts
    expect(a.id).not.toBe(b.id)
  })

  it('drop 按 id 移除指定 toast', () => {
    useApp.getState().push('info', 'a')
    useApp.getState().push('warn', 'b')
    const target = useApp.getState().toasts[0].id
    useApp.getState().drop(target)
    const { toasts } = useApp.getState()
    expect(toasts).toHaveLength(1)
    expect(toasts[0].text).toBe('b')
  })

  it('普通 toast 3200ms 后自动过期', () => {
    useApp.getState().push('info', 'bye')
    expect(useApp.getState().toasts).toHaveLength(1)
    vi.advanceTimersByTime(3199)
    expect(useApp.getState().toasts).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(useApp.getState().toasts).toHaveLength(0)
  })

  it('error toast 6000ms 才过期', () => {
    useApp.getState().push('error', 'boom')
    vi.advanceTimersByTime(3200)
    expect(useApp.getState().toasts).toHaveLength(1)
    vi.advanceTimersByTime(2800)
    expect(useApp.getState().toasts).toHaveLength(0)
  })

  it('手动 drop 后定时器再触发也不报错', () => {
    useApp.getState().push('ok', 'x')
    const id = useApp.getState().toasts[0].id
    useApp.getState().drop(id)
    expect(useApp.getState().toasts).toHaveLength(0)
    vi.advanceTimersByTime(10_000)
    expect(useApp.getState().toasts).toHaveLength(0)
  })
})

describe('toast 便捷方法', () => {
  it('toast.ok / toast.error 写入对应 kind', () => {
    toast.ok('good')
    toast.error('bad')
    const kinds = useApp.getState().toasts.map((t) => t.kind)
    expect(kinds).toEqual(['ok', 'error'])
  })
})
