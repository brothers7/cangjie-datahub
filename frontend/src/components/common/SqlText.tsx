/** SQL 语法高亮（轻量 tokenizer，无外部依赖）：
 * 关键字 / 函数 / 字符串 / 注释 / 数字 / ${ds} 分区变量分别着色。
 * 仅用于只读展示（pre），不用于编辑器。 */
import { useMemo } from 'react'

const KEYWORDS = new Set((
  'select from where group by order having limit insert overwrite table partition partitioned ' +
  'create if not exists comment join left right inner outer full cross on as case when then else end ' +
  'with union all distinct and or in is null not null like between desc asc into values set using ' +
  'distribute sort cluster lateral view explode'
).split(' '))

const FUNCTIONS = new Set((
  'sum count avg max min cast coalesce date_sub date_format date_add row_number rank dense_rank ' +
  'collect_set concat concat_ws substr substring trim upper lower round floor ceil abs ifnull nvl ' +
  'unix_timestamp from_unixtime to_date year month day hour weekofyear datediff split regexp_replace'
).split(' '))

const TOKEN_RE =
  /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^'\\]|\\.)*')|(\$\{[^}]*\})|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)/g

const CLS = { com: 'sql-com', str: 'sql-str', var: 'sql-var', num: 'sql-num',
              kw: 'sql-kw', fn: 'sql-fn' } as const

export function SqlText({ code }: { code: string }) {
  const nodes = useMemo(() => {
    const out: React.ReactNode[] = []
    let last = 0
    let i = 0
    for (const m of code.matchAll(TOKEN_RE)) {
      const idx = m.index ?? 0
      if (idx > last) out.push(code.slice(last, idx))
      const [full, com, str, v, num, word] = m
      let cls: string | null = null
      if (com) cls = CLS.com
      else if (str) cls = CLS.str
      else if (v) cls = CLS.var
      else if (num) cls = CLS.num
      else if (word) {
        const w = word.toLowerCase()
        if (KEYWORDS.has(w)) cls = CLS.kw
        else if (FUNCTIONS.has(w)) cls = CLS.fn
      }
      out.push(cls ? <span key={i} className={cls}>{full}</span> : full)
      last = idx + full.length
      i += 1
    }
    if (last < code.length) out.push(code.slice(last))
    return out
  }, [code])
  return <>{nodes}</>
}
