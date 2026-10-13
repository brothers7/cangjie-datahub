"""API 依赖：认证 / 角色 / 数据源权限。"""
from __future__ import annotations

import uuid as _uuid

import jwt as pyjwt
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthError, PermissionDenied
from app.core.security import decode_token
from app.db.models import DataSource, DatasourcePermission, Role, User, UserStatus
from app.db.session import get_db


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        # SSE 场景允许 query token
        token = request.query_params.get("token", "")
    if not token:
        raise AuthError("未登录")
    payload = decode_token(token)
    if not payload:
        raise AuthError("登录已过期，请重新登录")
    user = await db.get(User, _uuid.UUID(payload["sub"]))
    if user is None or user.status != UserStatus.ACTIVE:
        raise AuthError("账号不可用")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != Role.ADMIN:
        raise PermissionDenied("需要管理员权限")
    return user


async def can_access_datasource(db: AsyncSession, user: User, datasource_id) -> bool:
    if user.role == Role.ADMIN:
        return True
    perm = (await db.execute(
        select(DatasourcePermission).where(
            DatasourcePermission.user_id == user.id,
            DatasourcePermission.datasource_id == datasource_id,
            DatasourcePermission.can_query.is_(True))
    )).scalar_one_or_none()
    return perm is not None


async def accessible_datasource_ids(db: AsyncSession, user: User) -> list | None:
    """用户可查询的数据源 id 列表；None 表示不限制（admin）。"""
    if user.role == Role.ADMIN:
        return None
    rows = (await db.execute(
        select(DatasourcePermission.datasource_id).where(
            DatasourcePermission.user_id == user.id,
            DatasourcePermission.can_query.is_(True))
    )).scalars().all()
    return list(rows)


async def get_datasource_checked(ds_id: str, user: User = Depends(get_current_user),
                                 db: AsyncSession = Depends(get_db)) -> DataSource:
    ds = await db.get(DataSource, _uuid.UUID(ds_id))
    if ds is None:
        from app.core.errors import NotFoundError
        raise NotFoundError("数据源不存在")
    if not await can_access_datasource(db, user, ds.id):
        raise PermissionDenied("无该数据源的查询权限")
    return ds
