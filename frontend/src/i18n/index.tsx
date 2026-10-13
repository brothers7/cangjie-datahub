/** 轻量 i18n：中/英切换，默认英文，localStorage 持久化。
 *
 * 词条按模块放在 dict/<ns>.ts，统一导出 { en, zh } 两张平铺表（key → 文案），
 * registry.ts 汇总。key 命名：<模块>.<语义>，通用词用 common.*。
 * t('key', { n: 1 }) 支持 {var} 插值；缺 key 时回退中文，再退 key 本身。
 */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { MESSAGES } from './registry'

export type Lang = 'en' | 'zh'
const STORAGE_KEY = 'cangjie-lang'

interface I18nCtx {
  lang: Lang
  setLang: (l: Lang) => void
  t: (key: string, vars?: Record<string, string | number>) => string
}

const Ctx = createContext<I18nCtx>({
  lang: 'en',
  setLang: () => undefined,
  t: (k) => k,
})

function detectInitial(): Lang {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved === 'en' || saved === 'zh') return saved
  return 'en'   // 默认英文（PRD：开源版本默认 English）
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(detectInitial)

  const setLang = (l: Lang) => {
    setLangState(l)
    localStorage.setItem(STORAGE_KEY, l)
  }

  useEffect(() => {
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en'
    document.title = lang === 'zh'
      ? '仓颉 Cangjie · AI 数仓工作台'
      : 'Cangjie · AI Data Warehouse Workbench'
  }, [lang])

  const value = useMemo<I18nCtx>(() => ({
    lang,
    setLang,
    t: (key, vars) => {
      let s = MESSAGES[lang][key] ?? MESSAGES.zh[key] ?? key
      if (vars) {
        for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(String(v))
      }
      return s
    },
  }), [lang])

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useI18n() {
  return useContext(Ctx)
}

/** 语言切换器（地球图标 + EN/中）。 */
export function LangSwitch({ compact = false }: { compact?: boolean }) {
  const { lang, setLang } = useI18n()
  return (
    <button
      className="btn ghost sm"
      title={lang === 'zh' ? 'Switch to English' : '切换到中文'}
      onClick={() => setLang(lang === 'zh' ? 'en' : 'zh')}
      style={{ fontWeight: 600 }}
    >
      🌐 {compact ? (lang === 'zh' ? '中' : 'EN') : (lang === 'zh' ? '中文' : 'EN')}
    </button>
  )
}
