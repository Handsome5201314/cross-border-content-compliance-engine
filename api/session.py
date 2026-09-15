# -*- coding: utf-8 -*-
"""Cookie 会话（ARCHITECTURE_V3.md §2.3）。

选型：itsdangerous.TimestampSigner 签名 Cookie —— 平台保留 Authorization /
X-modelscope-* / X-studio-* 请求头，Cookie 是唯一安全通道；签名 Cookie 无状态、
零额外存储、同源请求自动携带。

- Cookie 名 ``cce_session``，TTL 7 天；
- 载荷 ``{"uid":<int>,"role":"user|admin"}``，签发时间由 TimestampSigner 内嵌；
- 密钥取环境变量 ``SESSION_SECRET``：生产（CCE_ENV=production）缺失即拒绝启动；
  本地缺失用随机值兜底并置位告警标志（前端经 /api/auth/me 的 session_insecure 展示）；
- HttpOnly + SameSite=Lax；Secure 由环境变量 SESSION_SECURE 控制（本地 http 为关）。
"""
import json
import logging
import os
import secrets

from itsdangerous import BadSignature, TimestampSigner

logger = logging.getLogger("cce.session")

COOKIE_NAME = "cce_session"
SESSION_TTL_S = 7 * 24 * 3600  # 7 天

_signer: TimestampSigner | None = None
_insecure_secret = False


def _get_signer() -> TimestampSigner:
    global _signer, _insecure_secret
    if _signer is not None:
        return _signer
    secret = (os.environ.get("SESSION_SECRET") or "").strip()
    if secret:
        _insecure_secret = False
    else:
        if (os.environ.get("CCE_ENV") or "").strip().lower() == "production":
            # 安全优先：生产环境没有会话密钥就直接拒绝启动（ARCHITECTURE_V3.md §7-2）。
            raise RuntimeError(
                "生产环境（CCE_ENV=production）必须设置 SESSION_SECRET 环境变量，拒绝启动")
        secret = secrets.token_hex(32)
        _insecure_secret = True
        logger.warning("SESSION_SECRET 未设置：本次进程使用随机临时密钥，重启后所有会话失效（仅限本地开发）。")
    _signer = TimestampSigner(secret)
    return _signer


def insecure_secret() -> bool:
    """当前是否使用随机临时密钥（供前端界面告警展示）。"""
    _get_signer()
    return _insecure_secret


def issue_session(user_id: int, role: str) -> str:
    """签发签名 Cookie 值。"""
    payload = json.dumps({"uid": int(user_id), "role": str(role)},
                         separators=(",", ":"), ensure_ascii=True)
    return _get_signer().sign(payload.encode("utf-8")).decode("ascii")


def verify_session(token: str) -> dict | None:
    """校验签名 Cookie；过期 / 篡改 / 格式错误一律返回 None（不泄露原因细节）。"""
    if not token:
        return None
    try:
        raw = _get_signer().unsign(token.encode("ascii"), max_age=SESSION_TTL_S)
        data = json.loads(raw.decode("utf-8"))
        uid, role = int(data["uid"]), str(data.get("role", ""))
        if role not in ("user", "admin"):
            return None
        return {"uid": uid, "role": role}
    except (BadSignature, KeyError, ValueError, TypeError, UnicodeError):
        return None


def cookie_max_age() -> int:
    return SESSION_TTL_S


def cookie_secure() -> bool:
    return (os.environ.get("SESSION_SECURE") or "").strip().lower() in ("1", "true", "yes")
