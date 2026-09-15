# -*- coding: utf-8 -*-
"""GenerationWorker：后台线程跑 Pipeline，进度经 queue 转 SSE（ARCHITECTURE_V3.md §2.4）。

要点：
- 每用户同时仅 1 个生成任务：全局 ``dict[user_id, worker]`` + 锁，防止同用户双开
  导致余额 / 任务错乱；积分防双花仍由 V2 ``credits_lock`` 兜底。
- worker 子线程绝不写任何响应对象，只往 ``queue.Queue`` 放事件；SSE 生成器在
  请求侧读队列并以 ``data: {json}\\n\\n`` 推给前端，空闲 15s 发 ``: ping`` 心跳注释行。
- 收尾语义与 V2 app.py 一致：completed 不退款；cancelled / failed 全额退还预扣
  （``credits_service.refund`` 自带 task_id 幂等守卫，重复收尾不会双花）。
- ``create_llm_client`` / ``build_pipeline`` 为可替换工厂（测试注入 stub，不出网）。
"""
import logging
import queue
import threading

from credits import pricing, service as credits_service
from credits.service import InsufficientCredits  # noqa: F401 （re-export 供路由使用）
from core.llm_client import LLMClient
from core.pipeline import Pipeline
from core.privacy_gate import validate_product
from dao import tasks_dao
from tasks import service as tasks_service

logger = logging.getLogger("cce.generation")

# 事件队列哨兵：SSE 生成器收到 None 即结束流。
_SENTINEL = None


def create_llm_client() -> LLMClient:
    """构造 LLM 客户端（.env / 环境变量凭证）。失败时 worker 走 failed + 退款路径。"""
    return LLMClient()


def build_pipeline(client, product: dict, languages: list, platforms: list,
                   compliance_level: str, models: dict, deadline_s: float) -> Pipeline:
    """构造 Pipeline（独立函数便于测试替换，不出网）。"""
    return Pipeline(client, product, languages=languages, platforms=platforms,
                    compliance_level=compliance_level, models=models,
                    batch_deadline_s=float(deadline_s))


