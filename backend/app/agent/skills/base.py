"""Skill 注册表：独立可编排的数据执行能力（PRD 2.1）。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

SkillFn = Callable[..., Awaitable]

SKILLS: dict[str, SkillFn] = {}


def skill(name: str, description: str = ""):
    def deco(fn: SkillFn) -> SkillFn:
        fn.skill_name = name  # type: ignore[attr-defined]
        fn.skill_description = description  # type: ignore[attr-defined]
        SKILLS[name] = fn
        return fn
    return deco


def get_skill(name: str) -> SkillFn:
    if name not in SKILLS:
        raise KeyError(f"Skill 未注册: {name}")
    return SKILLS[name]
