# -*- coding: utf-8 -*-
"""密码哈希：bcrypt 优先，导入失败时自动降级 pbkdf2_hmac（600k 迭代，SHA-256）。

哈希串为「自描述格式」，两种格式都能校验通过：
- bcrypt:  "bcrypt$<完整 $2b$... 哈希>"
- pbkdf2:  "pbkdf2$<iterations>$<salt_hex>$<hash_hex>"

主理人已批准该降级方案（ARCHITECTURE_V2.md §11-1 / 任务书硬约束 #4）。
验证一律用恒定时间比较，绝不自写明文比较。
"""
import hashlib
import hmac
import os

# bcrypt 在导入期探测；不可用时回退 pbkdf2（零依赖）。
try:
    import bcrypt  # type: ignore
    _HAVE_BCRYPT = True
except Exception:  # noqa: BLE001  ImportError 或其他加载异常都回退
    _HAVE_BCRYPT = False

_PBKDF2_ITERATIONS = 600_000
_PBKDF2_SALT_BYTES = 16
_PBKDF2_HASH = "sha256"


def hash_password(password: str) -> str:
    """返回自描述格式哈希串。"""
    pw = password.encode("utf-8")
    if _HAVE_BCRYPT:
        hashed = bcrypt.hashpw(pw, bcrypt.gensalt())
        return "bcrypt$" + hashed.decode("ascii")
    salt = os.urandom(_PBKDF2_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_HASH, pw, salt, _PBKDF2_ITERATIONS)
    return "pbkdf2$" + "$".join([
        str(_PBKDF2_ITERATIONS),
        salt.hex(),
        dk.hex(),
    ])


def verify_password(password: str, stored: str) -> bool:
    """恒定时间校验；格式不符或异常一律返回 False（不暴露细节）。"""
    if not stored:
        return False
    try:
        pw = password.encode("utf-8")
        if stored.startswith("bcrypt$"):
            if not _HAVE_BCRYPT:
                return False
            return bcrypt.checkpw(pw, stored[len("bcrypt$"):].encode("ascii"))
        if stored.startswith("pbkdf2$"):
            _, iters_s, salt_hex, hash_hex = stored.split("$")
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(hash_hex)
            actual = hashlib.pbkdf2_hmac(_PBKDF2_HASH, pw, salt, int(iters_s))
            return hmac.compare_digest(expected, actual)
    except Exception:  # noqa: BLE001  解码/迭代失败均视为不匹配
        return False
    return False


def has_bcrypt() -> bool:
    return _HAVE_BCRYPT
