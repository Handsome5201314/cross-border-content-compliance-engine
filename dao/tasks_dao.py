# -*- coding: utf-8 -*-
"""任务 DAO：任务增 / 状态更新 / 最近任务查询（ARCHITECTURE_V2.md §3.2）。"""
import json

from dao.db import Database, now_iso


class TaskDAO:
    @classmethod
    def create(cls, user_id: int, product_name: str, product_name_en: str,
               languages: list, platforms: list, compliance_level: str) -> int:
        ts = now_iso()
        with Database.get().transaction() as conn:
            cur = conn.execute(
                "INSERT INTO tasks "
                "(user_id, product_name, product_name_en, languages, platforms, "
                " language_count, platform_count, compliance_level, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (user_id, product_name, product_name_en,
                 json.dumps(languages, ensure_ascii=False),
                 json.dumps(platforms, ensure_ascii=False),
                 len(languages), len(platforms), compliance_level, ts))
            return cur.lastrowid

    @classmethod
    def set_running(cls, task_id: int) -> None:
        with Database.get().transaction() as conn:
            conn.execute(
                "UPDATE tasks SET status = 'running' WHERE task_id = ?", (task_id,))

    @classmethod
    def finish(cls, task_id: int, status: str, *, credit_cost: int,
               credit_refunded: int, result_summary: dict, result_path: str | None) -> None:
        ts = now_iso()
        with Database.get().transaction() as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, credit_cost = ?, credit_refunded = ?, "
                " result_summary = ?, result_path = ?, finished_at = ? WHERE task_id = ?",
                (status, credit_cost, credit_refunded,
                 json.dumps(result_summary, ensure_ascii=False) if result_summary is not None else None,
                 result_path, ts, task_id))

    @classmethod
    def get(cls, task_id: int) -> dict | None:
        row = Database.get().query_one(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        if row:
            row["languages"] = json.loads(row["languages"]) if row.get("languages") else []
            row["platforms"] = json.loads(row["platforms"]) if row.get("platforms") else []
            row["result_summary"] = json.loads(row["result_summary"]) if row.get("result_summary") else None
        return row

    @classmethod
    def list_recent(cls, user_id: int, limit: int = 20) -> list:
        # 按 task_id 倒序（自增主键 = 真实创建序），避免同一秒时间戳并列导致乱序。
        rows = Database.get().query_all(
            "SELECT * FROM tasks WHERE user_id = ? ORDER BY task_id DESC LIMIT ?",
            (user_id, limit))
        for r in rows:
            r["languages"] = json.loads(r["languages"]) if r.get("languages") else []
            r["platforms"] = json.loads(r["platforms"]) if r.get("platforms") else []
            r["result_summary"] = json.loads(r["result_summary"]) if r.get("result_summary") else None
        return rows
