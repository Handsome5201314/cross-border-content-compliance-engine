# -*- coding: utf-8 -*-
"""Bounded provider attempts and task-scoped diagnostics; never a billing ledger."""
import copy
import math
import re
import threading
import time
import uuid

from openai import OpenAI, DefaultHttpxClient

from . import load_env
from .privacy_gate import assert_text_clean
from .image_io import decode_image, download_image
from .output_io import atomic_write
from .gateway_policy import gateway_origin

# ---------------- 默认模型路由（可被上层覆盖） ----------------
MODEL_ROLES = {
    "main": "qwen3.7-plus",    # 主力：卖点拆解 / 平台适配
    "fast": "qwen3.6-flash",   # 快速：多语言本地化 / 素材 Prompt
    # 合规审查：换厂商交叉审，降低同源盲区。
    # F2 修复：原用 glm-5.2，实测在审查长文案时 90s 超时导致整条判失败；
    # 改用 deepseek-v4-flash（实测 1.4s，仍为跨厂商，保留交叉审价值）。
    "review": "deepseek-v4-flash",
    "image": "qwen-image-2.0", # 生图（仅 --with-image / UI 显式按钮时调用）
}

# 图像模型选项（仅显式生图时使用；不自动跨模型重发）
IMAGE_MODEL_CANDIDATES = ["qwen-image-2.0", "wan2.7-image", "qwen-image-2.0-pro", "wan2.7-image-pro"]

# ---------------- 成本估算表（元/百万 token，混合输入输出计价的估算值） ----------------
# 说明：比赛网关未公布分档价格，此表为可调估算值（不是实际成本），README 有说明。
PRICE_CNY_PER_MTOK = {
    "qwen3.6-flash": 1.0,
    "deepseek-v4-flash": 1.0,
    "qwen3.7-plus": 2.0,
    "qwen3.6-plus": 2.0,
    "glm-5.2": 2.0,
    "MiniMax-M2.5": 2.0,
    "kimi-k2.7-code": 4.0,
    "qwen3.7-max": 6.0,
    "qwen3.8-max": 8.0,
    "deepseek-v4-pro": 4.0,
}
DEFAULT_PRICE_CNY_PER_MTOK = 4.0

_QWEN_THINKING_MODEL_RE = re.compile(r"^qwen3\.\d")

class CallStopped(RuntimeError):
    """No new external call may start after cancellation or deadline."""


def _validate_model(model):
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model):
        raise ValueError("模型名必须是 1–128 位 ASCII 模型标识符")
    assert_text_clean(model, context="模型标识符")


def _param_rejected(err, parameter="enable_thinking"):
    # Only an explicit parameter rejection can authorize a compatibility retry.
    return (getattr(err, "status_code", None) == 400
            and parameter in str(err).lower()
            and any(word in str(err).lower() for word in
                    ("unknown", "unsupported", "not supported", "invalid", "unrecognized", "extra_forbidden")))


def _usage_values(usage):
    usage = usage if isinstance(usage, dict) else {}
    values = {key: usage.get(key) for key in
              ("prompt_tokens", "completion_tokens", "total_tokens")}
    if (any(type(v) is not int or v < 0 for v in values.values())
            or values["total_tokens"] != values["prompt_tokens"] + values["completion_tokens"]):
        raise RuntimeError("模型用量缺失或不合法，费用待核对")
    return values


