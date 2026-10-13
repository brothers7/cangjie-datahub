/** 全局提示（轻量 toast）与共享数据源列表缓存。 */
import { create } from 'zustand'
import { api } from '@/api/client'
import type { Datasource } from '@/types'

export interface Toast {
  id: number
  kind: 'info' | 'ok' | 'warn' | 'error'
  text: string
}

interface AppState {
  toasts: Toast[]
  push: (kind: Toast['kind'], text: string) => void
  drop: (id: number) => void

  datasources: Datasource[]
  datasourcesLoaded: boolean
  loadDatasources: (force?: boolean) => Promise<Datasource[]>
}

let seq = 1

export const useApp = create<AppState>((set, get) => ({
  toasts: [],
  push: (kind, text) => {
    const id = seq++
    set((s) => ({ toasts: [...s.toasts, { id, kind, text }] }))
    setTimeout(() => get().drop(id), kind === 'error' ? 6000 : 3200)
  },
  drop: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),

  datasources: [],
  datasourcesLoaded: false,
  loadDatasources: async (force = false) => {
    if (get().datasourcesLoaded && !force) return get().datasources
    const rows = await api.listDatasources()
    set({ datasources: rows, datasourcesLoaded: true })
    return rows
  },
}))

export const toast = {
  info: (t: string) => useApp.getState().push('info', t),
  ok: (t: string) => useApp.getState().push('ok', t),
  warn: (t: string) => useApp.getState().push('warn', t),
  error: (t: string) => useApp.getState().push('error', t),
}
