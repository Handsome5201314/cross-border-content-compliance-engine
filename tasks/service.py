# -*- coding: utf-8 -*-
"""任务业务服务（ARCHITECTURE_V2.md §3.2 / §9-D1）。

建任务 → 生成 → 完成/取消/失败 → 落库 + 结果 JSON 落用户隔离目录。
结果落盘经 storage.write_result（隔离层），与 tasks 表双写，便于读取与审计。
"""
from dao import tasks_dao
from storage import space as storage_space


def create_task(user_id: int, product_name: str, product_name_en: str,
                languages: list, platforms: list, compliance_level: str) -> int:
    """建任务（status=pending），返回 task_id。"""
    return tasks_dao.TaskDAO.create(
        user_id, product_name, product_name_en, languages, platforms, compliance_level)


def finalize_task(user_id: int, task_id: int, status: str, *,
                  credit_cost: int, credit_refunded: int,
                  result: dict | None = None) -> dict | None:
    """写任务终态 + 结果 JSON 落盘；返回落盘后的结果快照（若有）。"""
    summary = _build_summary(result)
    result_path = None
    if result is not None:
        result_path = str(storage_space.write_result(user_id, task_id, result))
    tasks_dao.TaskDAO.finish(
        task_id, status, credit_cost=credit_cost, credit_refunded=credit_refunded,
        result_summary=summary, result_path=result_path)
    return summary


def _build_summary(result: dict | None) -> dict:
    counts = (result or {}).get("counts", {}) or {}
    return {
        "delivered": counts.get("delivered", 0),
        "review_blocked": counts.get("review_blocked", 0),
        "failed": counts.get("failed", 0),
        "skipped_deadline": counts.get("skipped_deadline", 0),
    }


def recent_tasks(user_id: int, limit: int = 20) -> list:
    """最近任务真实列表（按创建倒序）。"""
    return tasks_dao.TaskDAO.list_recent(user_id, limit=limit)
