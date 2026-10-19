/** 路由与登录守卫。 */
import { lazy, Suspense, useEffect } from 'react'
import { Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { setUnauthorizedHandler } from '@/api/client'
import { Spinner } from '@/components/common/ui'
import MainLayout from '@/layouts/MainLayout'
import { useAuth } from '@/stores/auth'

// 路由级代码分割：页面（含 ECharts 依赖）按需加载，首屏只拉登录/布局
const ChatPage = lazy(() => import('@/pages/ChatPage'))
const DataSourcesPage = lazy(() => import('@/pages/DataSourcesPage'))
const IntegrationsPage = lazy(() => import('@/pages/IntegrationsPage'))
const KnowledgeGraphPage = lazy(() => import('@/pages/KnowledgeGraphPage'))
const LlmConfigPage = lazy(() => import('@/pages/LlmConfigPage'))
const LoginPage = lazy(() => import('@/pages/LoginPage'))
const LogsPage = lazy(() => import('@/pages/LogsPage'))
const ModelingPage = lazy(() => import('@/pages/ModelingPage'))
const GovernancePage = lazy(() => import('@/pages/GovernancePage'))
const ReportsPage = lazy(() => import('@/pages/ReportsPage'))
const UsersPage = lazy(() => import('@/pages/UsersPage'))

/** 路由懒加载兜底：沿用现有加载样式 */
const routeFallback = (
  <div className="center" style={{ height: '100vh' }}><Spinner text="加载中…" /></div>
)

export default function App() {
  const ready = useAuth((s) => s.ready)
  const user = useAuth((s) => s.user)
  const bootstrap = useAuth((s) => s.bootstrap)
  const logout = useAuth((s) => s.logout)
  const nav = useNavigate()

  useEffect(() => { void bootstrap() }, [bootstrap])

  // 401 → 清理登录态并回到登录页
  useEffect(() => {
    setUnauthorizedHandler(() => {
      logout()
      nav('/login', { replace: true })
    })
  }, [logout, nav])

  if (!ready) {
    return <div className="center" style={{ height: '100vh' }}><Spinner text="加载中…" /></div>
  }

  if (!user) {
    return (
      <Suspense fallback={routeFallback}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="*" element={<Navigate to="/login" replace />} />
        </Routes>
      </Suspense>
    )
  }

  const isAdmin = user.role === 'admin'
  // 管理页接口本身有 require_admin 校验；前端再兜一层路由守卫，避免直链打开看到一堆 403
  const adminOnly = (el: React.ReactNode) => (isAdmin ? el : <Navigate to="/chat" replace />)

  return (
    <Suspense fallback={routeFallback}>
      <Routes>
        <Route path="/login" element={<Navigate to="/chat" replace />} />
        <Route element={<MainLayout />}>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:cid" element={<ChatPage />} />
          <Route path="/datasources" element={<DataSourcesPage />} />
          <Route path="/modeling" element={<ModelingPage />} />
          <Route path="/governance" element={<GovernancePage />} />
          <Route path="/kg" element={<KnowledgeGraphPage />} />
          <Route path="/llm" element={adminOnly(<LlmConfigPage />)} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/logs" element={<LogsPage />} />
          <Route path="/users" element={adminOnly(<UsersPage />)} />
          <Route path="/integrations" element={adminOnly(<IntegrationsPage />)} />
        </Route>
        <Route path="*" element={<Navigate to="/chat" replace />} />
      </Routes>
    </Suspense>
  )
}
