"""数仓规范管理 API（PRD 3.3E）：规范文档 CRUD（仅管理员可写，登录用户可读）。"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.errors import NotFoundError, ValidationError
from app.db.models import AuditLog, DwStandard
from app.db.session import get_db
from app.services.dw_standards import RULE_DEFS, suggest_rules

router = APIRouter(prefix="/standards", tags=["dw-standards"])


def _row(s: DwStandard) -> dict:
    return {"id": str(s.id), "name": s.name, "content": s.content,
            "rules": s.rules or [], "enabled": s.enabled,
            "updated_by": s.updated_by,
            "created_at": s.created_at.isoformat(), "updated_at": s.updated_at.isoformat()}


class StandardIn(BaseModel):
    name: str
    content: str = ""
    rules: list[str] = []
    enabled: bool = True


def _validate(body: StandardIn) -> None:
    if not body.name.strip():
        raise ValidationError("规范名称不能为空")
    unknown = [r for r in body.rules if r not in RULE_DEFS]
    if unknown:
        raise ValidationError(f"未知规则开关：{'、'.join(unknown)}（可选：{'、'.join(RULE_DEFS)}）")


@router.get("/rule-defs")
async def rule_defs(user=Depends(get_current_user)):
    """可选的结构化规则开关清单（供前端渲染勾选项）。"""
    return {"rules": [{"key": k, "label": v} for k, v in RULE_DEFS.items()]}


@router.get("")
async def list_standards(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    rows = (await db.execute(select(DwStandard)
                             .order_by(DwStandard.updated_at.desc()))).scalars()
    return {"standards": [_row(s) for s in rows]}


@router.post("/import")
async def import_standard(file: UploadFile, user=Depends(require_admin)):
    """导入规范文件（.md/.txt，≤512KB）：解析正文 + 启发式建议结构化开关。

    不落库——返回预填内容，由前端编辑器确认后再走 POST /standards 保存生效。
    """
    raw = await file.read()
    if not raw:
        raise ValidationError("文件为空")
    if len(raw) > 512 * 1024:
        raise ValidationError("规范文件不能超过 512KB")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            content = raw.decode("gbk")
        except UnicodeDecodeError as e:
            raise ValidationError("文件编码无法识别（请使用 UTF-8 或 GBK 文本）") from e
    fname = (file.filename or "数仓规范").rsplit("/", 1)[-1]
    fallback = re.sub(r"\.(md|markdown|txt)$", "", fname, flags=re.I).strip() or "导入的规范"
    # 优先取正文首个 Markdown 一级标题作为规范名
    h1 = re.search(r"^#\s+(.+?)\s*$", content, flags=re.M)
    name = (h1.group(1).strip() if h1 else "") or fallback
    return {"name": name, "content": content, "rules": suggest_rules(content)}


@router.post("")
async def create_standard(body: StandardIn, db: AsyncSession = Depends(get_db),
                          user=Depends(require_admin)):
    _validate(body)
    s = DwStandard(name=body.name.strip(), content=body.content, rules=body.rules,
                   enabled=body.enabled, updated_by=user.username)
    db.add(s)
    await db.flush()
    db.add(AuditLog(user_id=user.id, username=user.username, action="standard.create",
                    resource_type="dw_standard", resource_id=str(s.id),
                    detail={"name": s.name, "rules": s.rules}))
    await db.commit()
    return _row(s)


@router.patch("/{sid}")
async def update_standard(sid: str, body: StandardIn, db: AsyncSession = Depends(get_db),
                          user=Depends(require_admin)):
    _validate(body)
    s = await db.get(DwStandard, uuid.UUID(sid))
    if s is None:
        raise NotFoundError("规范不存在")
    s.name, s.content, s.rules, s.enabled = body.name.strip(), body.content, body.rules, body.enabled
    s.updated_by = user.username
    db.add(AuditLog(user_id=user.id, username=user.username, action="standard.update",
                    resource_type="dw_standard", resource_id=sid,
                    detail={"name": s.name, "rules": s.rules, "enabled": s.enabled}))
    await db.commit()
    return _row(s)


@router.delete("/{sid}")
async def delete_standard(sid: str, db: AsyncSession = Depends(get_db),
                          user=Depends(require_admin)):
    s = await db.get(DwStandard, uuid.UUID(sid))
    if s is None:
        raise NotFoundError("规范不存在")
    db.add(AuditLog(user_id=user.id, username=user.username, action="standard.delete",
                    resource_type="dw_standard", resource_id=sid, detail={"name": s.name}))
    await db.delete(s)
    await db.commit()
    return {"ok": True}
