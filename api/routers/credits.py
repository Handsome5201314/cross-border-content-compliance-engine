# -*- coding: utf-8 -*-
"""积分端点：余额 / 明细（§2.2 #15-16）。"""
from fastapi import APIRouter, Depends, Query

from api.deps import get_current_user
from credits import service as credits_service
from dao import credits_dao

router = APIRouter(prefix="/api/credits", tags=["credits"])


@router.get("/balance")
def balance(user: dict = Depends(get_current_user)):
    return {"credits": credits_service.get_balance(user["user_id"])}


@router.get("/ledger")
def ledger(type: str | None = Query(None, max_length=32),
           since: str | None = Query(None, max_length=32),
           until: str | None = Query(None, max_length=32),
           limit: int = Query(100, ge=1, le=500),
           user: dict = Depends(get_current_user)):
    rows = credits_dao.CreditDAO.ledger_for_user(
        user["user_id"], type=type, since=since, until=until, limit=limit)
    return {"ledger": rows}
