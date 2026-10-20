"""认证接口。"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.errors import AuthError
from app.core.security import create_token, verify_password
from app.db.models import AuditLog, User, UserStatus
from app.db.models.base import utcnow
from app.db.session import get_db
from app.schemas.entities import LoginIn, TokenOut

router = APIRouter(prefix="/auth", tags=["auth"])

# 登录失败限流（PRD 4.4）：连续密码错误达到上限即锁定，防暴力破解；阈值走 settings 可调
MAX_LOGIN_FAILS = settings.login_max_fails
LOCK_MINUTES = settings.login_lock_minutes

# 时序防枚举：不存在的账号也执行一次 argon2 校验（对预生成的假 hash），
# 使响应耗时不泄露账号是否存在。该 hash 不对应任何真实账号。
_DUMMY_HASH = ("$argon2id$v=19$m=65536,t=3,p=4$MonXBSkoeJc0Y7TJWeoGbg$"
               "mdc3rsG6q30M06jYSFL5Wn5X8OyQjMTWGKeRHaQnvXk")


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    ip = request.client.host if request.client else ""
    user = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    now = utcnow()
    if user is not None and user.locked_until and user.locked_until > now:
        remain = int((user.locked_until - now).total_seconds() // 60) + 1
        raise AuthError(f"登录失败次数过多，账号已锁定，请约 {remain} 分钟后重试")
    if user is None:
        # dummy 校验拉平耗时；不存在的账号不计入锁定计数（保持既有行为）
        verify_password(body.password, _DUMMY_HASH)
        raise AuthError("用户名或密码错误")
    if not verify_password(body.password, user.password_hash):
        # 失败计数入库（必须提交，否则限流无效）
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
