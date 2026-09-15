# -*- coding: utf-8 -*-
"""管理端审计流水 DAO：所有管理操作落库。"""
import json

from dao.db import Database, now_iso


class AuditDAO:
    @classmethod
    def append(cls, operator_id: int, action: str, target_user_id: int | None,
               detail: dict | None = None) -> int:
        with Database.get().transaction() as conn:
            cur = conn.execute(
                "INSERT INTO audit_log (operator_id, action, target_user_id, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (operator_id, action, target_user_id,
                 json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                 now_iso()))
            return cur.lastrowid

    @classmethod
    def list(cls, *, operator_id: int | None = None, target_user_id: int | None = None,
             action: str | None = None, limit: int = 500) -> list:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params = []
        if operator_id is not None:
            sql += " AND operator_id = ?"
            params.append(operator_id)
        if target_user_id is not None:
            sql += " AND target_user_id = ?"
            params.append(target_user_id)
        if action is not None:
            sql += " AND action = ?"
            params.append(action)
        sql += " ORDER BY audit_id DESC LIMIT ?"
        params.append(limit)
        rows = Database.get().query_all(sql, tuple(params))
        for r in rows:
            r["detail"] = json.loads(r["detail"]) if r.get("detail") else None
        return rows
