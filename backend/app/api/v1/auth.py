"""认证接口。"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.errors import AuthError
from app.core.security import create_token, verify_password
from app.db.models import AuditLog, User, UserStatus
from app.db.models.base import utcnow
from app.db.session import get_db
from app.schemas.entities import LoginIn, TokenOut

router = APIRouter(prefix="/auth", tags=["auth"])

# 登录失败限流（PRD 4.4）：连续 5 次密码错误锁定 10 分钟，防暴力破解
MAX_LOGIN_FAILS = 5
LOCK_MINUTES = 10


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    ip = request.client.host if request.client else ""
    user = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    now = utcnow()
    if user is not None and user.locked_until and user.locked_until > now:
        remain = int((user.locked_until - now).total_seconds() // 60) + 1
        raise AuthError(f"登录失败次数过多，账号已锁定，请约 {remain} 分钟后重试")
    if user is None or not verify_password(body.password, user.password_hash):
        if user is not None:
            # 失败计数入库（必须提交，否则限流无效）；不存在的账号不计数，避免用户名探测差异
            user.failed_login_count = (user.failed_login_count or 0) + 1
            locked_now = user.failed_login_count >= MAX_LOGIN_FAILS
            if locked_now:
                user.failed_login_count = 0
                user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
            db.add(AuditLog(user_id=user.id, username=user.username, action="login_failed",
                            ip=ip, detail={"locked": locked_now}))
            await db.commit()
        raise AuthError("用户名或密码错误")
    if user.status != UserStatus.ACTIVE:
        raise AuthError("账号已禁用")
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    db.add(AuditLog(user_id=user.id, username=user.username, action="login", ip=ip))
    await db.commit()
    return TokenOut(token=create_token(str(user.id), user.username, user.role.value),
                    username=user.username, display_name=user.display_name or user.username,
                    role=user.role.value)


@router.get("/me", response_model=dict)
async def me(user: User = Depends(get_current_user)):
    return {"id": str(user.id), "username": user.username, "display_name": user.display_name,
            "role": user.role.value}
