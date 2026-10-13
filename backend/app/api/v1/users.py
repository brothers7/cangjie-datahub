"""用户管理（admin）与数据源权限分配。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.errors import NotFoundError, ValidationError
from app.core.security import hash_password
from app.db.models import DatasourcePermission, Role, User, UserStatus
from app.db.session import get_db
from app.schemas.entities import UserCreateIn, UserOut, UserUpdateIn

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserOut])
async def list_users(db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    rows = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    return rows


@router.post("", response_model=UserOut)
async def create_user(body: UserCreateIn, db: AsyncSession = Depends(get_db),
                      _=Depends(require_admin)):
    if body.role not in ("admin", "analyst", "viewer"):
        raise ValidationError("角色非法")
    exists = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if exists:
        raise ValidationError("用户名已存在")
    user = User(username=body.username, password_hash=hash_password(body.password),
                display_name=body.display_name or body.username, role=Role(body.role))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: str, body: UserUpdateIn,
                      db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    user = await db.get(User, uuid.UUID(user_id))
    if user is None:
        raise NotFoundError("用户不存在")
    if body.display_name is not None:
        user.display_name = body.display_name
    # 最后一名启用中的 admin 不可被降级/禁用（防系统失去管理员）
    demoting = ((body.role is not None and body.role != "admin" and user.role == Role.ADMIN)
                or (body.status is not None and body.status != "active"
                    and user.role == Role.ADMIN))
    if demoting:
        from sqlalchemy import func, select
        admins = (await db.execute(select(func.count()).select_from(User).where(
            User.role == Role.ADMIN, User.status == UserStatus.ACTIVE))).scalar() or 0
        if admins <= 1:
            raise ValidationError("系统至少保留一名启用中的管理员")
    if body.role is not None:
        user.role = Role(body.role)
    if body.status is not None:
        user.status = UserStatus(body.status)
    if body.password:
        user.password_hash = hash_password(body.password)
    await db.commit()
    await db.refresh(user)
    return user


# ---------- 数据源权限 ----------

@router.get("/{user_id}/datasource-permissions")
async def get_permissions(user_id: str, db: AsyncSession = Depends(get_db),
                          _=Depends(require_admin)):
    rows = (await db.execute(
        select(DatasourcePermission).where(DatasourcePermission.user_id == uuid.UUID(user_id))
    )).scalars().all()
    return [{"datasource_id": str(r.datasource_id), "can_query": r.can_query} for r in rows]


@router.put("/{user_id}/datasource-permissions")
async def set_permissions(user_id: str, body: dict,
                          db: AsyncSession = Depends(get_db), _=Depends(require_admin)):
    """body: {"permissions": [{"datasource_id": "...", "can_query": true}]}（全量覆盖）"""
    user = await db.get(User, uuid.UUID(user_id))
    if user is None:
        raise NotFoundError("用户不存在")
    for r in (await db.execute(
            select(DatasourcePermission).where(DatasourcePermission.user_id == user.id))).scalars():
        await db.delete(r)
    for p in body.get("permissions", []):
        db.add(DatasourcePermission(user_id=user.id,
                                    datasource_id=uuid.UUID(p["datasource_id"]),
                                    can_query=bool(p.get("can_query", True))))
    await db.commit()
    return {"ok": True}
