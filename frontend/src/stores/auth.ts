/** 登录态与当前用户（zustand）。 */
import { create } from 'zustand'
import { api, tokenStore } from '@/api/client'
import type { Me } from '@/types'

interface AuthState {
  user: Me | null
  ready: boolean          // 已尝试恢复登录态
  username: string
  displayName: string
  setUser: (u: Me | null) => void
  login: (username: string, password: string) => Promise<void>
  logout: () => void
  bootstrap: () => Promise<void>
}

export const useAuth = create<AuthState>((set) => ({
  user: null,
  ready: false,
  username: '',
  displayName: '',

  setUser: (u) => set({ user: u, username: u?.username || '', displayName: u?.display_name || '' }),

  login: async (username, password) => {
    const r = await api.login(username, password)
    tokenStore.set(r.token)
    set({
      user: { id: '', username: r.username, display_name: r.display_name, role: r.role as Me['role'] },
      username: r.username,
      displayName: r.display_name,
      ready: true,
    })
    // 拉取完整用户信息（含 id）
    try {
      const me = await api.me()
      set({ user: me, username: me.username, displayName: me.display_name })
    } catch { /* 保持登录态 */ }
  },

  logout: () => {
    tokenStore.clear()
    set({ user: null, username: '', displayName: '' })
  },

  bootstrap: async () => {
    if (!tokenStore.get()) {
      set({ ready: true, user: null })
      return
    }
    try {
      const me = await api.me()
      set({ user: me, username: me.username, displayName: me.display_name, ready: true })
    } catch {
      set({ user: null, ready: true })
    }
  },
}))
