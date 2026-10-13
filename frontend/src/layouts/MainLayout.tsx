/** 主布局：左侧导航 + 顶栏 + 内容区。 */
import { useEffect } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { Logo } from '@/components/common/Logo'
import { Toasts } from '@/components/common/ui'
import { LangSwitch, useI18n } from '@/i18n'
import { useApp } from '@/stores/app'
import { useAuth } from '@/stores/auth'

const NAV = [
  { to: '/chat', key: 'nav.chat', icon: '💬' },
  { to: '/modeling', key: 'nav.modeling', icon: '🏗' },
  { to: '/governance', key: 'nav.governance', icon: '🩺' },
  { to: '/datasources', key: 'nav.datasources', icon: '🗄' },
  { to: '/kg', key: 'nav.kg', icon: '🕸' },
  { to: '/llm', key: 'nav.llm', icon: '🤖', adminOnly: true },
  { to: '/reports', key: 'nav.reports', icon: '📄' },
  { to: '/logs', key: 'nav.logs', icon: '📜' },
  { to: '/users', key: 'nav.users', icon: '👥', adminOnly: true },
  { to: '/integrations', key: 'nav.integrations', icon: '🧩', adminOnly: true },
]

export default function MainLayout() {
  const user = useAuth((s) => s.user)
  const logout = useAuth((s) => s.logout)
  const loadDatasources = useApp((s) => s.loadDatasources)
  const nav = useNavigate()
  const { t } = useI18n()

  // 全局加载数据源（供各页面复用，避免重复请求）
  useEffect(() => { void loadDatasources().catch(() => undefined) }, [loadDatasources])

  const onLogout = () => {
    logout()
    nav('/login', { replace: true })
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <Logo size={30} />
          <div style={{ minWidth: 0 }}>
            <div className="brand-word grad-text" style={{ fontSize: 17, lineHeight: 1.2 }}>仓颉</div>
            <div className="tiny muted">Cangjie · AI Data Warehouse</div>
          </div>
        </div>

        <nav className="nav">
          {NAV.filter((n) => !n.adminOnly || user?.role === 'admin').map((n) => (
            <NavLink key={n.to} to={n.to}
              className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}>
              <span className="nav-icon">{n.icon}</span>
              <span style={{ minWidth: 0 }}>
                <span className="nav-label">{t(n.key)}</span>
                <span className="nav-desc">{t(`${n.key}.desc`)}</span>
              </span>
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="brand-slogan tiny muted" style={{ padding: '0 10px 8px' }}>
            {t('layout.slogan')}
          </div>
          <div className="userbox">
            <span className="avatar">{(user?.display_name || user?.username || '?').slice(0, 1)}</span>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div className="truncate" style={{ fontSize: 13, fontWeight: 550 }}>
                {user?.display_name || user?.username || t('layout.notLogin')}
              </div>
              <div className="tiny muted">
                {t(`layout.role.${user?.role || 'viewer'}`)}
              </div>
            </div>
            <LangSwitch compact />
            <button className="btn ghost sm" title={t('layout.logout')} onClick={onLogout}>⏻</button>
          </div>
        </div>
      </aside>

      <main className="content">
        <Outlet />
      </main>

      <Toasts />
    </div>
  )
}
