"""自定义 Skill 管理 API（PRD 3.3C）：上传/启停/版本/删除。仅管理员可管理，登录可查已启用列表。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.errors import NotFoundError, ValidationError
from app.db.models import AuditLog, CustomSkill, SkillStatus, SkillType
from app.db.session import get_db
from app.services import custom_skills as cs

router = APIRouter(prefix="/skills", tags=["skills"])


def _row(s: CustomSkill) -> dict:
    return {"id": str(s.id), "name": s.name, "description": s.description,
            "version": s.version, "skill_type": s.skill_type.value,
            "trigger_keywords": s.trigger_keywords or [],
            "status": s.status.value, "created_by": s.created_by_name,
            "invoke_count": s.invoke_count,
            "last_invoked_at": s.last_invoked_at.isoformat() if s.last_invoked_at else None,
            "created_at": s.created_at.isoformat()}


@router.get("")
async def list_skills(all: bool = False, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)):
    """普通用户看已启用；管理员带 ?all=1 看全部（含停用/历史版本）。"""
    q = select(CustomSkill).order_by(CustomSkill.name, CustomSkill.version.desc())
    if not (all and user.role.value == "admin"):
        q = q.where(CustomSkill.status == SkillStatus.ENABLED)
    return [_row(s) for s in (await db.execute(q)).scalars()]


@router.post("/upload")
async def upload(file: UploadFile, db: AsyncSession = Depends(get_db),
                 user=Depends(require_admin)):
    """上传 Skill zip 包。同名自动累加版本号；上传后默认停用，需显式启用。"""
    data = await file.read()
    pkg = cs.parse_package(data)   # 所有非法情况在此抛 ValidationError

    max_ver = (await db.execute(
        select(func.max(CustomSkill.version)).where(CustomSkill.name == pkg.name)
    )).scalar() or 0
    # 同名已有启用版本：自动停用旧版（同名同时只有一个启用版本）
    for old in (await db.execute(
            select(CustomSkill).where(CustomSkill.name == pkg.name,
                                      CustomSkill.status == SkillStatus.ENABLED))).scalars():
        old.status = SkillStatus.DISABLED

    skill = CustomSkill(name=pkg.name, description=pkg.description, version=max_ver + 1,
                        skill_type=SkillType(pkg.skill_type),
                        trigger_keywords=pkg.trigger_keywords,
                        files=pkg.files, entry=pkg.entry,
                        status=SkillStatus.DISABLED,
                        created_by=user.id, created_by_name=user.username)
    db.add(skill)
    await db.flush()
    db.add(AuditLog(user_id=user.id, username=user.username, action="skill.upload",
                    resource_type="custom_skill", resource_id=str(skill.id),
                    detail={"name": pkg.name, "version": skill.version,
                            "type": pkg.skill_type, "files": sorted(pkg.files)}))
    await db.commit()
    return _row(skill)


@router.post("/{skill_id}/status")
async def set_status(skill_id: str, body: dict, db: AsyncSession = Depends(get_db),
                     user=Depends(require_admin)):
    """启用/停用。body: {"status": "enabled"|"disabled"}。启用时自动停用同名其它版本。"""
    s = await db.get(CustomSkill, uuid.UUID(skill_id))
    if s is None:
        raise NotFoundError("Skill 不存在")
    target = (body or {}).get("status")
    if target not in ("enabled", "disabled"):
        raise ValidationError("status 必须是 enabled 或 disabled")
    if target == "enabled":
        for other in (await db.execute(
                select(CustomSkill).where(CustomSkill.name == s.name,
                                          CustomSkill.id != s.id))).scalars():
            other.status = SkillStatus.DISABLED
    s.status = SkillStatus(target)
    db.add(AuditLog(user_id=user.id, username=user.username,
                    action=f"skill.{target}", resource_type="custom_skill",
                    resource_id=str(s.id), detail={"name": s.name, "version": s.version}))
    await db.commit()
    return _row(s)


@router.post("/{skill_id}/rollback")
async def rollback(skill_id: str, body: dict, db: AsyncSession = Depends(get_db),
                   user=Depends(require_admin)):
    """版本回滚：把同名的目标历史版本置为启用（同组其它版本停用），历史全量保留。

    body 支持两种定位方式（二选一）：
    - {"target_version": 1}     按版本号
    - {"target_skill_id": "…"}  按历史版本行 id（也兼容 "skill_id" 键）
    采用「置启用」而非「复制新版本」：语义更安全，不产生内容歧义的新行。
    """
    current = await db.get(CustomSkill, uuid.UUID(skill_id))
    if current is None:
        raise NotFoundError("Skill 不存在")
    body = body or {}
    target: CustomSkill | None = None
    if body.get("target_version") is not None:
        target = (await db.execute(select(CustomSkill).where(
            CustomSkill.name == current.name,
            CustomSkill.version == int(body["target_version"])))).scalar_one_or_none()
    elif body.get("target_skill_id") or body.get("skill_id"):
        tid = body.get("target_skill_id") or body.get("skill_id")
        target = await db.get(CustomSkill, uuid.UUID(str(tid)))
        if target is not None and target.name != current.name:
            raise ValidationError("目标版本不属于该 Skill（同名版本组内才能回滚）")
    else:
        raise ValidationError("请提供 target_version 或 target_skill_id")
    if target is None:
        raise NotFoundError("目标历史版本不存在")

    # 同组其它版本全部停用，目标版本启用
    for other in (await db.execute(
            select(CustomSkill).where(CustomSkill.name == current.name,
                                      CustomSkill.id != target.id))).scalars():
        other.status = SkillStatus.DISABLED
    target.status = SkillStatus.ENABLED
    db.add(AuditLog(user_id=user.id, username=user.username, action="skill.rollback",
                    resource_type="custom_skill", resource_id=str(target.id),
                    detail={"name": target.name, "from_version": current.version,
                            "target_version": target.version}))
    await db.commit()
    return _row(target)


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str, db: AsyncSession = Depends(get_db),
                       user=Depends(require_admin)):
    s = await db.get(CustomSkill, uuid.UUID(skill_id))
    if s is None:
        raise NotFoundError("Skill 不存在")
    await db.delete(s)
    db.add(AuditLog(user_id=user.id, username=user.username, action="skill.delete",
                    resource_type="custom_skill", resource_id=str(s.id),
                    detail={"name": s.name, "version": s.version}))
    await db.commit()
    return {"ok": True}
