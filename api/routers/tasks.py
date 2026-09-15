# -*- coding: utf-8 -*-
"""任务端点：最近任务 / 详情 / 结果下载（§2.2 #12-14）。"""
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from api.deps import get_current_user
from dao import tasks_dao
from storage import space as storage_space
from tasks import service as tasks_service

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _owned_task(task_id: int, user: dict) -> dict:
    task = tasks_dao.TaskDAO.get(task_id)
    if task is None or task["user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


def _brief(t: dict) -> dict:
    return {"task_id": t["task_id"], "product_name": t["product_name"],
            "language_count": t["language_count"], "platform_count": t["platform_count"],
            "status": t["status"], "summary": t.get("result_summary"),
            "credit_cost": t["credit_cost"], "credit_refunded": t["credit_refunded"],
            "created_at": t["created_at"], "finished_at": t["finished_at"],
            "languages": t.get("languages") or [], "platforms": t.get("platforms") or []}


@router.get("/recent")
def recent(limit: int = Query(20, ge=1, le=100), user: dict = Depends(get_current_user)):
    rows = tasks_service.recent_tasks(user["user_id"], limit=limit)
    return {"tasks": [_brief(t) for t in rows]}


@router.get("/{task_id}")
def detail(task_id: int, user: dict = Depends(get_current_user)):
    return _owned_task(task_id, user)


@router.get("/{task_id}/result")
def result(task_id: int, user: dict = Depends(get_current_user)):
    """结果 JSON 下载；文件名 {product}_{langN}lang_{ts}.json（R08，可区分来源）。"""
    task = _owned_task(task_id, user)
    data = storage_space.read_result(user["user_id"], task_id)
    if data is None:
        rp = task.get("result_path")
        if rp and Path(rp).exists():
            try:
                data = json.loads(Path(rp).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
    if data is None:
        raise HTTPException(status_code=404, detail="结果文件不存在（任务可能未成功产出）")
    name = storage_space.secure_filename(task["product_name"] or "task", max_len=40)
    filename = f"{name}_{len(task.get('languages') or [])}lang_{datetime.now():%Y%m%d_%H%M%S}.json"
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(content=content, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
