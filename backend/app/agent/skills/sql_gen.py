"""SQL 生成 Skill：自然语言 → 合规 SELECT（口径来自知识图谱，防幻觉）。"""
from __future__ import annotations

import re

from app.agent.skills.base import skill
from app.agent.skills.sql_guard import detect_injection
from app.core.errors import LLMError
from app.db.models.llm import LlmScene
from app.llm.gateway import gateway
from app.llm.types import ChatMessage

SQL_SYSTEM_PROMPT = """你是企业数据仓库的 SQL 专家。严格规则：
1. 只输出一条 SELECT 语句（可用 WITH/UNION），禁止 INSERT/UPDATE/DELETE/DDL，禁止多语句。
2. 只能使用「可用表结构与指标口径」中列出的表和字段，禁止编造任何表名、字段名。
3. 时间范围由 <时间范围> 给出，必须用它过滤时间字段；日期值直接写入 SQL。
4. 聚合方式必须遵循指标口径中的「聚合」要求；比率类指标不要对百分比再求和。
5. 需要维度拆解时 GROUP BY 对应维度列；排行类 ORDER BY 主指标 DESC。
6. 维度物理列不在主表上时，必须按「表关联」给出的 JOIN 条件连接对应表后再 GROUP BY，
   禁止猜测关联键，也禁止用同名维度列硬凑。
7. 不要输出任何解释、注释或 markdown 代码块，只输出 SQL 本身。
8. 使用标准 SQL 语法（系统会自动转换到目标方言）。"""

FIX_SYSTEM_PROMPT = """你是 SQL 修复专家。根据执行错误信息修复 SQL。
只输出修复后的一条 SELECT 语句，不要任何解释。保持原有查询意图，只修正错误的部分。"""


def _clean(sql: str) -> str:
    sql = (sql or "").strip()
    sql = re.sub(r"^```(?:sql)?\s*|\s*```$", "", sql, flags=re.I | re.S)
    return sql.strip()


def sanitize_question(q: str) -> str:
    """用户输入清洗：截断 + 去注释符号（防提示注入/SQL 注入进入 prompt）。"""
    q = (q or "").strip()[:1000]
    q = q.replace("--", "——").replace("/*", "（").replace("*/", "）")
    if detect_injection(q):
        q = re.sub(r"[;'", "", q)
    return q


@skill("sql_generate", "自然语言转合规 SELECT")
async def sql_generate(*, question: str, kg_context: str, dialect: str,
                       time_expr: str, extra_rules: str = "",
                       few_shot: str = "", override_config_id: str | None = None) -> str:
    user = f"""<目标方言>{dialect}</目标方言>
{kg_context}

<时间范围>{time_expr}</时间范围>
{f'<附加规则>{extra_rules}</附加规则>' if extra_rules else ''}
{f'<参考示例>{few_shot}</参考示例>' if few_shot else ''}
<用户问题>{sanitize_question(question)}</用户问题>

请输出 SQL："""
    resp = await gateway.chat(LlmScene.SQL_GENERATE,
                              [ChatMessage("system", SQL_SYSTEM_PROMPT),
                               ChatMessage("user", user)],
                              temperature=0.0, override_config_id=override_config_id)
    return _clean(resp.text)


@skill("sql_fix", "带执行错误回喂的 SQL 修复")
async def sql_fix(*, question: str, kg_context: str, dialect: str,
                  bad_sql: str, errors: str, override_config_id: str | None = None) -> str:
    user = f"""<目标方言>{dialect}</目标方言>
{kg_context}

<用户问题>{sanitize_question(question)}</用户问题>

<有问题的SQL>
{bad_sql}
</有问题的SQL>

<错误信息>
{errors[:1500]}
</错误信息>

请输出修复后的 SQL："""
    resp = await gateway.chat(LlmScene.SQL_FIX,
                              [ChatMessage("system", FIX_SYSTEM_PROMPT),
                               ChatMessage("user", user)],
                              temperature=0.0, override_config_id=override_config_id)
    return _clean(resp.text)
