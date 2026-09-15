# -*- coding: utf-8 -*-
"""用户 DAO：增查、状态/角色、余额读写（ARCHITECTURE_V2.md §3.2）。"""
from dao.db import Database, now_iso


class UserDAO:
    @classmethod
    def create(cls, username: str, password_hash: str, role: str = "user",
               credits: int = 0) -> int:
        ts = now_iso()
        with Database.get().transaction() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, role, status, credits, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', ?, ?, ?)",
                (username, password_hash, role, credits, ts, ts))
            return cur.lastrowid

    @classmethod
    def get_by_username(cls, username: str) -> dict | None:
        return Database.get().query_one(
            "SELECT * FROM users WHERE username = ?", (username,))

    @classmethod
    def get_by_id(cls, user_id: int) -> dict | None:
        return Database.get().query_one(
            "SELECT * FROM users WHERE user_id = ?", (user_id,))

    @classmethod
    def exists_username(cls, username: str) -> bool:
        return Database.get().query_one(
            "SELECT 1 FROM users WHERE username = ?", (username,)) is not None

    @classmethod
    def set_status(cls, user_id: int, status: str) -> None:
        with Database.get().transaction() as conn:
            conn.execute(
                "UPDATE users SET status = ?, updated_at = ? WHERE user_id = ?",
                (status, now_iso(), user_id))

    @classmethod
    def set_role(cls, user_id: int, role: str) -> None:
        with Database.get().transaction() as conn:
            conn.execute(
                "UPDATE users SET role = ?, updated_at = ? WHERE user_id = ?",
                (role, now_iso(), user_id))

    @classmethod
    def get_credits(cls, user_id: int) -> int:
        row = Database.get().query_one(
            "SELECT credits FROM users WHERE user_id = ?", (user_id,))
        return int(row["credits"]) if row else 0

    @classmethod
    def add_credits(cls, user_id: int, delta: int) -> int:
        """事务内更新余额，返回新余额。并发由 Database.transaction() 的写锁串行化。"""
        with Database.get().transaction() as conn:
            conn.execute(
                "UPDATE users SET credits = credits + ?, updated_at = ? WHERE user_id = ?",
                (delta, now_iso(), user_id))
            row = conn.execute(
                "SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return int(row["credits"])

    @classmethod
    def list_all(cls, *, only_active: bool = False) -> list:
        if only_active:
            return Database.get().query_all(
                "SELECT * FROM users WHERE status = 'active' ORDER BY user_id")
        return Database.get().query_all("SELECT * FROM users ORDER BY user_id")
