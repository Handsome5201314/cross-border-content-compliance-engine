# -*- coding: utf-8 -*-
"""统一模型路由客户端。

对应评审返工项：
- P0-2：所有出站消息先过本地隐私门禁（privacy_gate），命中患者标识即拒绝发送，
  绝不用云端模型脱敏；
- P0-4：重试只有一层且按错误分类——鉴权/无效模型/参数等永久错误立即失败，
  超时/限流/5xx 仅重试 1 次（合计最多 2 次请求）；SDK 内置重试关闭（max_retries=0），
  不再出现"外层 3 次 × SDK 2 次"的放大；
- P2-13：每次调用带 tag（组合 ID / 阶段名），用量按 tag 归因，不对全局计数做差。

其余职责保持：凭证只从项目根 .env 读；token 用量与成本统计（线程安全）；
qwen3.x 关闭思考模式（网关不识别参数时自动去掉重发一次，不计入重试）。
"""
import base64
import re
import threading
import time

from openai import OpenAI

from . import ENV_PATH, load_env
from .privacy_gate import assert_text_clean

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

# 图像模型候选（仅显式生图时使用；第一个失败自动降级尝试下一个）
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

# 永久错误：重试没有意义，立即失败（P0-4）
_PERMANENT_STATUS = {401, 403, 404}
_PERMANENT_MARKERS = (
    "invalid_api_key", "invalid api key", "incorrect api key",
    "authentication", "unauthorized", "permission denied",
    "model_not_found", "model not found", "does not exist",
    "insufficient_quota", "quota exceeded",
    "invalid_request_error", "invalid request",
)


def _is_permanent(err: Exception) -> bool:
    """判断是否永久性错误（鉴权/无效模型/配额/参数非法等，重试无意义）。"""
    status = getattr(err, "status_code", None)
    if status in _PERMANENT_STATUS:
        return True
    text = str(err).lower()
    return any(k in text for k in _PERMANENT_MARKERS)