class LLMClient:
    def __init__(self, base_url=None, api_key=None, timeout=90.0, *, allow_env_file=True):
        # Never pair a user supplied URL with the platform's file/env credential.
        if (base_url is None) != (api_key is None):
            raise RuntimeError("自定义网关和凭证必须同时提供")
        env = load_env(allow_file=allow_env_file) if base_url is None else {}
        self.base_url = (env.get("HACKATHON_BASE_URL", "") if base_url is None else base_url).strip()
        self.api_key = (env.get("HACKATHON_API_KEY", "") if api_key is None else api_key).strip()
        if not self.api_key or not self.base_url:
            raise RuntimeError("缺少 HACKATHON_API_KEY / HACKATHON_BASE_URL")
        try:
            gateway_origin(self.base_url)
        except ValueError:
            raise RuntimeError("模型网关 URL 不合法，必须是无内嵌凭证的 HTTPS 地址") from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.timeout = timeout
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                             timeout=timeout, max_retries=0,
                             http_client=DefaultHttpxClient(follow_redirects=False))
        self._init_task()

    def _init_task(self):
        self.task_id = uuid.uuid4().hex
        self._lock = threading.Lock()
        self.usage_records = []
        self.attempt_records = []
        self._t_start = time.monotonic()
        self._deadline = None
        self._cancelled = threading.Event()

    def new_task(self):
        """Separate counters/control state; reuse the thread-safe SDK transport only."""
        task = copy.copy(self)
        task._init_task()
        return task

    def set_deadline(self, deadline):
        if not math.isfinite(deadline):
            raise ValueError("deadline must be finite")
        self._deadline = deadline

    def cancel(self):
        self._cancelled.set()

    def _check_call(self):
        if self._cancelled.is_set() or (self._deadline is not None and time.monotonic() >= self._deadline):
            raise CallStopped("任务已取消或到达截止时间；在途费用仍需核对")

    def _request_timeout(self):
        self._check_call()
        return min(self.timeout, max(0.001, self._deadline - time.monotonic())) if self._deadline else self.timeout

    def _begin(self, model, label, tag, kind):
        self._check_call()
        event = {"task_id": self.task_id, "attempt_id": uuid.uuid4().hex, "model": model,
                 "label": label or model, "tag": tag or label or model, "kind": kind,
                 "status": "in_flight", "usage_status": "unknown"}
        with self._lock:
            self.attempt_records.append(event)
        return event

    def _finish(self, event, **fields):
        with self._lock:
            event.update(fields)

    def attempts(self):
        with self._lock:
            return [dict(r) for r in self.attempt_records]

    def chat(self, model, messages, json_mode=False, temperature=0.7,
             max_tokens=None, label="", tag=""):
        _validate_model(model)
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError("temperature 必须为 0 到 2 的有限数值")
        # The current engine is text-only. Reject alternate representations instead of bypassing the gate.
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a nonempty text message list")
        for msg in messages:
            if (not isinstance(msg, dict) or set(msg) - {"role", "content"}
                    or msg.get("role") not in {"system", "user", "assistant", "developer"}
                    or not isinstance(msg.get("content"), str)):
                raise ValueError("only role/content text messages are supported")
            assert_text_clean(msg["content"], context="出站消息")
        limit = 8192 if max_tokens is None else max_tokens
        if type(limit) is not int or not 1 <= limit <= 32768:
            raise ValueError("max_tokens must be an integer from 1 to 32768")
        kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=limit)
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        use_extra = bool(_QWEN_THINKING_MODEL_RE.match(model))
        for attempt in range(2):
            timeout = self._request_timeout()
            event = self._begin(model, label, tag, "chat")
            started = time.monotonic()
            try:
                raw = self._create({**kwargs, "timeout": timeout}, use_extra)
            except Exception as error:
                status = getattr(error, "status_code", None)
                rejected = status in {400, 401, 403, 404, 422, 429}
                self._finish(event, status="failed", http_status=status,
                             usage_status="rejected" if rejected else "unknown",
                             latency_s=round(time.monotonic() - started, 2))
                if attempt == 0 and use_extra and _param_rejected(error):
                    use_extra = False
                    continue
                # Ambiguous timeouts/5xx are not automatically replayed: they may have incurred cost.
                if attempt == 0 and status == 429:
                    self._cancelled.wait(1.0)
                    continue
                raise RuntimeError(f"模型调用失败；HTTP={status or 'unknown'}；attempt={event['attempt_id']}；用量={event['usage_status']}") from None
            try:
                # Inspect JSON before the SDK coerces strings/bools into integer token fields.
                values = _usage_values(raw.http_response.json().get("usage"))
            except (RuntimeError, ValueError, AttributeError):
                self._finish(event, status="response_received", usage_status="unknown")
                raise RuntimeError(f"模型用量缺失或不合法，费用待核对；attempt={event['attempt_id']}") from None
            record = {**values, "task_id": self.task_id, "attempt_id": event["attempt_id"],
                      "model": model, "label": label or model, "tag": tag or label or model,
                      "latency_s": round(time.monotonic() - started, 2)}
            with self._lock:
                self.usage_records.append(record)
                event.update(status="response_received", usage_status="known", **values)
            # Preserve known consumption even if the response is unusable.
            try:
                resp = raw.parse()
            except Exception:
                raise RuntimeError("模型响应格式不合法；已记录用量") from None
            if not resp.choices or not isinstance(resp.choices[0].message.content, str):
                raise RuntimeError("模型响应缺少文本；已记录用量")
            return resp.choices[0].message.content, record

    def _create(self, kwargs, use_extra):
        if use_extra:
            kwargs = {**kwargs, "extra_body": {"enable_thinking": False}}
        return self.client.chat.completions.with_raw_response.create(**kwargs)

    def records_by_tag(self, tag):
        with self._lock:
            return [dict(r) for r in self.usage_records if r.get("tag") == tag]

    def usage_summary(self):
        with self._lock:
            records = [dict(r) for r in self.usage_records]
            attempts = [dict(r) for r in self.attempt_records]
        by_model, by_tag = {}, {}
        for record in records:
            m = by_model.setdefault(record["model"], {"calls": 0, "total_tokens": 0,
                                                      "prompt_tokens": 0, "completion_tokens": 0})
            m["calls"] += 1
            for key in ("total_tokens", "prompt_tokens", "completion_tokens"):
                m[key] += record[key]
            t = by_tag.setdefault(record["tag"], {"calls": 0, "total_tokens": 0})
            t["calls"] += 1
            t["total_tokens"] += record["total_tokens"]
        unknown = sum(a["usage_status"] == "unknown" for a in attempts)
        return {
            "task_id": self.task_id,
            "total_calls": len(records),  # compatibility: successfully metered chat responses
            "attempted_calls": len(attempts), "unknown_usage_calls": unknown,
            "usage_complete": unknown == 0, "billing_ready": False,
            **{key: sum(r[key] for r in records) for key in
               ("total_tokens", "prompt_tokens", "completion_tokens")},
            "by_model": by_model, "by_tag": by_tag,
            "cost_estimate_cny": round(sum(m["total_tokens"] / 1_000_000 *
                PRICE_CNY_PER_MTOK.get(model, DEFAULT_PRICE_CNY_PER_MTOK)
                for model, m in by_model.items()), 4),
            "wall_time_s": round(time.monotonic() - self._t_start, 1),
            "price_note": ("仅已知文本 token 的混合单价估算，非账单；未知用量/图片费用未计入。"
                           + ("存在待核对费用。" if unknown else "")),
        }

    def generate_image(self, prompt, out_path, model=None, size="1024*1024"):
        assert_text_clean(prompt, context="生图 Prompt")
        model = IMAGE_MODEL_CANDIDATES[0] if model is None else model
        _validate_model(model)
        if not isinstance(size, str) or not re.fullmatch(r"[1-9][0-9]{1,3}[*x-][1-9][0-9]{1,3}", size):
            raise ValueError("图片尺寸必须是数字宽高")
        sizes = list(dict.fromkeys([size, size.replace("*", "x")]))
        event = None
        for index, candidate_size in enumerate(sizes):
            try:
                timeout = self._request_timeout()
                event = self._begin(model, "image", "image", "image")
                resp = self.client.images.generate(model=model, prompt=prompt, n=1,
                                                   size=candidate_size, timeout=timeout)
            except CallStopped:
                return {"ok": False, "model": model, "path": None, "error": "任务已停止"}
            except Exception as error:
                status = getattr(error, "status_code", None)
                self._finish(event, status="failed", http_status=status,
                             usage_status="rejected" if status in {400, 401, 403, 404, 422, 429} else "unknown")
                if index == 0 and _param_rejected(error, "size"):
                    continue
                return {"ok": False, "model": model, "path": None,
                        "error": f"图像请求失败；HTTP={status or 'unknown'}；attempt={event['attempt_id']}"}
            # A returned image may already be billed. Download/validation/publish failure NEVER regenerates it.
            self._finish(event, status="response_received", usage_status="unknown")
            try:
                self._check_call()
                item = resp.data[0]
                data = decode_image(item.b64_json) if getattr(item, "b64_json", None) else download_image(item.url)
                atomic_write(out_path, data)
                self._finish(event, artifact_status="published")
                return {"ok": True, "model": model, "path": str(out_path), "error": None}
            except Exception:
                self._finish(event, artifact_status="failed")
                return {"ok": False, "model": model, "path": None,
                        "error": f"图片下载、校验或写入失败；生成可能已收费，不自动重新生成；attempt={event['attempt_id']}"}
        return {"ok": False, "model": model, "path": None, "error": "图片参数不受支持"}


def mask_key(key):
    return "已配置（不显示凭证）" if key else "未配置"
