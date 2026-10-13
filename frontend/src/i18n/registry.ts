/** 词条注册表：汇总各模块词典。 */
import type { Lang } from './index'
import common from './dict/common'
import nav from './dict/nav'
import chat from './dict/chat'
import login from './dict/login'
import ui from './dict/ui'
import modeling from './dict/modeling'
import datasources from './dict/datasources'
import kg from './dict/kg'
import governance from './dict/governance'
import admin from './dict/admin'

const modules = [common, nav, chat, login, ui, modeling, datasources, kg, governance, admin]

export const MESSAGES: Record<Lang, Record<string, string>> = {
  en: Object.assign({}, ...modules.map((m) => m.en)),
  zh: Object.assign({}, ...modules.map((m) => m.zh)),
}
