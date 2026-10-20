/** labels.ts：语言感知的标签映射。 */
import { describe, it, expect } from 'vitest'
import {
  auditLabel, auditActions, sceneLabel, sceneLabels,
  callStatusLabel, queryStatus,
} from './labels'

describe('auditLabel', () => {
  it('英文输出', () => {
    expect(auditLabel('login', 'en')).toBe('Login')
    expect(auditLabel('datasource.create', 'en')).toBe('Create Datasource')
  })

  it('中文输出', () => {
    expect(auditLabel('login', 'zh')).toBe('登录')
    expect(auditLabel('datasource.create', 'zh')).toBe('新建数据源')
  })

  it('默认 lang 为中文', () => {
    expect(auditLabel('login')).toBe('登录')
  })

  it('未知动作原样透传，不丢信息', () => {
    expect(auditLabel('some.new.action', 'en')).toBe('some.new.action')
    expect(auditLabel('some.new.action', 'zh')).toBe('some.new.action')
  })
})

describe('sceneLabel / sceneLabels', () => {
  it('按语言返回场景标签', () => {
    expect(sceneLabel('sql_generate', 'en')).toBe('SQL Generation')
    expect(sceneLabel('sql_generate', 'zh')).toBe('SQL 生成')
  })

  it('未知场景回退 key 本身', () => {
    expect(sceneLabel('nope', 'en')).toBe('nope')
  })

  it('sceneLabels 返回整表且 en/zh key 集合一致', () => {
    const en = sceneLabels('en')
    const zh = sceneLabels('zh')
    expect(Object.keys(en).sort()).toEqual(Object.keys(zh).sort())
  })
})

describe('callStatusLabel', () => {
  it('按语言返回调用状态', () => {
    expect(callStatusLabel('timeout', 'en')).toBe('Timeout')
    expect(callStatusLabel('timeout', 'zh')).toBe('超时')
  })
})

describe('queryStatus', () => {
  it('返回文本与样式类', () => {
    expect(queryStatus('success', 'en')).toEqual({ text: 'Success', cls: 'ok' })
    expect(queryStatus('blocked', 'zh')).toEqual({ text: '已拦截', cls: 'danger' })
  })

  it('未知状态返回 undefined', () => {
    expect(queryStatus('weird', 'en')).toBeUndefined()
  })
})

describe('auditActions', () => {
  it('en/zh 两张表 key 集合一致', () => {
    expect(Object.keys(auditActions('en')).sort()).toEqual(Object.keys(auditActions('zh')).sort())
  })
})
