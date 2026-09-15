# -*- coding: utf-8 -*-
"""积分业务服务（ARCHITECTURE_V2.md §3.2 / §9-C2 / §10）。

所有余额变动保证原子性与一致性：
- 单事务内同时执行「users.credits 余额变动」与「credit_ledger 流水（balance_after=新余额）」；
- 模块级 `credits_lock` 串行化所有余额写，杜绝并发双花（与 dao.db 的写锁叠加防御）。
- 哈希/会话不在此层，本模块可在无 Streamlit 环境下被单测。
"""
import threading

from dao.db import Database, now_iso
from dao import users_dao, credits_dao
from credits import pricing


class InsufficientCredits(Exception):
    """余额不足以完成预扣。"""


# 串行化所有余额写，防并发双花（ARCHITECTURE_V2.md §10）。
credits_lock = threading.Lock()


def _atomic_change(user_id: int, delta: int, type: str, *, task_id: int | None = None,
                   operator_id: int | None = None, note: str | None = None) -> int:
    """单事务内：更新余额 + 写流水，返回新余额。"""
    db = Database.get()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE users SET credits = credits + ?, updated_at = ? WHERE user_id = ?",
            (delta, now_iso(), user_id))
        row = conn.execute(
            "SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()
        new_balance = int(row["credits"])
        conn.execute(
            "INSERT INTO credit_ledger "
            "(user_id, change, type, task_id, balance_after, operator_id, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, delta, type, task_id, new_balance, operator_id, note, now_iso()))
    return new_balance


# ---------------------------------------------------------------- 对外业务
def grant_signup(user_id: int) -> int:
    """注册赠送初始积分，并产生一条 grant 明细。返回新余额。"""
    return _atomic_change(user_id, pricing.SIGNUP_BONUS, "grant")


def pre_deduct(user_id: int, amount: int, *, task_id: int | None = None) -> int:
    """预扣积分；余额不足抛 InsufficientCredits（不改动余额）。返回新余额。"""
    if amount <= 0:
        return get_balance(user_id)
    with credits_lock:
        balance = get_balance(user_id)
        if balance < amount:
            raise InsufficientCredits(
                f"积分不足：本次需 {amount}，当前余额 {balance}")
        return _atomic_change(user_id, -amount, "consume", task_id=task_id)


def refund(user_id: int, task_id: int, amount: int) -> int:
    """取消 / 失败全额退还预扣积分。返回新余额。"""
    if amount <= 0:
        return get_balance(user_id)
    with credits_lock:
        return _atomic_change(user_id, +amount, "refund", task_id=task_id)


def recharge(admin_id: int, username: str, amount: int, note: str) -> int:
    """管理员充值：给用户余额 +amount，并写 recharge 流水（operator_id=管理员）。"""
    if not isinstance(amount, int) or amount <= 0:
        raise ValueError("充值额必须为正整数")
    with credits_lock:
        target = users_dao.UserDAO.get_by_username(username)
        if target is None:
            raise ValueError(f"用户「{username}」不存在")
        if not (note or "").strip():
            raise ValueError("充值备注必填")
        return _atomic_change(target["user_id"], +amount, "recharge",
                              operator_id=admin_id, note=(note or "").strip())


def get_balance(user_id: int) -> int:
    return users_dao.UserDAO.get_credits(user_id)


def get_ledger(user_id: int, *, limit: int = 200) -> list:
    return credits_dao.CreditDAO.ledger_for_user(user_id, limit=limit)
