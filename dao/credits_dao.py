# -*- coding: utf-8 -*-
"""积分流水 DAO：每笔余额变动一条流水（含运行余额 balance_after）。"""
from dao.db import Database, now_iso


class CreditDAO:
    @classmethod
    def append(cls, user_id: int, change: int, type: str, *,
               task_id: int | None = None, balance_after: int,
               operator_id: int | None = None, note: str | None = None) -> int:
        with Database.get().transaction() as conn:
            cur = conn.execute(
                "INSERT INTO credit_ledger "
                "(user_id, change, type, task_id, balance_after, operator_id, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, change, type, task_id, balance_after,
                 operator_id, note, now_iso()))
            return cur.lastrowid

    @classmethod
    def ledger_for_user(cls, user_id: int, *, type: str | None = None,
                        since: str | None = None, until: str | None = None,
                        limit: int = 200) -> list:
        sql = "SELECT * FROM credit_ledger WHERE user_id = ?"
        params = [user_id]
        if type is not None:
            sql += " AND type = ?"
            params.append(type)
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since)
        if until is not None:
            sql += " AND created_at <= ?"
            params.append(until)
        sql += " ORDER BY ledger_id DESC LIMIT ?"
        params.append(limit)
        return Database.get().query_all(sql, tuple(params))

    @classmethod
    def ledger_all(cls, *, user_id: int | None = None, action: str | None = None,
                   since: str | None = None, until: str | None = None,
                   limit: int = 500) -> list:
        sql = "SELECT * FROM credit_ledger WHERE 1=1"
        params = []
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if action is not None:
            sql += " AND type = ?"
            params.append(action)
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since)
        if until is not None:
            sql += " AND created_at <= ?"
            params.append(until)
        sql += " ORDER BY ledger_id DESC LIMIT ?"
        params.append(limit)
        return Database.get().query_all(sql, tuple(params))

    @classmethod
    def has_refund_for_task(cls, user_id: int, task_id: int) -> bool:
        """幂等守卫：同 user_id + task_id 是否已存在 'refund' 流水（防重复退款双花）。"""
        row = Database.get().query_one(
            "SELECT 1 FROM credit_ledger WHERE user_id = ? AND task_id = ? AND type = 'refund' LIMIT 1",
            (user_id, task_id))
        return row is not None