class GenerationWorker:
    """单用户单任务的后台生成器。"""

    def __init__(self, user_id: int, task_id: int, cost: int,
                 product: dict, languages: list, platforms: list,
                 compliance_level: str, models: dict, deadline_s: float):
        self.user_id = int(user_id)
        self.task_id = int(task_id)
        self.cost = int(cost)
        self.product = product
        self.languages = list(languages)
        self.platforms = list(platforms)
        self.compliance_level = compliance_level
        self.models = dict(models or {})
        self.deadline_s = float(deadline_s)

        self.events: queue.Queue = queue.Queue()
        self.pipeline: Pipeline | None = None
        self._cancel_intent = False   # pipeline 尚未建好时的取消意图
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"cce-gen-{self.task_id}",
                                        daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def request_cancel(self) -> None:
        """用户取消：pipeline 已建则直接置取消标志；未建则记意图，建好后立即置。"""
        if self.pipeline is not None:
            self.pipeline.request_cancel()
        else:
            self._cancel_intent = True

    # ---------------------------------------------------------------- SSE
    def sse_events(self):
        """StreamingResponse 用的同步生成器：读队列 -> SSE 帧；15s 空闲发心跳。"""
        while True:
            try:
                ev = self.events.get(timeout=15)
            except queue.Empty:
                yield ": ping\n\n"   # SSE 注释行心跳，防网关缓冲断连
                continue
            if ev is None:
                yield "data: {\"type\": \"bye\"}\n\n"
                return
            yield f"data: {ev}\n\n"  # 事件在 _emit 时已序列化为 JSON 字符串

    # ---------------------------------------------------------------- 内部
    def _emit(self, event: dict) -> None:
        """事件入队（提前序列化，保证 sse_events 拿到即可发）。"""
        import json
        try:
            self.events.put(json.dumps(event, ensure_ascii=False))
        except Exception:  # noqa: BLE001 事件序列化失败不应拖垮 worker
            logger.exception("SSE 事件序列化失败")

    def _on_progress(self, stage: str, done: int, total: int, message: str) -> None:
        self._emit({"type": "progress", "stage": stage, "done": int(done),
                    "total": int(total), "message": str(message)[:200]})

    def _on_item(self, platform: str, language: str, item: dict) -> None:
        # 裁决卡只需要 localized/compliance/final_copy；平台基础 copy 体积大且非展示必需，剥离。
        slim = {k: v for k, v in item.items() if k not in ("copy",)}
        self._emit({"type": "item", "platform": platform, "language": language, "item": slim})

    def _run(self) -> None:
        try:
            self._emit({"type": "progress", "stage": "start", "done": 0, "total": 1,
                        "message": "任务启动，准备调用引擎…"})
            client = create_llm_client()
            pipeline = build_pipeline(client, self.product, self.languages,
                                      self.platforms, self.compliance_level,
                                      self.models, self.deadline_s)
            self.pipeline = pipeline
            if self._cancel_intent:
                pipeline.request_cancel()
            result = pipeline.run(progress_cb=self._on_progress, item_cb=self._on_item)
            cancelled = bool((result.get("meta") or {}).get("cancelled"))
            if cancelled:
                status, refunded = "cancelled", self._refund()
            else:
                status, refunded = "completed", 0
            tasks_service.finalize_task(self.user_id, self.task_id, status,
                                        credit_cost=self.cost, credit_refunded=refunded,
                                        result=result)
            summary = tasks_dao.TaskDAO.get(self.task_id)
            self._emit({"type": "cancelled" if cancelled else "done",
                        "task_id": self.task_id, "status": status,
                        "counts": (summary or {}).get("result_summary") or {},
                        "balance": credits_service.get_balance(self.user_id)})
        except Exception as e:  # noqa: BLE001 收尾兜底：失败全额退 + 任务落 failed
            logger.exception("生成任务失败 task_id=%s", self.task_id)
            try:
                refunded = self._refund()
                tasks_service.finalize_task(self.user_id, self.task_id, "failed",
                                            credit_cost=self.cost, credit_refunded=refunded,
                                            result=None)
            except Exception:  # noqa: BLE001
                logger.exception("失败收尾（退款/落库）异常 task_id=%s", self.task_id)
            self._emit({"type": "failed", "task_id": self.task_id,
                        "error": f"{type(e).__name__}: {str(e)[:200]}",
                        "balance": _safe_balance(self.user_id)})
        finally:
            self.events.put(_SENTINEL)
            unregister_worker(self.user_id, self)

    def _refund(self) -> int:
        """取消 / 失败全额退预扣；refund 自带幂等守卫，返回实际退款额。"""
        _, ok = credits_service.refund(self.user_id, self.task_id, self.cost)
        return self.cost if ok else 0


def _safe_balance(user_id: int) -> int:
    try:
        return credits_service.get_balance(user_id)
    except Exception:  # noqa: BLE001
        return -1


# ---------------------------------------------------------------- 每用户单实例注册表
_WORKERS: dict[int, GenerationWorker] = {}
_registry_lock = threading.Lock()


def register_worker(user_id: int, worker: GenerationWorker) -> None:
    with _registry_lock:
        _WORKERS[int(user_id)] = worker


def unregister_worker(user_id: int, worker: GenerationWorker) -> None:
    with _registry_lock:
        if _WORKERS.get(int(user_id)) is worker:
            _WORKERS.pop(int(user_id), None)


def get_worker(user_id: int) -> GenerationWorker | None:
    with _registry_lock:
        return _WORKERS.get(int(user_id))


def has_active_worker(user_id: int) -> bool:
    worker = get_worker(user_id)
    return worker is not None and worker.alive()


def estimate_cost(languages: list) -> int:
    """实时生成费用：语种数 × 每语种积分（ARCHITECTURE_V2.md 锁定参数）。"""
    return len(languages) * pricing.PER_LANGUAGE


def check_insufficient(user_id: int, cost: int) -> None:
    """余额校验（不扣款）；不足抛 InsufficientCredits。"""
    balance = credits_service.get_balance(user_id)
    if balance < cost:
        raise InsufficientCredits(f"积分不足：本次需 {cost}，当前余额 {balance}")