class LLMClient:
    """线程安全的统一模型调用客户端（并行阶段会被多线程共享）。"""

    def __init__(self, base_url: str = None, api_key: str = None,
                 timeout: float = 90.0):
        env = load_env()
        self.base_url = base_url or env.get("HACKATHON_BASE_URL", "")
        self.api_key = api_key or env.get("HACKATHON_API_KEY", "")
        if not self.api_key or not self.base_url:
            raise RuntimeError(
                f"[FATAL] .env 缺少 HACKATHON_API_KEY / HACKATHON_BASE_URL（期望路径: {ENV_PATH}）"
            )
        self.timeout = timeout
        # SDK 内置重试关闭：重试策略完全由本类控制（P0-4）
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url,
                             timeout=timeout, max_retries=0)
        self._lock = threading.Lock()
        self.usage_records = []   # {model, prompt_tokens, completion_tokens, total_tokens, latency_s, label, tag}
        self._t_start = time.time()

    # ---------------- 核心调用 ----------------
    def chat(self, model: str, messages: list, json_mode: bool = False,
             temperature: float = 0.7, max_tokens: int = None,
             label: str = "", tag: str = "") -> tuple:
        """单次对话调用。返回 (content, usage_record)。

        - 出站隐私门禁：任何消息内容命中患者标识 → 本地抛 PrivacyViolation，不发送（P0-2）；
        - 重试：仅一层且分类——永久错误立即失败；瞬时错误（超时/限流/5xx/连接）重试 1 次。
        """
        # P0-2 隐私门禁（最后一道防线，所有调用路径统一覆盖）
        for msg in messages:
            content = (msg or {}).get("content", "") or ""
            if isinstance(content, str):
                assert_text_clean(content, context=f"出站消息[{label or model}]")

        max_attempts = 2
        last_err = None
        for attempt in range(1, max_attempts + 1):
            t0 = time.time()
            try:
                kwargs = dict(model=model, messages=messages, temperature=temperature)
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                if max_tokens:
                    kwargs["max_tokens"] = max_tokens

                use_extra = bool(_QWEN_THINKING_MODEL_RE.match(model))
                try:
                    resp = self._create(kwargs, use_extra)
                except Exception as e_inner:
                    # 网关不认 enable_thinking 参数时去掉重发一次（参数兼容回退，不计入重试）
                    if use_extra and _param_rejected(e_inner):
                        resp = self._create(kwargs, False)
                    else:
                        raise

                content = resp.choices[0].message.content or ""
                usage = getattr(resp, "usage", None)
                record = {
                    "model": model,
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                    "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
                    "total_tokens": getattr(usage, "total_tokens", 0) if usage else 0,
                    "latency_s": round(time.time() - t0, 2),
                    "label": label or model,
                    "tag": tag or label or model,
                }
                with self._lock:
                    self.usage_records.append(record)
                return content, record
            except Exception as e:  # noqa: BLE001
                last_err = e
                permanent = _is_permanent(e)
                if permanent or attempt >= max_attempts:
                    kind = "永久性错误（不重试）" if permanent else f"已重试 {max_attempts - 1} 次"
                    raise RuntimeError(f"模型调用失败（{kind}）: {model}: {last_err}") from e
                time.sleep(2.0)  # 瞬时错误退避后仅重试 1 次
        raise RuntimeError(f"模型调用失败: {model}: {last_err}")

    def _create(self, kwargs: dict, use_extra: bool):
        if use_extra:
            kwargs = dict(kwargs)
            kwargs["extra_body"] = {"enable_thinking": False}
        return self.client.chat.completions.create(**kwargs)

    # ---------------- 用量与成本 ----------------
    def records_by_tag(self, tag: str) -> list:
        """按 tag 取用量记录（P2-13：组合级归因用，不做全局差值）。"""
        with self._lock:
            return [r for r in self.usage_records if r.get("tag") == tag]

    def usage_summary(self) -> dict:
        """累计用量汇总：总 token、分模型、分 tag、成本估算、耗时。"""
        with self._lock:
            records = list(self.usage_records)
        by_model = {}
        by_tag = {}
        for r in records:
            m = by_model.setdefault(r["model"], {"calls": 0, "total_tokens": 0,
                                                 "prompt_tokens": 0, "completion_tokens": 0})
            m["calls"] += 1
            m["total_tokens"] += r["total_tokens"]
            m["prompt_tokens"] += r["prompt_tokens"]
            m["completion_tokens"] += r["completion_tokens"]
            t = by_tag.setdefault(r.get("tag") or r["label"], {"calls": 0, "total_tokens": 0})
            t["calls"] += 1
            t["total_tokens"] += r["total_tokens"]
        cost = 0.0
        for model, m in by_model.items():
            price = PRICE_CNY_PER_MTOK.get(model, DEFAULT_PRICE_CNY_PER_MTOK)
            cost += m["total_tokens"] / 1_000_000 * price
        return {
            "total_calls": len(records),
            "total_tokens": sum(r["total_tokens"] for r in records),
            "prompt_tokens": sum(r["prompt_tokens"] for r in records),
            "completion_tokens": sum(r["completion_tokens"] for r in records),
            "by_model": by_model,
            "by_tag": by_tag,
            "cost_estimate_cny": round(cost, 4),
            "wall_time_s": round(time.time() - self._t_start, 1),
            "price_note": "单价为可调估算值（元/百万token，混合输入输出），非实际成本，见 llm_client.PRICE_CNY_PER_MTOK",
        }

    # ---------------- 生图（仅显式开启时调用；失败如实上报） ----------------
    def generate_image(self, prompt: str, out_path, model: str = None,
                       size: str = "1024*1024") -> dict:
        """图像模型真实生成一张图。依次尝试候选模型；返回 {ok, model, path, error}，失败不抛异常。"""
        assert_text_clean(prompt, context="生图 Prompt")
        candidates = [model] if model else list(IMAGE_MODEL_CANDIDATES)
        last_err = None
        for m in candidates:
            for sz in (size, size.replace("*", "x"), size.replace("*", "-")):
                try:
                    resp = self.client.images.generate(model=m, prompt=prompt, n=1, size=sz)
                    item = resp.data[0]
                    if getattr(item, "b64_json", None):
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        out_path.write_bytes(base64.b64decode(item.b64_json))
                        return {"ok": True, "model": m, "path": str(out_path), "error": None}
                    if getattr(item, "url", None):
                        import urllib.request
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        with urllib.request.urlopen(item.url, timeout=60) as r, open(out_path, "wb") as f:
                            f.write(r.read())
                        return {"ok": True, "model": m, "path": str(out_path), "error": None}
                    last_err = f"{m}: 响应中无 b64_json 也无 url"
                except Exception as e:  # noqa: BLE001
                    last_err = f"{m}: {type(e).__name__}: {str(e)[:200]}"
        return {"ok": False, "model": None, "path": None, "error": last_err}


def _param_rejected(err: Exception) -> bool:
    """判断异常是否为'不识别 extra 参数'类错误（用于决定是否去掉 enable_thinking 重发）。"""
    text = str(err).lower()
    keys = ("enable_thinking", "extra_body", "invalid parameter", "unknown parameter",
            "extra_forbidden", "not supported")
    return any(k in text for k in keys)


def mask_key(key: str) -> str:
    """Key 脱敏显示：只显示前 10 位 + 长度。日志/打印统一走这里。"""
    return f"{key[:10]}...（已脱敏，长度 {len(key)}）"
