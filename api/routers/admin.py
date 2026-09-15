# -*- coding: utf-8 -*-
"""管理后台端点：用户管理 / 禁用 / 启用 / 充值 / 审计 / 任务流水（§2.2 #21-26）。

范围 = V2 范围（用户管理 + 充值 + 审计 + 任务流水），不做用量统计 / 模型网关。
全部操作走 V2 admin_service（自带审计落库）。

注：跨用户任务流水查询在 API 层直接用 SQL 实现 —— V2 tasks 层只提供按用户的
查询接口，为满足「业务层零改动」硬约束，不在业务层新增方法。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from admin import service as admin_service
from api.deps import get_current_admin
from api.schemas import RechargeIn
from dao import users_dao
from dao.db import Database
from storage import space as storage_space

logger = logging.getLogger("cce.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _user_brief(u: dict) -> dict:
    return {"user_id": u["user_id"], "username": u["username"], "role": u["role"],
            "status": u["status"], "credits": int(u["credits"]),
            "created_at": u["created_at"]}


@router.get("/users")
def users(only_active: bool = False, admin: dict = Depends(get_current_admin)):
    rows = admin_service.list_users(only_active=only_active)
    out = []
    for u in rows:
        item = _user_brief(u)
        # 真实目录计数（替换设计稿假 12/3/9 / 128MB）：文件数诚实统计，不估体积。
        uid = u["user_id"]
        item["inputs_count"] = storage_space.count_files(storage_space.inputs_dir(uid))
        item["outputs_count"] = storage_space.count_files(storage_space.outputs_dir(uid))
        out.append(item)
    return {"users": out}


@router.post("/users/{user_id}/disable")
def disable(user_id: int, admin: dict = Depends(get_current_admin)):
    target = users_dao.UserDAO.get_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user_id == admin["user_id"]:
        raise HTTPException(status_code=409, detail="不能禁用当前登录的管理员自己")
    admin_service.disable_user(admin["user_id"], user_id, "管理员操作")
    return {}


@router.post("/users/{user_id}/enable")
def enable(user_id: int, admin: dict = Depends(get_current_admin)):
    target = users_dao.UserDAO.get_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    admin_service.enable_user(admin["user_id"], user_id, "管理员操作")
    return {}


@router.post("/recharge")
def recharge(body: RechargeIn, admin: dict = Depends(get_current_admin)):
    try:
        new_balance = admin_service.recharge(admin["user_id"], body.username.strip(),
                                             body.amount, body.note.strip())
    except ValueError as e:
        msg = str(e)
        status = 404 if "不存在" in msg else 422
        raise HTTPException(status_code=status, detail=msg)
    target = users_dao.UserDAO.get_by_username(body.username.strip())
    return {"user_id": target["user_id"], "new_balance": new_balance}


@router.get("/audit")
def audit(action: str | None = Query(None, max_length=64),
          target_user_id: int | None = Query(None),
          limit: int = Query(200, ge=1, le=1000),
          admin: dict = Depends(get_current_admin)):
    rows = admin_service.audit_list(action=action, target_user_id=target_user_id, limit=limit)
    return {"audit": rows}


@router.get("/task-ledger")
def task_ledger(user_id: int | None = Query(None),
                since: str | None = Query(None, max_length=32),
                until: str | None = Query(None, max_length=32),
                limit: int = Query(200, ge=1, le=1000),
                admin: dict = Depends(get_current_admin)):
    sql = ("SELECT t.task_id, t.user_id, u.username, t.product_name, t.language_count, "
           "t.platform_count, t.status, t.credit_cost, t.credit_refunded, "
           "t.created_at, t.finished_at "
           "FROM tasks t LEFT JOIN users u ON u.user_id = t.user_id WHERE 1=1")
    params: list = []
    if user_id is not None:
        sql += " AND t.user_id = ?"
        params.append(user_id)
    if since is not None:
        sql += " AND t.created_at >= ?"
        params.append(since)
    if until is not None:
        sql += " AND t.created_at <= ?"
        params.append(until)
    sql += " ORDER BY t.task_id DESC LIMIT ?"
    params.append(limit)
    return {"tasks": Database.get().query_all(sql, tuple(params))}
