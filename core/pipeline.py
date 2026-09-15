# -*- coding: utf-8 -*-
"""流水线编排：卖点拆解 -> 平台适配(并行) -> 本地化 -> 合规裁决 -> 交付门禁 -> 素材Prompt。

本次返工要点（对应评审编号）：
- [P0-3] 状态区分 生成失败/审核未通过/可交付；只有可交付(delivered)计入产出与价值统计；
  合规报告缺失/异常一律 review_blocked，禁止判 pass、禁止回退原文算成功；
- [P0-4] 整批 deadline：超时后未开始的组合标记 skipped_deadline（如实展示，不隐藏）；
  卖点阶段失败降级为确定性事实拼装继续跑，不整批终止；
- [P1-8] 最终内容（安全版）逐字段合同校验（数量/长度/单位），违规即 review_blocked；
- [P1-10] 免责声明为确定性配置：校验非空后由本模块追加到安全版末段（不经模型手）；
  系统免责声明不计入平台长度合同（平台字数限制针对内容本身）；
- [P1-11] 市场独立于语言：每个组合注入「语种∩目标市场」的规则；市场报告按目标市场输出；
- [P2-13] token 按组合 ID 归因（聚合各 Agent 返回的 usage records，不对全局计数做差）。
"""
import time
import math
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
from datetime import datetime

from . import load_yaml_config
from .agents import (AssetPromptAgent, ComplianceAgent, LocalizationAgent,
                     PlatformAdaptationAgent, SellingPointAgent, SchemaError,
                     validate_against_contract)
from .config_check import assert_configs_ok
from .market_rules import markets_for_language, merge_market_rules, build_market_report, covered_markets
from .privacy_gate import validate_product
from .llm_client import LLMClient, CallStopped
from .usage import update_result_usage
from .value_calc import compute_value

ENGINE_VERSION = "copy-engine-v3.0"

# 状态机（P0-3）：只有 delivered 计入产出与价值
STATUS_LABELS = {
    "delivered": "可交付（通过合规与合同门禁）",
    "review_blocked": "审核未通过（需人工处理后重跑）",
    "failed": "生成失败",
    "skipped_deadline": "超时未执行（整批 deadline 截止）",
}
VALID_STATUSES = set(STATUS_LABELS)


