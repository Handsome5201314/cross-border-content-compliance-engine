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
import time
import uuid

from itsdangerous import BadSignature, TimestampSigner

from dao import db as _db

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
    """签发签名 Cookie 值（含 jti，用于服务端吊销）。"""
    payload = json.dumps(
        {"uid": int(user_id), "role": str(role), "jti": uuid.uuid4().hex},
        separators=(",", ":"), ensure_ascii=True)
    return _get_signer().sign(payload.encode("utf-8")).decode("ascii")


def verify_session(token: str) -> dict | None:
    """校验签名 Cookie；过期 / 篡改 / 格式错误 / 已吊销一律返回 None（不泄露原因细节）。"""
    if not token:
        return None
    try:
        raw = _get_signer().unsign(token.encode("ascii"), max_age=SESSION_TTL_S)
        data = json.loads(raw.decode("utf-8"))
        uid, role = int(data["uid"]), str(data.get("role", ""))
        if role not in ("user", "admin"):
            return None
        jti = data.get("jti")
        if jti and is_revoked(jti):
            # 该会话已被登出/吊销：签名仍有效但服务端已作废（§会话吊销）
            return None
        return {"uid": uid, "role": role, "jti": jti}
    except (BadSignature, KeyError, ValueError, TypeError, UnicodeError):
        return None


# ---------------------------------------------------------------- 服务端会话吊销（jti 表）
def _ensure_revocation_table() -> None:
    """惰性建表（与 dao.db._SCHEMA 中的 DDL 幂等互补，覆盖 reset 后重连场景）。"""
    db = _db.Database.get()
    db.execute(
        "CREATE TABLE IF NOT EXISTS session_revocation ("
        "jti TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_revocation_expires "
        "ON session_revocation(expires_at)")


def revoke_session(jti: str) -> None:
    """吊销某个会话 jti（登出 / 改密时调用）。吊销记录按 Cookie TTL 过期清理。"""
    if not jti:
        return
    _ensure_revocation_table()
    db = _db.Database.get()
    now = int(time.time())
    with db.transaction():
        # 清理已过期吊销记录（TTL 边界），避免表无限增长
        db.execute("DELETE FROM session_revocation WHERE expires_at < ?", (now,))
        db.execute(
            "INSERT OR REPLACE INTO session_revocation (jti, expires_at) VALUES (?, ?)",
            (jti, now + SESSION_TTL_S))


def is_revoked(jti: str) -> bool:
    """该 jti 是否已被吊销。"""
    if not jti:
        return False
    _ensure_revocation_table()
    db = _db.Database.get()
    row = db.query_one("SELECT jti FROM session_revocation WHERE jti = ?", (jti,))
    return row is not None


def purge_expired_revocation() -> int:
    """清理过期的吊销记录（运维用）；返回清理条数。"""
    _ensure_revocation_table()
    db = _db.Database.get()
    now = int(time.time())
    with db.transaction():
        cur = db.execute("DELETE FROM session_revocation WHERE expires_at < ?", (now,))
        return cur.rowcount or 0


def cookie_max_age() -> int:
    return SESSION_TTL_S


def cookie_secure() -> bool:
    return (os.environ.get("SESSION_SECURE") or "").strip().lower() in ("1", "true", "yes")
