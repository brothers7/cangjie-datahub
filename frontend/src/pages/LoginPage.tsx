/** 登录页：左侧品牌叙事（Logo + Slogan + 能力标签），右侧登录卡。 */
import { useState } from 'react'
import { ApiError } from '@/api/client'
import { Logo } from '@/components/common/Logo'
import { useI18n } from '@/i18n'
import { useAuth } from '@/stores/auth'

export default function LoginPage() {
  const { t } = useI18n()
  const login = useAuth((s) => s.login)
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const FEATURES = [
    { icon: '💬', text: t('login.featureChat') },
    { icon: '📚', text: t('login.featureQa') },
    { icon: '🏗', text: t('login.featureModeling') },
    { icon: '🩺', text: t('login.featureGovernance') },
  ]

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError('')
    try {
      await login(username.trim(), password)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t('login.failed'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      {/* 品牌叙事区 */}
      <div className="login-hero">
        <div className="hero-layers" aria-hidden>
          <span /><span /><span /><span />
        </div>
        <div className="row" style={{ gap: 14, alignItems: 'center' }}>
          <Logo size={56} glow />
          <div>
            <div className="brand-word" style={{ fontSize: 34, lineHeight: 1.1 }}>仓颉</div>
            <div style={{ fontSize: 13, opacity: .75, letterSpacing: '.12em' }}>CANGJIE</div>
          </div>
        </div>
        <div className="brand-slogan" style={{ fontSize: 30, marginTop: 36, lineHeight: 1.4 }}>
          {t('login.slogan')}
        </div>
        <div style={{ fontSize: 14.5, opacity: .82, marginTop: 10 }}>
          {t('login.subtitle')}
        </div>
        <div className="col" style={{ gap: 10, marginTop: 40, maxWidth: 420 }}>
          {FEATURES.map((f) => (
            <div key={f.text} className="login-feature">
              <span>{f.icon}</span><span>{f.text}</span>
            </div>
          ))}
        </div>
      </div>

      {/* 登录表单 */}
      <div className="login-form-side">
        <form className="card login-card" style={{ padding: 30 }} onSubmit={submit}>
          <div className="row" style={{ gap: 10, marginBottom: 4 }}>
            <Logo size={34} />
            <div>
              <h1 style={{ fontSize: 16 }}>{t('login.welcome')}</h1>
              <div className="tiny muted">{t('login.cardSub')}</div>
            </div>
          </div>

          <div className="col mt16" style={{ gap: 12 }}>
            <div className="field">
              <label>{t('login.username')}</label>
              <input className="input" value={username} autoFocus
                onChange={(e) => setUsername(e.target.value)} placeholder={t('login.username')} />
            </div>
            <div className="field">
              <label>{t('login.password')}</label>
              <input className="input" type="password" value={password}
                onChange={(e) => setPassword(e.target.value)} placeholder={t('login.password')} />
            </div>
            {error && <div className="tag danger" style={{ whiteSpace: 'normal' }}>{error}</div>}
            <button className="btn primary" type="submit" disabled={busy || !username || !password}
              style={{ padding: '9px 0', marginTop: 4 }}>
              {busy ? t('login.submitting') : t('login.submit')}
            </button>
            <div className="tiny muted center">{t('login.defaultAccount')}</div>
          </div>
        </form>
      </div>
    </div>
  )
}