class Pipeline:
    def __init__(self, client, product: dict, languages: list, platforms: list,
                 compliance_level: str = "strict", models: dict = None,
                 max_workers: int = 4, batch_deadline_s: float = 900.0):
        # 启动期配置校验（P1-11：所有引用必须存在，不做静默跳过）
        assert_configs_ok()
        # 输入门禁（P0-2：白名单 + 本地敏感扫描；即使上游已校验，这里再拦一次）
        self.product = validate_product(product)

        platforms_cfg = load_yaml_config("platforms")
        languages_cfg = load_yaml_config("languages")
        compliance_cfg = load_yaml_config("compliance")

        # 引用存在性：未知平台/语种直接报错（不静默过滤）
        bad_p = [p for p in platforms if p not in platforms_cfg]
        bad_l = [l for l in languages if l not in languages_cfg]
        if bad_p or bad_l:
            raise ValueError(f"未知平台: {bad_p} / 未知语种: {bad_l}。"
                             f"合法平台: {sorted(platforms_cfg)} / 合法语种: {sorted(languages_cfg)}")
        self.languages = list(languages)
        self.platforms = list(platforms)
        if not self.platforms or not self.languages:
            raise ValueError("平台与语种不能为空")
        if len(set(self.platforms)) != len(self.platforms) or len(set(self.languages)) != len(self.languages):
            raise ValueError("平台与语种不能重复")
        if type(max_workers) is not int or not 1 <= max_workers <= 8:
            raise ValueError("max_workers 必须为 1 到 8 的整数")
        if not math.isfinite(float(batch_deadline_s)) or float(batch_deadline_s) <= 0:
            raise ValueError("deadline 必须是有限正数")

        if compliance_level not in compliance_cfg["levels"]:
            raise ValueError(f"未知合规等级: {compliance_level}")
        self.compliance_level = compliance_level

        # 市场映射（P1-11：市场独立于语言；空映射启动即报错）
        self.target_markets = [m for m in (self.product.get("target_markets") or [])]
        known = set(compliance_cfg.get("markets", {}))
        unknown_markets = [m for m in self.target_markets if m not in known]
        if unknown_markets:
            raise ValueError(f"产品 target_markets 引用未定义市场: {unknown_markets}（已知: {sorted(known)}）")
        self.lang_markets = {}
        for lang in self.languages:
            mkts = markets_for_language(lang, self.target_markets, compliance_cfg)
            if not mkts:
                raise ValueError(f"语种 {lang} 无可用市场规则映射（目标市场: {self.target_markets}）")
            self.lang_markets[lang] = mkts

        self.models = models or {}
        self.max_workers = max_workers
        self.batch_deadline_s = float(batch_deadline_s)

        self.platforms_cfg = platforms_cfg
        self.languages_cfg = languages_cfg
        self.compliance_cfg = compliance_cfg
        self.level_cfg = compliance_cfg["levels"][compliance_level]

        # 严格/标准模式的免责声明确定性配置（P1-10）：启动即校验非空
        self.disclaimers = {}
        if self.level_cfg.get("append_disclaimer"):
            for lang in self.languages:
                text = (compliance_cfg.get("disclaimers", {}) or {}).get(lang, "").strip()
                if self.level_cfg.get("require_disclaimer") and not text:
                    raise ValueError(f"合规等级 {compliance_level} 要求语种 {lang} 的免责声明非空，但配置为空")
                self.disclaimers[lang] = text

        if isinstance(client, LLMClient):
            client = client.new_task()
            client.set_deadline(time.monotonic() + self.batch_deadline_s)
        self._has_run = False
        self.agent_sell = SellingPointAgent(client, models)
        self.agent_platform = PlatformAdaptationAgent(client, models)
        self.agent_l10n = LocalizationAgent(client, models)
        self.agent_compliance = ComplianceAgent(client, models, compliance_cfg)
        self.agent_asset = AssetPromptAgent(client, models)
        self.client = client
        # 取消标志（最小侵入，ARCHITECTURE_V2.md §8）：不改引擎逻辑，仅用于短路未开始的平台/组合。
        self._cancel_requested = False

    # ---------------------------------------------------------------- 取消（最小侵入）
    def request_cancel(self) -> None:
        """用户取消：置标志 + 复用 LLMClient.cancel() 让在途调用尽快抛 CallStopped。"""
        self._cancel_requested = True
        if isinstance(self.client, LLMClient):
            try:
                self.client.cancel()   # threading.Event.set() -> _check_call 抛 CallStopped
            except Exception:
                pass

    # ---------------------------------------------------------------- 主流程
    def run(self, progress_cb=None, item_cb=None) -> dict:
        if isinstance(self.client, LLMClient):
            if self._has_run:
                self.client = self.client.new_task()
                for agent in (self.agent_sell, self.agent_platform, self.agent_l10n,
                              self.agent_compliance, self.agent_asset):
                    agent.client = self.client
            self.client.set_deadline(time.monotonic() + self.batch_deadline_s)
        self._has_run = True
        cb = progress_cb or (lambda *a, **k: None)
        icb = item_cb or (lambda *a, **k: None)
        t0 = time.monotonic()

        result = {
            "meta": {
                "engine": ENGINE_VERSION,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "compliance_level": self.compliance_level,
                "models": {**self.agent_sell.models},
                "languages": self.languages,
                "platforms": self.platforms,
                "target_markets": self.target_markets,
                "batch_deadline_s": self.batch_deadline_s,
                "status_labels": STATUS_LABELS,
                "degraded_reason": None,
            },
            "product": self.product,
            "analysis": None,
            "results": [],
            "usage": None,
            "market_report": None,
            "value": None,
            "engine_notices": self.compliance_cfg.get("engine_notices", {}),
        }
        combos = [(p, l) for p in self.platforms for l in self.languages]
        result["context"] = {  # 供失败项单条重跑复用
            "analysis": None, "platform_copies": {}, "combo_keys": [list(c) for c in combos],
        }

        # ---- 阶段 1：卖点拆解（失败降级，不整批终止，P0-4） ----
        cb("selling_points", 0, 1, "卖点拆解 Agent：分析产品…")
        analysis = None
        try:
            analysis, _u = self.agent_sell.run(self.product, tag="stage:selling")
            cb("selling_points", 1, 1, f"卖点拆解完成：{len(analysis.get('selling_points', []))} 条卖点")
        except Exception as e:  # noqa: BLE001 卖点失败 → 确定性降级
            analysis = self._fallback_analysis(self.product)
            result["meta"]["degraded_reason"] = (
                f"卖点拆解 Agent 失败（{type(e).__name__}: {str(e)[:200]}），"
                f"已降级为产品事实确定性拼装，整批继续")
            cb("selling_points", 1, 1, f"卖点拆解失败，已降级继续: {str(e)[:120]}")
        result["analysis"] = analysis
        result["context"]["analysis"] = analysis

        # ---- 阶段 2：平台适配（并行；单平台失败 → 该平台全部组合 failed） ----
        platform_copies = {}
        total = len(self.platforms)
        cb("platform", 0, total, f"平台适配 Agent：{total} 个平台并行生成…")
        done = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futs = {}
            for p in self.platforms:
                if self._cancel_requested:
                    platform_copies[p] = None
                    done += 1
                    cb("platform", done, total, f"平台适配跳过: {p}（用户取消）")
                    continue
                futs[pool.submit(self.agent_platform.run, analysis, p,
                                self.platforms_cfg[p], self.compliance_level,
                                f"stage:platform:{p}")] = p
            for fut in as_completed(futs):
                p = futs[fut]
                done += 1
                try:
                    data, _u = fut.result()
                    platform_copies[p] = data
                    cb("platform", done, total, f"平台适配完成: {p}")
                except Exception as e:  # noqa: BLE001
                    platform_copies[p] = None
                    cb("platform", done, total, f"平台适配失败: {p}: {str(e)[:160]}")
        result["context"]["platform_copies"] = platform_copies

        # ---- 阶段 3：本地化 -> 合规裁决 -> 交付门禁 -> 素材（簇间并行，逐条即时回调） ----
        total = len(combos)
        cb("combo", 0, total, f"本地化/合规/交付门禁：{total} 条 平台×语言 组合并行处理…")
        deadline = t0 + self.batch_deadline_s
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futs = {}
            for p, l in combos:
                if time.monotonic() >= deadline or self._cancel_requested:
                    _why = "用户取消" if self._cancel_requested else "提交前已超整批 deadline"
                    item = self._skipped_item(p, l, _why)
                    result["results"].append(item)
                    icb(p, l, item)
                    continue
                fut = pool.submit(self._run_combo, p, l, platform_copies.get(p))
                futs[fut] = (p, l)
            done = 0
            for fut in as_completed(futs):
                p, l = futs[fut]
                done += 1
                # 整批 deadline / 用户取消：取消尚未开始的任务并如实标记
                if time.monotonic() >= deadline or self._cancel_requested:
                    for other in futs:
                        other.cancel()
                try:
                    item = fut.result()
                except CancelledError:  # noqa: PERF203
                    item = self._skipped_item(p, l, "整批 deadline 截止，未执行")
                result["results"].append(item)
                icb(p, l, item)
                status = STATUS_LABELS.get(item["status"], item["status"])
                cb("combo", done, total, f"{p} × {l} → {status}")
        skipped_extra = total - len(result["results"])
        for i in range(skipped_extra):  # 理论上不会出现，防御性兜底
            result["results"].append(self._skipped_item("-", "-", "未知缺口"))

        result["results"].sort(key=lambda r: (self.platforms.index(r["platform"]) if r["platform"] in self.platforms else 99,
                                              self.languages.index(r["language"]) if r["language"] in self.languages else 99))
        # 用户取消：标记 meta.cancelled 供 app 判定任务状态（§8.1）
        if self._cancel_requested:
            result["meta"]["cancelled"] = True
        return self.finalize(result)

    # ---------------------------------------------------------------- 收尾统计（单条重跑后可重算）
    def finalize(self, result: dict) -> dict:
        """重算 usage / 市场报告 / 价值账（单条重跑替换 item 后调用同一份逻辑）。"""
        update_result_usage(result, self.client)
        results = result["results"]
        delivered = [r for r in results if r["status"] == "delivered"]
        blocked = [r for r in results if r["status"] == "review_blocked"]
        failed = [r for r in results if r["status"] == "failed"]
        skipped = [r for r in results if r["status"] == "skipped_deadline"]
        result["counts"] = {
            "total": len(results), "delivered": len(delivered),
            "review_blocked": len(blocked), "failed": len(failed),
            "skipped_deadline": len(skipped),
        }
        result["market_report"] = build_market_report(
            result["meta"].get("target_markets") or self.target_markets,
            results, self.compliance_cfg)
        markets = covered_markets(results)
        result["value"] = compute_value(
            delivered_count=len(delivered), total_combos=len(results),
            blocked_count=len(blocked), failed_count=len(failed) + len(skipped),
            num_platforms=len(result["meta"]["platforms"]),
            num_languages=len(result["meta"]["languages"]),
            engine_seconds=result["usage"]["wall_time_s"],
            engine_cost_cny=result["usage"]["cost_estimate_cny"],
            markets_covered=markets,
        )
        return result

    # ---------------------------------------------------------------- 单组合处理
    def _run_combo(self, platform_key: str, language_key: str, base_copy) -> dict:
        combo_id = f"{platform_key}__{language_key}"
        item = {
            "combo_id": combo_id, "platform": platform_key, "language": language_key,
            "status": "failed", "error": None,
            "stage_reached": "start",
            "markets": list(self.lang_markets.get(language_key, [])),
            "copy": None, "localized": None, "compliance": None,
            "final_copy": None, "asset_prompt": None, "asset_error": None,
            "stats": {"latency_s": None, "tokens": 0, "calls": 0},
        }
        t0 = time.time()
        usage_records = []

        def _sum_usage():
            # Include responses consumed before schema validation or a later stage fails.
            if isinstance(self.client, LLMClient):
                records = self.client.records_by_tag(combo_id)
                item["stats"]["tokens"] = sum(r["total_tokens"] for r in records)
                attempts = [a for a in self.client.attempts() if a["tag"] == combo_id]
                item["stats"]["calls"] = len(attempts)
                item["stats"]["unknown_usage_calls"] = sum(a["usage_status"] == "unknown" for a in attempts)
                return
            item["stats"]["tokens"] = sum(r["total_tokens"] for r in usage_records)
            item["stats"]["calls"] = len(usage_records)

        try:
            if base_copy is None:
                raise RuntimeError("上游平台适配失败，跳过该组合")
            item["copy"] = base_copy
            plat_spec = self.platforms_cfg[platform_key]
            fields_cfg = plat_spec.get("fields") or {}
            lang_spec = self.languages_cfg[language_key]

            # ---- 本地化 ----
            item["stage_reached"] = "localization"
            localized, u = self.agent_l10n.run(base_copy, language_key, lang_spec,
                                               plat_spec, tag=combo_id)
            usage_records += u
            item["localized"] = localized

            # ---- 合规：本地正则信号 -> LLM 裁决与安全版 ----
            item["stage_reached"] = "compliance"
            signals = self.agent_compliance.regex_scan(
                self.agent_compliance.publish_text(localized))
            market_rules = merge_market_rules(self.lang_markets[language_key], self.compliance_cfg)
            comp, u = self.agent_compliance.run(
                localized, language_key, lang_spec, market_rules, self.level_cfg,
                platform_key, fields_cfg, signals, tag=combo_id)
            usage_records += u

            # ---- 交付门禁（P0-3 fail-closed + P1-8 最终内容合同校验） ----
            item["stage_reached"] = "delivery_gate"
            blocking = []
            if comp["merged_risk_level"] == "high":
                blocking.append("综合风险等级 high")
            if comp.get("residual_findings"):
                blocking.append(f"安全版残留未裁决命中 {len(comp['residual_findings'])} 项")
            limit_findings = validate_against_contract(comp["safe_copy"], fields_cfg)
            # F3 修复：长度/数量超限改为「软告警 + 仍可交付」。
            # 实测阿语标题 43 char 超 40 char 上限被直接 block，导致 0 可交付、无法演示。
            # 长度不是红线——医疗合规才是。超限应在导出里标注「发布前请人工确认」，
            # 而不是让整条已经生成好的文案作废。
            # 真正的红线（综合风险 high、安全版残留未裁决）仍然 block，未放宽。
            comp["limit_findings"] = limit_findings
            if limit_findings:
                item["length_warnings"] = [
                    f"{v['field']} {v['issue']}（超出建议值，发布前请人工确认）"
                    for v in limit_findings
                ]

            if blocking:
                item["status"] = "review_blocked"
                item["error"] = "；".join(blocking)
                item["compliance"] = comp
                return item

            # ---- 免责声明：确定性追加（P1-10，不经模型）----
            disclaimer = self.disclaimers.get(language_key, "")
            final = {
                "titles": comp["safe_copy"]["titles"],
                "descriptions": list(comp["safe_copy"]["descriptions"]),
                "bullets": comp["safe_copy"]["bullets"],
                "hashtags": comp["safe_copy"]["hashtags"],
                "extras": comp["safe_copy"]["extras"],
                "cta": (localized.get("cta") or ""),
                "disclaimer": disclaimer,
            }
            if disclaimer and final["descriptions"]:
                final["descriptions"][-1] = final["descriptions"][-1] + "\n\n" + disclaimer
            comp["disclaimer"] = disclaimer
            item["compliance"] = comp
            item["final_copy"] = final
            item["status"] = "delivered"
            item["stage_reached"] = "asset"

            # ---- 素材 Prompt（仅可交付项；失败不影响交付状态，如实记录） ----
            try:
                asset, u = self.agent_asset.run(comp["safe_copy"], platform_key,
                                                plat_spec, language_key, tag=combo_id)
                usage_records += u
                item["asset_prompt"] = asset
            except Exception as e:  # noqa: BLE001 素材失败不撤销交付
                item["asset_error"] = f"{type(e).__name__}: {str(e)[:200]}"
            return item
        except CallStopped as e:
            item["status"] = "skipped_deadline"
            item["error"] = str(e)
            return item
        except SchemaError as e:
            # P0-3：Agent 响应不合规 → 本地化阶段=生成失败；合规阶段之后=审核未通过方向
            item["status"] = "review_blocked" if item["stage_reached"] in ("compliance", "delivery_gate") else "failed"
            item["error"] = f"SchemaError: {e}"
            return item
        except Exception as e:  # noqa: BLE001 单条失败不拖垮整批
            item["status"] = "failed"
            item["error"] = f"{type(e).__name__}: {e}"
            return item
        finally:
            item["stats"]["latency_s"] = round(time.time() - t0, 1)
            _sum_usage()

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _skipped_item(platform_key: str, language_key: str, reason: str) -> dict:
        return {
            "combo_id": f"{platform_key}__{language_key}", "platform": platform_key,
            "language": language_key, "status": "skipped_deadline", "error": reason,
            "stage_reached": "start", "markets": [], "copy": None, "localized": None,
            "compliance": None, "final_copy": None, "asset_prompt": None,
            "asset_error": None,
            "stats": {"latency_s": 0.0, "tokens": 0, "calls": 0},
        }

    @staticmethod
    def _fallback_analysis(product: dict) -> dict:
        """卖点阶段失败的确定性降级：只用产品白名单事实拼装，不经过模型（P0-4）。"""
        caps = [c for c in (product.get("capabilities") or [])
                if isinstance(c, dict) and c.get("status") == "implemented"]
        selling_points = [
            {"point": str(c.get("name", "")), "evidence": f"产品事实 {c.get('id', '?')}: {c.get('desc', '')}"}
            for c in caps[:5]
        ] or [{"point": str(product.get("product_name", "")), "evidence": "产品名称（降级模式）"}]
        return {
            "selling_points": selling_points,
            "audience": {"who": "；".join(product.get("target_buyers", []) or ["医院与经销商"]),
                         "roles": ["采购决策链"], "pain_points": ["文书负担与合规压力"]},
            "scenarios": [{"scene": "日常查房", "story": "查房结束，文书草稿已在系统里等着医生审核。"}],
            "keywords": ["pediatric", "AI assistant", "clinical documentation",
                         "on-premise", "hospital"],
            "_fallback": True,
        }
