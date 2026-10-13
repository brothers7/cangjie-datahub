/** 截图脚本：对本地运行的仓颉 DataHub 拍双语言界面截图。
 * 用法：BASE=http://localhost:5173 node shoot.mjs [en|zh|all]
 * 需要后端已启动且存在演示数据，账号 admin / admin12345（可用 ADMIN_USER/ADMIN_PASS 覆盖）。
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import path from 'node:path'

const BASE = process.env.BASE || 'http://localhost:5173'
const USER = process.env.ADMIN_USER || 'admin'
const PASS = process.env.ADMIN_PASS || 'admin12345'
const OUT = path.resolve(process.cwd(), '../../docs/screenshots')
const mode = process.argv[2] || 'all'
const langs = mode === 'all' ? ['en', 'zh'] : [mode]

// [名称, 路径, 等待ms, 可选：进入后要点按的页签文案（按语言）]
const PAGES = [
  ['chat', '/chat', 2500],
  ['modeling-ai', '/modeling', 2500],
  ['modeling-tasks', '/modeling', 2000, { en: 'Modeling Tasks', zh: '建模任务' }],
  ['modeling-standards', '/modeling', 2000, { en: 'DW Standards', zh: '数仓规范' }],
  ['scheduler', '/modeling', 2000, { en: 'Scheduler Center', zh: '调度中心' }],
  ['modeling-meta', '/modeling', 2000, { en: 'Task Metadata', zh: '任务元数据' }],
  ['governance', '/governance', 2500],
  ['datasources', '/datasources', 2500],
  ['kg', '/kg', 3000],
  ['kg-graph', '/kg', 3500, { en: 'Graph', zh: '图谱可视化' }],
  ['llm', '/llm', 2000],
  ['logs', '/logs', 2000],
  ['users', '/users', 2000],
  ['integrations', '/integrations', 2500],
  ['reports', '/reports', 2000],
]

// 登录态：API 层拿 token 注入 localStorage（与 api/client.ts 的 tokenStore 键一致）
async function makeAuthedContext(browser, lang) {
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    locale: lang === 'zh' ? 'zh-CN' : 'en-US',
  })
  await ctx.addInitScript((l) => localStorage.setItem('cangjie-lang', l), lang)
  const page = await ctx.newPage()
  const resp = await page.request.post(`${BASE}/api/v1/auth/login`, {
    data: { username: USER, password: PASS },
  })
  const body = await resp.json()
  if (!body.token) throw new Error(`login failed: ${JSON.stringify(body)}`)
  await ctx.addInitScript((token) => localStorage.setItem('da.token', token), body.token)
  await page.close()
  return ctx
}

for (const lang of langs) {
  const browser = await chromium.launch()
  try {
    const ctx = await makeAuthedContext(browser, lang)
    const page = await ctx.newPage()
    await page.goto(`${BASE}/chat`, { waitUntil: 'networkidle' })
    if (page.url().includes('/login')) {
      console.log(`[${lang}] token injection failed — check token storage key`)
    }
    const outDir = path.join(OUT, lang)
    mkdirSync(outDir, { recursive: true })
    for (const [name, url, wait, clickTab] of PAGES) {
      try {
        await page.goto(`${BASE}${url}`, { waitUntil: 'networkidle' })
        if (clickTab) {
          await page.getByRole('button', { name: clickTab[lang], exact: true }).first().click()
        }
        await page.waitForTimeout(wait)
        await page.screenshot({ path: path.join(outDir, `${name}.png`) })
        console.log(`[${lang}] ${name} ✓`)
      } catch (e) {
        console.log(`[${lang}] ${name} ✗ ${e.message.split('\n')[0]}`)
      }
    }
    await page.close()
    await ctx.close()
    // 登录页（无登录态上下文）
    const ctx2 = await browser.newContext({
      viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2,
      locale: lang === 'zh' ? 'zh-CN' : 'en-US',
    })
    await ctx2.addInitScript((l) => localStorage.setItem('cangjie-lang', l), lang)
    const p2 = await ctx2.newPage()
    await p2.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
    await p2.waitForTimeout(1500)
    await p2.screenshot({ path: path.join(outDir, 'login.png') })
    console.log(`[${lang}] login ✓`)
  } catch (e) {
    console.log(`[${lang}] FAILED: ${e.message.split('\n')[0]}`)
  } finally {
    await browser.close()
  }
}
console.log('done →', OUT)
