/** 管线步骤时间线（SSE step 事件实时渲染）。 */
import { useI18n } from '@/i18n'

/** 步骤中文名（作为 i18n 回退默认值，与 dict/chat.ts 的 chat.step.* 保持一致）。 */
const LABELS: Record<string, string> = {
  rewrite: '问题理解',
  route: '意图识别',
  memory: '记忆检索',
  kg_match: '指标匹配',
  validation: '口径校验',
  route_table: '数仓路由',
  sql: 'SQL 生成',
  guard: '安全校验',
  query: '数据查询',
  compute: '指标计算',
  check: '数据校验',
  anomaly: '异常检测',
  chart: '图表生成',
  web: '联网检索',
  report: '报表生成',
  answer: '分析解读',
  table_qa: '库表答疑',
  etl_design: 'ETL 方案生成',
  etl_plan: 'ETL·需求规划',
  etl_draft: 'ETL·SQL 起草',
  etl_fix: 'ETL·自动修复',
  etl_check: 'ETL·规范校验',
  custom_skill: '自定义 Skill',
  agent_step: 'AI 建模工程师',
}

export interface StepState {
  step: string
  status: 'running' | 'finished' | 'failed' | 'skipped'
  detail?: string
  ms?: number
}

export function StepTimeline({ steps, live }: { steps: StepState[]; live?: boolean }) {
  const { t } = useI18n()
  if (!steps.length) return null
  return (
    <div className="steps">
      {steps.map((s, i) => (
        <div className={`step-line ${s.status}`} key={`${s.step}-${i}`}>
          <span className="dot" />
          <span className="name">{LABELS[s.step] ? t(`chat.step.${s.step}`) : s.step}</span>
          {s.detail && <span className="truncate" style={{ maxWidth: 400 }}>· {s.detail}</span>}
          {s.ms !== undefined && s.ms > 0 && <span className="tiny muted">{s.ms}ms</span>}
          {s.status === 'running' && live && <span className="spin" style={{ width: 10, height: 10 }} />}
        </div>
      ))}
    </div>
  )
}

export { LABELS }
