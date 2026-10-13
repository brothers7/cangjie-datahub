"""报表生成 Skill：结构化分析报表（PRD 3.7）。

章节固定：指标汇总 / 趋势解读 / 波动原因 / 结论与建议 / 风险标注 /（可选）外部参考。
LLM 输出按固定 markdown 标题分节，解析失败时降级为模板拼接。
"""
from __future__ import annotations

import re

from app.agent.skills.base import skill
from app.db.models.llm import LlmScene
from app.llm.gateway import gateway
from app.llm.types import ChatMessage

SECTION_KEYS = ["summary", "trend", "volatility", "conclusion", "risk", "external"]
SECTION_TITLES = {
    "summary": "指标汇总", "trend": "数据趋势解读", "volatility": "波动原因分析",
    "conclusion": "业务结论与建议", "risk": "异常风险标注", "external": "外部参考（联网）",
}

REPORT_SYSTEM_PROMPT = """你是资深数据分析师，为企业内部生成结构化分析报表。要求：
1. 结论先行、用数据说话；引用给出的真实数字，禁止编造数据。
2. 使用简体中文；数值保留合理精度；波动描述带百分比。
3. 严格按以下 markdown 二级标题分节输出（顺序固定、标题原文不变，无内容的节写"本期无显著异常"）：

## 指标汇总
## 数据趋势解读
## 波动原因分析
## 业务结论与建议
## 异常风险标注

风格：专业、克制、可执行。"""


def _split_sections(md: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    pattern = re.compile(r"^##\s*(.+)$", re.M)
    matches = list(pattern.finditer(md))
    title_to_key = {v: k for k, v in SECTION_TITLES.items()}
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        body = md[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(md)].strip()
        key = title_to_key.get(title)
        if key:
            sections[key] = body
    return sections


def _fallback_sections(question: str, snapshot: str, anomalies_text: str,
                       compare_text: str) -> dict[str, str]:
    return {
        "summary": f"分析问题：{question}\n\n{snapshot}",
        "trend": "（LLM 服务暂不可用，趋势解读生成失败，可稍后重试）",
        "volatility": anomalies_text or "本期无显著异常",
        "conclusion": "（LLM 服务暂不可用，结论建议生成失败）",
        "risk": anomalies_text or "无",
    }


@skill("report_generate", "生成完整结构化分析报表")
async def report_generate(*, question: str, metrics_info: str, data_snapshot: str,
                          anomalies_text: str = "", compare_text: str = "",
                          external_text: str = "", override_config_id: str | None = None) -> dict:
    user_parts = [
        f"<分析问题>{question}</分析问题>",
        f"<指标口径>{metrics_info}</指标口径>",
        f"<数据快照>{data_snapshot}</数据快照>",
    ]
    if compare_text:
        user_parts.append(f"<对比信息>{compare_text}</对比信息>")
    if anomalies_text:
        user_parts.append(f"<异常检测结果>{anomalies_text}</异常检测结果>")
    if external_text:
        user_parts.append(f"<外部参考（仅作背景，不参与内部指标计算）>{external_text}</外部参考>")

    sections: dict[str, str]
    try:
        resp = await gateway.chat(LlmScene.REPORT_GENERATE,
                                  [ChatMessage("system", REPORT_SYSTEM_PROMPT),
                                   ChatMessage("user", "\n\n".join(user_parts))],
                                  temperature=0.3, override_config_id=override_config_id)
        sections = _split_sections(resp.text)
        if not sections:
            raise ValueError("报表分节解析为空")
    except Exception:
        sections = _fallback_sections(question, data_snapshot, anomalies_text, compare_text)

    if external_text:
        sections["external"] = f"> 以下为互联网公开信息，仅供对标参考，非内部口径数据。\n\n{external_text}"

    ordered = [{"key": k, "title": SECTION_TITLES[k], "content_md": sections.get(k, "无")}
               for k in SECTION_KEYS if sections.get(k)]

    md_parts = [f"# {question}", ""]
    for s in ordered:
        md_parts += [f"## {s['title']}", s["content_md"], ""]
    return {"title": question[:80], "content_md": "\n".join(md_parts), "sections": ordered}
