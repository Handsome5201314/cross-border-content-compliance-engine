# -*- coding: utf-8 -*-
"""认证端点：注册 / 登录 / 登出 / 当前用户 + 演示数据 + 看板参数（§2.2 #3-8）。

行为与 V2 app.py 的 do_register / do_login 保持一致：
- 注册：唯一性校验（重复 409）→ bcrypt 存密 → 注册赠送积分 → 自动发会话 Cookie；
- 登录：错误凭证统一「用户名或密码错误」（401，不区分账号不存在与密码错误）；
  禁用用户拦截并落 login_blocked 审计；
- 登出：清 Cookie（登录态才允许，未登录 401 由依赖层统一拦）。
"""
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response

from admin import service as admin_service
from api.deps import get_current_user
from api import session as cookie_session
from api.schemas import AuthIn
from auth.hashing import hash_password, verify_password
from core.value_calc import DEFAULT_PARAMS
from credits import pricing, service as credits_service
from dao import users_dao

logger = logging.getLogger("cce.auth")

ROOT = Path(__file__).resolve().parents[2]
PRECOMPUTED_PATH = ROOT / "samples" / "precomputed_full.json"

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_payload(user: dict) -> dict:
    return {"user_id": user["user_id"], "username": user["username"],
            "role": user["role"], "credits": int(user["credits"])}


def _set_session_cookie(response: Response, user: dict) -> None:
    token = cookie_session.issue_session(user["user_id"], user["role"])
    response.set_cookie(
        cookie_session.COOKIE_NAME, token,
        max_age=cookie_session.cookie_max_age(), path="/",
        httponly=True, samesite="lax", secure=cookie_session.cookie_secure())


@router.post("/register", status_code=201)
def register(body: AuthIn, response: Response):
    username = body.username.strip()
    if not username or not body.password:
        raise HTTPException(status_code=422, detail="用户名和密码不能为空")
    if users_dao.UserDAO.exists_username(username):
        raise HTTPException(status_code=409, detail="该用户名已被注册")
    uid = users_dao.UserDAO.create(username, hash_password(body.password), role="user")
    credits_service.grant_signup(uid)
    user = users_dao.UserDAO.get_by_id(uid)
    _set_session_cookie(response, user)
    payload = _user_payload(user)
    payload["session_insecure"] = cookie_session.insecure_secret()
    return payload


@router.post("/login")
def login(body: AuthIn, response: Response):
    user = users_dao.UserDAO.get_by_username(body.username.strip())
    if user is None or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user["status"] == "disabled":
        from dao import audit_dao
        audit_dao.AuditDAO.append(user["user_id"], "login_blocked", user["user_id"],
                                  detail={"reason": "disabled"})
        raise HTTPException(status_code=401, detail="该账号已被禁用，请联系管理员")
    _set_session_cookie(response, user)
    payload = _user_payload(user)
    payload["session_insecure"] = cookie_session.insecure_secret()
    return payload


@router.post("/logout")
def logout(response: Response, user: dict = Depends(get_current_user)):
    response.delete_cookie(cookie_session.COOKIE_NAME, path="/")
    return {}


@router.get("/me")
def me(user: dict = Depends(get_current_user)):
    payload = _user_payload(user)
    payload["status"] = user["status"]
    payload["session_insecure"] = cookie_session.insecure_secret()
    return payload


# ---------------------------------------------------------------- 演示模式与看板（公开）
_demo_cache: dict = {"mtime": None, "data": None}


def demo_router() -> APIRouter:
    """演示 / 看板端点挂在独立前缀（/api/demo、/api/value-board）。"""
    r = APIRouter(tags=["demo"])

    @r.get("/api/demo/precomputed")
    def demo_precomputed():
        """预生成全量产物（免登录演示模式，含 generated_at；只读，不写用户目录）。"""
        if not PRECOMPUTED_PATH.exists():
            raise HTTPException(status_code=404, detail="预生成数据不存在，请先运行 precompute_full.py")
        mtime = PRECOMPUTED_PATH.stat().st_mtime
        if _demo_cache["data"] is None or _demo_cache["mtime"] != mtime:
            _demo_cache["data"] = json.loads(PRECOMPUTED_PATH.read_text(encoding="utf-8"))
            _demo_cache["mtime"] = mtime
        return _demo_cache["data"]

    @r.get("/api/value-board/params")
    def value_board_params():
        """价值看板默认参数（纯计算，免登录）。"""
        return {"signup_bonus": pricing.SIGNUP_BONUS,
                "per_language": pricing.PER_LANGUAGE,
                "per_image": pricing.PER_IMAGE,
                "value_params": DEFAULT_PARAMS}

    return r
