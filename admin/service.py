# -*- coding: utf-8 -*-
"""管理后台业务服务（ARCHITECTURE_V2.md §3.2 / §9-G1）。

用户管理（列表 / 禁用 / 启用）与充值编排；所有管理操作经 AuditDAO 落审计流水。
充值委托 credits_service.recharge（已写 ledger + balance），本层负责补充审计。
注意：本模块不依赖 Streamlit，可在无 UI 环境下被单测。
"""
from dao import users_dao, audit_dao
from credits import service as credits_service


def list_users(*, only_active: bool = False) -> list:
    return users_dao.UserDAO.list_all(only_active=only_active)


def disable_user(operator_id: int, target_user_id: int, reason: str | None = None) -> None:
    """禁用用户（= 禁止登录，R19 / Q6）并落审计。"""
    users_dao.UserDAO.set_status(target_user_id, "disabled")
    audit_dao.AuditDAO.append(operator_id, "disable_user", target_user_id,
                              detail={"reason": reason})


def enable_user(operator_id: int, target_user_id: int, reason: str | None = None) -> None:
    """启用用户并落审计。"""
    users_dao.UserDAO.set_status(target_user_id, "active")
    audit_dao.AuditDAO.append(operator_id, "enable_user", target_user_id,
                              detail={"reason": reason})


def recharge(operator_id: int, username: str, amount: int, note: str) -> int:
    """管理员充值：委托 credits_service.recharge（写 ledger + 余额），并补审计。"""
    new_balance = credits_service.recharge(operator_id, username, amount, note)
    target = users_dao.UserDAO.get_by_username(username)
    audit_dao.AuditDAO.append(operator_id, "recharge", target["user_id"],
                              detail={"amount": amount, "note": note,
                                      "new_balance": new_balance})
    return new_balance


def audit_list(*, operator_id: int | None = None, target_user_id: int | None = None,
               action: str | None = None, limit: int = 500) -> list:
    return audit_dao.AuditDAO.list(operator_id=operator_id, target_user_id=target_user_id,
                                   action=action, limit=limit)
