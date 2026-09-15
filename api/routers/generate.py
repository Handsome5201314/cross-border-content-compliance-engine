# -*- coding: utf-8 -*-
"""生成端点：创建任务（预扣积分）+ SSE 进度流 + 取消（§2.2 #9-11 / §2.4）。

流程（与 V2 app.py 语义一致）：
1. 登录校验；同用户已有进行中任务 → 409（每用户单 worker，防余额/任务错乱）；
2. 建 pending 任务 → 按语种数预扣积分（不足 402，任务落 failed 不收费）；
3. 启动 GenerationWorker 后台线程，立即返回 task_id；
4. 前端 EventSource 订阅 /stream（Cookie 自动携带，不用任何平台保留头）；
5. 取消触发 pipeline.request_cancel()；worker 收尾按 cancelled/failed 全额退。
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from api import generation
from api.deps import get_current_user
from api.schemas import GenerateIn
from core.privacy_gate import PrivacyViolation, validate_product
from credits import service as credits_service
from dao import tasks_dao
from tasks import service as tasks_service

logger = logging.getLogger("cce.generate")

router = APIRouter(prefix="/api/generate", tags=["generate"])

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                "Connection": "keep-alive"}


def _owned_task(task_id: int, user: dict) -> dict:
    task = tasks_dao.TaskDAO.get(task_id)
    if task is None or task["user_id"] != user["user_id"]:
        # 404 而非 403：不向其他用户泄露任务存在性。
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.post("", status_code=201)
def create_generation(body: GenerateIn, user: dict = Depends(get_current_user)):
    uid = user["user_id"]

    languages = [str(x).strip() for x in body.languages if str(x).strip()]
    platforms = [str(x).strip() for x in body.platforms if str(x).strip()]
    if not languages or not platforms:
        raise HTTPException(status_code=422, detail="语种与平台不能为空")
    if len(set(languages)) != len(languages) or len(set(platforms)) != len(platforms):
        raise HTTPException(status_code=422, detail="语种与平台不能重复")
    models = {k: v for k, v in (body.models or {}).items()
              if k in ("main", "fast", "review", "image") and isinstance(v, str) and v.strip()}

    # 隐私门禁前置：命中患者标识在预扣前就拒绝（Pipeline 内还会再拦一次，纵深防御）。
    product = _build_product(body, languages, platforms)
    try:
        validate_product(product)
    except PrivacyViolation as e:
        raise HTTPException(status_code=422, detail=str(e))

    if generation.has_active_worker(uid):
        raise HTTPException(status_code=409, detail="已有生成任务进行中，请等待完成或先取消")

    cost = generation.estimate_cost(languages)
    task_id = tasks_service.create_task(uid, body.product_name.strip(),
                                        body.product_name_en.strip(), languages,
                                        platforms, body.compliance_level)
    try:
        credits_service.pre_deduct(uid, cost, task_id=task_id)
    except credits_service.InsufficientCredits as e:
        # 未扣款成功：任务如实落 failed，不产生费用。
        tasks_service.finalize_task(uid, task_id, "failed", credit_cost=0,
                                    credit_refunded=0, result=None)
        raise HTTPException(status_code=402, detail=str(e))
    except Exception:
        tasks_service.finalize_task(uid, task_id, "failed", credit_cost=0,
                                    credit_refunded=0, result=None)
        raise

    worker = generation.GenerationWorker(
        user_id=uid, task_id=task_id, cost=cost, product=product,
        languages=languages, platforms=platforms,
        compliance_level=body.compliance_level, models=models,
        deadline_s=body.deadline_s)
    generation.register_worker(uid, worker)
    try:
        worker.start()
    except Exception:
        generation.unregister_worker(uid, worker)
        _, ok = credits_service.refund(uid, task_id, cost)
        tasks_service.finalize_task(uid, task_id, "failed", credit_cost=cost,
                                    credit_refunded=cost if ok else 0, result=None)
        raise HTTPException(status_code=500, detail="任务启动失败，预扣积分已退还")

    logger.info("生成任务已创建 user=%s task=%s cost=%s", uid, task_id, cost)
    return {"task_id": task_id, "cost": cost,
            "balance": credits_service.get_balance(uid)}


@router.get("/{task_id}/stream")
def stream(task_id: int, user: dict = Depends(get_current_user)):
    task = _owned_task(task_id, user)
    worker = generation.get_worker(user["user_id"])
    if worker is not None and worker.task_id == task_id:
        return StreamingResponse(worker.sse_events(), media_type="text/event-stream",
                                 headers=_SSE_HEADERS)
    # 没有活跃 worker：任务已到终态（或服务重启后孤儿已回收），按落库状态合成终态事件。
    status = task["status"]
    ev_type = {"completed": "done", "cancelled": "cancelled"}.get(status, "failed")
    payload = {"type": ev_type, "task_id": task_id, "status": status,
               "counts": task.get("result_summary") or {},
               "balance": credits_service.get_balance(user["user_id"])}
    if ev_type == "failed" and status in ("pending", "running"):
        payload["error"] = "服务曾重启，任务已中断；预扣积分已自动退还"

    def gen():
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "data: {\"type\": \"bye\"}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/{task_id}/cancel")
def cancel(task_id: int, user: dict = Depends(get_current_user)):
    _owned_task(task_id, user)
    worker = generation.get_worker(user["user_id"])
    if worker is not None and worker.task_id == task_id:
        worker.request_cancel()
        return {"cancelled": True}
    raise HTTPException(status_code=409, detail="任务不在进行中，无法取消")


def _build_product(body: GenerateIn, languages: list, platforms: list) -> dict:
    """组装产品输入：以医疗示例为基底（capabilities/target_buyers 等事实字段），
    表单字段覆盖；与 V2 app.py 的构建方式一致（字段全部在 privacy_gate 白名单内）。"""
    sample = _sample_product_path()
    base = json.loads(sample.read_text(encoding="utf-8")) if sample.exists() else {}
    product = {**base,
               "product_name": body.product_name.strip(),
               "product_name_en": body.product_name_en.strip(),
               "form": body.form.strip() or base.get("form", ""),
               "deployment": body.deployment.strip() or base.get("deployment", ""),
               "marketing_notes": body.marketing_notes.strip(),
               "target_languages": languages,
               "target_platforms": platforms,
               "compliance_level": body.compliance_level}
    if body.target_markets:
        product["target_markets"] = [m.strip() for m in body.target_markets if m.strip()]
    return product


def _sample_product_path():
    from pathlib import Path
    return Path(__file__).resolve().parents[2] / "samples" / "product_medical_appliance.json"
