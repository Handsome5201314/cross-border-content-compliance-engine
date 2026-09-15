# -*- coding: utf-8 -*-
"""请求依赖：从 cce_session Cookie 解析当前用户 / 管理员（ARCHITECTURE_V3.md §2.3）。

- 401：无 Cookie / 签名无效 / 过期 / 用户不存在 / 用户已被禁用；
- 403：已登录但角色非 admin；
- 禁用用户即使持有合法 Cookie 也拒绝登录态，并落 login_blocked 审计（与 V2 行为一致）。
"""
from fastapi import HTTPException, Request

from api import session as cookie_session
from dao import audit_dao, users_dao


def _load_user(request: Request) -> dict:
    token = request.cookies.get(cookie_session.COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    payload = cookie_session.verify_session(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="会话无效或已过期，请重新登录")
    user = users_dao.UserDAO.get_by_id(payload["uid"])
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在，请重新登录")
    if user["status"] == "disabled":
        audit_dao.AuditDAO.append(user["user_id"], "login_blocked", user["user_id"],
                                  detail={"reason": "disabled_with_valid_cookie"})
        raise HTTPException(status_code=401, detail="该账号已被禁用，请联系管理员")
    # 透传会话 jti 供登出吊销使用（见 api/routers/auth.py logout）
    user["jti"] = payload.get("jti")
    return user


def get_current_user(request: Request) -> dict:
    """登录态依赖：返回 users 表行（credits 为实时余额）。"""
    return _load_user(request)


def get_current_admin(request: Request) -> dict:
    """管理员依赖：非 admin 角色 403。"""
    user = _load_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
