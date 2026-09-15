# -*- coding: utf-8 -*-
"""5 个 Agent：卖点拆解 / 平台适配 / 多语言本地化 / 合规审查 / 素材Prompt。

本次返工要点（对应评审编号）：
1. [P1-8] 结构化产物契约贯穿全程：平台适配与本地化统一输出
   titles[] / descriptions[] / bullets[] / hashtags[] / extras{}，
   长度校验有明确单位与计数规则，对最终内容逐字段校验，无容忍倍率；
2. [P0-3] 每个 Agent 的 JSON 响应有严格 schema 校验（SchemaError）：
   空报告/缺字段/枚举外的值一律判审核失败（review_failed 方向），禁止判 pass 或回退原文；
3. [P1-12] 正则命中一律是「待审信号」（source=regex_signal），由语义层逐条裁决
   （verdict: violation|benign）；否定语境（Not a cure / do not guarantee）判 benign
   且保留原文，不强制删除；未裁决信号按违规保守处理（fail-closed）；
4. [P1-9] 合规安全版覆盖全部发布字段：safe_titles/safe_descriptions/safe_bullets/
   safe_hashtags/safe_extras；残留扫描覆盖完整发布内容；
5. [P1-10] 免责声明不由模型生成：由流水线按配置确定性追加（见 pipeline.py）。
"""
import json
import re

from .privacy_gate import assert_text_clean

# 各平台素材图默认画幅（供素材 Prompt Agent 使用）
PLATFORM_ASPECT = {
    "tiktok": "9:16",
    "instagram": "4:5",
    "youtube_shorts": "9:16",
    "google_ads": "1.91:1 (1200x628)",
    "amazon": "1:1",
    "landing_page": "16:9",
}

# 长度单位与计数规则（P1-8：明确单位，逐字段校验）
UNIT_RULE_NOTE = "char=按 Unicode 字符计数(len)；word=按空白切分的词计数(split)"
RISK_ENUM = ("high", "medium", "low", "pass")
SEVERITY_ENUM = ("high", "medium", "low")


class SchemaError(ValueError):
    """Agent 响应 schema 校验失败：上游必须按 生成失败/审核失败 处理，不得回退原文。"""


# ================================================================ 产物契约校验（P1-8）
def count_units(text: str, unit: str) -> int:
    if not isinstance(text, str):
        return 0
    if unit == "word":
        return len(text.split())
    return len(text)  # char


def validate_against_contract(copy: dict, fields_cfg: dict) -> list:
    """对最终内容逐字段校验数量与长度（无容忍倍率）。返回违规列表，空列表 = 通过。"""
    out = []
    for field in ("titles", "descriptions", "bullets"):
        spec = fields_cfg.get(field) or {}
        expected = spec.get("count", 0)
        mx = spec.get("max", 0)
        unit = spec.get("unit", "char")
        items = copy.get(field) or []
        if not isinstance(items, list):
            items = []
        non_empty = [x for x in items if isinstance(x, str) and x.strip()]
        if expected and len(non_empty) != expected:
            out.append({"field": field, "issue": f"数量 {len(non_empty)} != 要求 {expected}"})
        for i, s in enumerate(items):
            if not isinstance(s, str) or not s.strip():
                out.append({"field": f"{field}[{i}]", "issue": "空值"})
                continue
            c = count_units(s, unit)
            if mx and c > mx:
                out.append({"field": f"{field}[{i}]",
                            "issue": f"长度 {c} {unit} 超上限 {mx} {unit}",
                            "count": c, "max": mx, "unit": unit})
    ht = copy.get("hashtags") or []
    if not isinstance(ht, list):
        ht = []
    hspec = fields_cfg.get("hashtags") or {}
    hmin, hmax = hspec.get("count_min", 0), hspec.get("count_max", 0)
    if len(ht) < hmin or len(ht) > hmax:
        out.append({"field": "hashtags", "issue": f"数量 {len(ht)} 不在允许区间 [{hmin},{hmax}]"})
    extras_spec = fields_cfg.get("extras") or {}
    extras = copy.get("extras") or {}
    if not isinstance(extras, dict):
        extras = {}
    for key, spec in extras_spec.items():
        val = extras.get(key)
        if spec.get("max_total_bytes") is not None:
            vals = val if isinstance(val, list) else ([val] if val else [])
            total = sum(len(str(v).encode("utf-8")) + 1 for v in vals)
            if total > spec["max_total_bytes"]:
                out.append({"field": f"extras.{key}",
                            "issue": f"总字节 {total} 超上限 {spec['max_total_bytes']} bytes"})
        elif spec.get("max") is not None and isinstance(val, str) and len(val) > spec["max"]:
            out.append({"field": f"extras.{key}",
                        "issue": f"长度 {len(val)} char 超上限 {spec['max']} char"})
    return out


def contract_prompt(fields_cfg: dict) -> str:
    """把平台字段合同渲染成提示词片段（给平台适配/本地化/合规三个 Agent 共用）。"""
    lines = []
    for field in ("titles", "descriptions", "bullets"):
        spec = fields_cfg.get(field) or {}
        n, mx, unit = spec.get("count", 0), spec.get("max", 0), spec.get("unit", "char")
        if n:
            lines.append(f"- {field}: 恰好 {n} 条，每条 ≤ {mx} {unit}（{UNIT_RULE_NOTE}）")
        else:
            lines.append(f"- {field}: 必须为空数组")
    hspec = fields_cfg.get("hashtags") or {}
    hmin, hmax = hspec.get("count_min", 0), hspec.get("count_max", 0)
    lines.append(f"- hashtags: {hmin}~{hmax} 个（超出区间即不合格）")
    for key, spec in (fields_cfg.get("extras") or {}).items():
        if spec.get("max_total_bytes") is not None:
            lines.append(f"- extras.{key}: 列表，总字节数 ≤ {spec['max_total_bytes']}")
        elif spec.get("max") is not None:
            lines.append(f"- extras.{key}: 字符串，长度 ≤ {spec['max']} char")
    return "\n".join(lines)


# ================================================================ 各 Agent 的 schema 校验（P0-3）
def _is_str_list(v, allow_empty=False) -> bool:
    if not isinstance(v, list):
        return False
    if not allow_empty and not v:
        return False
    return all(isinstance(x, str) and x.strip() for x in v)


def sanitize_str_list(v) -> list:
    """F1 修复：把任意值净化成「非空字符串数组」。

    原实现对 bullets/hashtags 用 _is_str_list 判死：只要混进一个 None、数字或空串，
    整条平台适配就抛 SchemaError，下游所有语种跟着全灭（实测 tiktok 即因此 0 交付）。
    字段合同应当「净化 > 判死」：丢掉非法元素即可，不该因一个杂项废掉整条产物。
    """
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        if x is None:
            continue
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def sanitize_copy_shape(d: dict) -> None:
    """就地净化产物字段，保证后续校验与导出拿到干净结构。"""
    if not isinstance(d, dict):
        return
    # 注意：字段「缺失」也要净化成空数组，否则 d.get(field) 为 None 会再次触发判死
    for field in ("titles", "descriptions", "bullets", "hashtags"):
        d[field] = sanitize_str_list(d.get(field))
    if not isinstance(d.get("extras"), dict):
        d["extras"] = {}
    cta = d.get("cta")
    if not isinstance(cta, str) or not cta.strip():
        # CTA 缺失不判死：给中性占位，避免空 CTA 废掉已生成的好文案
        d["cta"] = "Contact us"


def validate_analysis(d: dict) -> None:
    """卖点拆解 schema：缺字段/空值直接 SchemaError（整批降级由 pipeline 处理）。"""
    if not isinstance(d, dict):
        raise SchemaError("卖点拆解输出不是 JSON 对象")
    sp = d.get("selling_points")
    if not (isinstance(sp, list) and sp and all(
            isinstance(x, dict) and str(x.get("point", "")).strip()
            and str(x.get("evidence", "")).strip() for x in sp)):
        raise SchemaError("selling_points 缺失或为空（要求 3-5 条，每条含非空 point/evidence）")
    if not isinstance(d.get("audience"), dict) or not d["audience"].get("who"):
        raise SchemaError("audience.who 缺失")
    if not isinstance(d.get("scenarios"), list) or not d["scenarios"]:
        raise SchemaError("scenarios 缺失或为空")
    if not _is_str_list(d.get("keywords"), allow_empty=False):
        raise SchemaError("keywords 缺失或类型错误（要求字符串数组）")


def validate_copy_shape(d: dict, what: str) -> None:
    """平台适配/本地化产物契约的形状校验（类型与非空；数量与长度的硬校验只对最终安全版做）。"""
    if not isinstance(d, dict):
        raise SchemaError(f"{what} 输出不是 JSON 对象")
    sanitize_copy_shape(d)  # F1：先净化，再校验
    for field in ("titles", "descriptions"):
        if not _is_str_list(d.get(field)):
            raise SchemaError(f"{what}.{field} 缺失/为空/类型错误（要求非空字符串数组）")
    for field in ("bullets", "hashtags"):
        # F1：允许空数组，元素非法已在净化阶段丢弃，不再因类型判死
        if not isinstance(d.get(field), list):
            raise SchemaError(f"{what}.{field} 须为数组（允许空数组）")
    if not isinstance(d.get("extras"), dict):
        raise SchemaError(f"{what}.extras 缺失或不是对象（无则空对象）")
    if not isinstance(d.get("cta"), str) or not d.get("cta", "").strip():
        raise SchemaError(f"{what}.cta 缺失或为空")


def validate_localized(d: dict) -> None:
    validate_copy_shape(d, "本地化输出")
    if not isinstance(d.get("localization_notes"), str) or not d["localization_notes"].strip():
        raise SchemaError("本地化输出 localization_notes 缺失或为空")


def validate_compliance_response(d: dict) -> None:
    """合规审查响应 schema（P0-3 核心：空报告/缺字段/枚举外 → SchemaError → review_failed）。"""
    if not isinstance(d, dict):
        raise SchemaError("合规审查输出不是 JSON 对象")
    if d.get("risk_level") not in RISK_ENUM:
        raise SchemaError(f"合规 risk_level 非法或缺失（合法: {RISK_ENUM}）")
    verdicts = d.get("signal_verdicts")
    if not isinstance(verdicts, list):
        raise SchemaError("signal_verdicts 缺失或不是数组（即使无信号也须空数组）")
    for v in verdicts:
        if not isinstance(v, dict) or v.get("verdict") not in ("violation", "benign") \
                or not str(v.get("term", "")).strip() or not str(v.get("reason", "")).strip():
            raise SchemaError("signal_verdicts 项非法（须含 term/verdict∈violation|benign/reason）")
    findings = d.get("llm_findings")
    if not isinstance(findings, list):
        raise SchemaError("llm_findings 缺失或不是数组（即使无发现也须空数组）")
    for f in findings:
        if not isinstance(f, dict) or not str(f.get("term", "")).strip():
            raise SchemaError("llm_findings 项非法（须含非空 term）")
        if f.get("severity") not in SEVERITY_ENUM:
            raise SchemaError("llm_findings.severity 非法")
    for field in ("safe_titles", "safe_descriptions"):
        if not _is_str_list(d.get(field)):
            raise SchemaError(f"安全版 {field} 缺失/为空（审核失败方向，禁止回退原文）")
    for field in ("safe_bullets", "safe_hashtags"):
        d[field] = sanitize_str_list(d.get(field))  # F1：净化而非判死
        if not isinstance(d.get(field), list):
            raise SchemaError(f"安全版 {field} 须为数组（允许空数组）")
    if not isinstance(d.get("safe_extras"), dict):
        raise SchemaError("安全版 safe_extras 缺失或不是对象（无则空对象）")


# ================================================================ 合并逻辑（可独立回归测试，P0-3）
_SEV_RANK = {"high": 3, "medium": 2, "low": 1, "pass": 0}


def merge_compliance(llm_data: dict, signals: list) -> dict:
    """合并正则信号与语义裁决（fail-closed）。

    - llm_data 缺字段/枚举外 → SchemaError（上游判 review_failed，绝不判 pass）；
    - 信号未获裁决 → 按原 severity 保守计入（未裁决≠无害）；
    - verdict=violation → 计入 confirmed；verdict=benign → 保留原文，不定罪；
    - 模型自己的 risk_level 参与合并（模型判 high 就是 high，不允许被空 findings 冲淡）。
    """
    validate_compliance_response(llm_data)
    verdicts = {str(v["term"]).strip().lower(): v for v in llm_data["signal_verdicts"]}
    confirmed, unresolved, benign = [], [], []
    for s in signals or []:
        v = verdicts.get(str(s.get("term", "")).strip().lower())
        if v is None:
            unresolved.append({**s, "adjudication": "未裁决，按违规保守处理"})
        elif v["verdict"] == "violation":
            confirmed.append({**s, "adjudication": f"语义确认违规：{v['reason']}"})
        else:
            benign.append({"term": s.get("term"), "reason": v["reason"]})
    sevs = [_SEV_RANK.get(llm_data["risk_level"], 0)]
    sevs += [_SEV_RANK.get(f.get("severity"), 1) for f in llm_data["llm_findings"]]
    sevs += [_SEV_RANK.get(s.get("severity"), 1) for s in confirmed + unresolved]
    top = max(sevs)
    merged = "high" if top >= 3 else "medium" if top == 2 else "low" if top == 1 else "pass"
    return {
        "risk_level": llm_data["risk_level"],
        "merged_risk_level": merged,
        "signal_findings": signals or [],
        "signal_verdicts": llm_data["signal_verdicts"],
        "confirmed_findings": confirmed,
        "unresolved_signals": unresolved,
        "benign_verdicts": benign,
        "llm_findings": llm_data["llm_findings"],
    }


# ---------------------------------------------------------------- 基类
class BaseAgent:
    """LLM Agent 基类：JSON 调用 + 解析修复（一次）+ schema 校验。"""

    name = "base"

    def __init__(self, client, models: dict = None):
        self.client = client
        from .llm_client import MODEL_ROLES
        self.models = {**MODEL_ROLES, **(models or {})}

    def _chat_json(self, system: str, user: str, model_key: str = "main",
                   temperature: float = 0.5, label: str = "", tag: str = "") -> tuple:
        """调用并解析 JSON。返回 (data_dict, usage_records)。

        解析失败带错误信息修复重试一次（这是唯一一层重试；网络层重试在 client 内分类处理）。
        """
        model = self.models[model_key]
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        usage = []
        content, rec = self.client.chat(model, messages, json_mode=True,
                                        temperature=temperature, label=label or self.name, tag=tag)
        usage.append(rec)
        data = extract_json(content)
        if data is None:
            repair_msg = [
                *messages,
                {"role": "assistant", "content": content[:3000]},
                {"role": "user", "content": "上面的输出不是合法 JSON。请重新输出，只输出一个合法 JSON 对象，"
                                            "不要任何解释、不要 markdown 代码围栏。"},
            ]
            content2, rec2 = self.client.chat(model, repair_msg, json_mode=True,
                                              temperature=0.1,
                                              label=f"{label or self.name}-repair", tag=tag)
            usage.append(rec2)
            data = extract_json(content2)
        if data is None:
            raise SchemaError(f"[{self.name}] 模型未能返回合法 JSON；已记录调用用量，原始响应不写入错误信息")
        return data, usage


# ---------------------------------------------------------------- 1. 卖点拆解
class SellingPointAgent(BaseAgent):
    name = "selling_point_agent"

    def run(self, product: dict, tag: str = "stage:selling") -> tuple:
        system = (
            "你是资深的跨境 B2B 医疗器械营销顾问。给定产品信息，输出营销结构化分析 JSON。"
            "事实纪律（最高优先级）：\n"
            "1. 只能使用产品信息中给出的事实；capabilities 里 status=planned 的能力只能作为"
            "roadmap 提及且必须标注「规划中」，不得当作现有能力宣传；\n"
            "2. 产品资料未提供认证信息与性能数字 → 一律不得出现任何认证/性能数字宣称；\n"
            "3. evidence 字段必须引用事实来源（capability id 或字段名），不得编造。\n"
            "严格输出如下 JSON schema（字段名精确一致，point/audience/scenarios 用中文，keywords 用英文）：\n"
            "{\n"
            '  "selling_points": [{"point": "卖点一句话", "evidence": "支撑依据（引用 capability id/字段）"}, ...],  // 3-5 条\n'
            '  "audience": {"who": "目标客户画像", "roles": ["决策链角色"], "pain_points": ["痛点"]},\n'
            '  "scenarios": [{"scene": "使用场景", "story": "一段30字内的场景故事"}],  // 2-3 个\n'
            '  "keywords": ["english keyword", ...]  // 5-8 个英文搜索关键词\n'
            "}\n"
        )
        user = "产品信息（已过隐私门禁的营销公开字段）：\n" + json.dumps(product, ensure_ascii=False, indent=1)
        data, usage = self._chat_json(system, user, model_key="main",
                                      temperature=0.4, label="卖点拆解", tag=tag)
        validate_analysis(data)
        return data, usage


# ---------------------------------------------------------------- 2. 平台适配
class PlatformAdaptationAgent(BaseAgent):
    name = "platform_agent"

    def run(self, analysis: dict, platform_key: str, platform_spec: dict,
            compliance_level: str = "strict", tag: str = "") -> tuple:
        fields_cfg = platform_spec.get("fields") or {}
        system = (
            "你是多平台内容营销专家。基于产品卖点分析，为一个指定平台创作中文源文案"
            "（后续会由本地化 Agent 翻译成各目标语言，所以这里用中文写）。"
            "必须严格遵守用户给出的平台字段合同（数量/长度/单位）与平台规范（语气/结构/标签/禁忌）。\n"
            "输出 JSON schema（字段名精确一致）：\n"
            "{\n"
            '  "titles": ["标题"],            // 按合同数量输出；单标题平台就是一个元素\n'
            '  "descriptions": ["正文/描述"],  // 按合同数量输出\n'
            '  "bullets": ["要点"],           // 合同要求 0 条则输出空数组\n'
            '  "hashtags": ["#标签"],         // 合同区间外不合格\n'
            '  "extras": {"平台特有字段": 值}, // amazon 的 backend_keywords / landing_page 的 meta_description；无则空对象\n'
            '  "cta": "行动号召文案",\n'
            '  "visual_suggestion": "封面/素材建议"\n'
            "}\n"
            "医疗产品红线（任何平台不得违反）：不做疗效承诺；不使用 best/cure/100%/guaranteed 等绝对化用语"
            "（含否定语境的用法如 not a cure 允许，但源文案尽量避免）；"
            "不编造认证与性能数字；强调'辅助医师'定位，不得暗示替代医生。"
            "合规等级：" + compliance_level + "（strict 时所有宣称最保守）。"
        )
        user = (
            f"平台: {platform_key} ({platform_spec.get('name', '')})\n"
            f"平台字段合同（逐条硬性约束，超限即不合格）:\n{contract_prompt(fields_cfg)}\n\n"
            f"平台规范:\n{json.dumps({k: v for k, v in platform_spec.items() if k != 'fields'}, ensure_ascii=False, indent=1)}\n\n"
            f"产品卖点分析:\n{json.dumps(analysis, ensure_ascii=False, indent=1)}"
        )
        data, usage = self._chat_json(system, user, model_key="main", temperature=0.7,
                                      label=f"平台适配-{platform_key}", tag=tag)
        validate_copy_shape(data, f"平台适配[{platform_key}]")
        return data, usage


# ---------------------------------------------------------------- 3. 多语言本地化
class LocalizationAgent(BaseAgent):
    name = "localization_agent"

    def run(self, copy: dict, language_key: str, lang_spec: dict,
            platform_spec: dict, tag: str = "") -> tuple:
        fields_cfg = platform_spec.get("fields") or {}
        rtl_note = "本语种为 RTL（从右到左）书写，注意标点方向与拉丁字符（型号/品牌）的嵌入方式。" \
            if lang_spec.get("rtl") else ""
        system = (
            "你是母语级多语言本地化专家（不是翻译）。把中文源文案改写为地道的目标语言文案。"
            "要求：符合目标语言礼貌体系与文化禁忌（见语言规范）；保留产品事实与卖点不变（包括"
            "「规划中/roadmap」等状态标注不得丢失）；语气适配平台特性；禁止机翻腔。\n"
            "输出 JSON schema（与源文案同构，数量必须一致）：\n"
            "{\n"
            '  "titles": ["本地化标题"],     // 数量与源文案 titles 一致\n'
            '  "descriptions": ["本地化正文"], // 数量与源一致，保持分段\n'
            '  "bullets": ["本地化要点"],\n'
            '  "hashtags": ["本地化标签"],   // 行业大标签可保留英文，文化性标签本地化\n'
            '  "extras": {"平台特有字段": "本地化值"},\n'
            '  "cta": "本地化行动号召",\n'
            '  "localization_notes": "用中文说明你做了哪些本地化决策（敬语/RTL/称谓/禁忌规避等，30-80字）"\n'
            "}\n"
            + rtl_note
        )
        user = (
            f"目标语言: {language_key} ({lang_spec.get('name')} / {lang_spec.get('native_name')})\n"
            f"语言规范（必须逐条遵守）:\n{json.dumps(lang_spec, ensure_ascii=False, indent=1)}\n\n"
            f"平台语气: {platform_spec.get('tone', '')}｜CTA 风格: {platform_spec.get('cta_style', '')}\n"
            f"平台字段合同（本地化后仍须满足，超限即不合格）:\n{contract_prompt(fields_cfg)}\n\n"
            f"源文案:\n{json.dumps(copy, ensure_ascii=False, indent=1)}\n"
            f"要求：输出语言 = {lang_spec.get('name')}；泰语/越南语等必须保留正确声调符号。"
        )
        data, usage = self._chat_json(system, user, model_key="fast",
                                      temperature=0.6, label=f"本地化-{language_key}", tag=tag)
        validate_localized(data)
        return data, usage


# ---------------------------------------------------------------- 4. 合规审查
class ComplianceAgent(BaseAgent):
    """双层审查：本地正则规则引擎（待审信号）+ LLM 语义裁决与安全版改写。"""
    name = "compliance_agent"

    def __init__(self, client, models=None, compliance_cfg: dict = None):
        super().__init__(client, models)
        self.cfg = compliance_cfg or {}
        self._compiled = []
        for item in (self.cfg.get("global_rules", {}) or {}).get("forbidden_patterns", []) or []:
            try:
                self._compiled.append(
                    (re.compile(item["pattern"], re.IGNORECASE), item["severity"], item["reason"])
                )
            except re.error:
                continue  # 坏正则跳过（config_check 启动期已报错，这里不让引擎崩溃）

    # ---- 第 1 层：确定性正则（命中=待审信号，P1-12） ----
    def regex_scan(self, text: str) -> list:
        findings = []
        for rx, severity, reason in self._compiled:
            for m in rx.finditer(text):
                findings.append({
                    "source": "regex_signal",   # 待审信号，不是确定违规
                    "term": m.group(0),
                    "severity": severity,
                    "reason": reason,
                })
        return findings

    @staticmethod
    def publish_text(copy: dict) -> str:
        """把产物契约的全部发布字段拼成完整待审文本（含 hashtags 与 extras，P1-9）。"""
        parts = []
        for field in ("titles", "descriptions", "bullets", "hashtags"):
            v = copy.get(field) or []
            if isinstance(v, list):
                parts.extend(str(x) for x in v)
        extras = copy.get("extras") or {}
        if isinstance(extras, dict):
            for k, v in extras.items():
                if isinstance(v, list):
                    parts.extend(f"{k}: {x}" for x in v)
                else:
                    parts.append(f"{k}: {v}")
        cta = copy.get("cta")
        if isinstance(cta, str) and cta.strip():
            parts.append(cta)
        return "\n".join(parts)

    # ---- 第 2 层：LLM 语义裁决 + 安全版改写 ----
    def run(self, localized: dict, language_key: str, lang_spec: dict,
            market_rules: dict, level_cfg: dict, platform_key: str,
            fields_cfg: dict, signals: list, tag: str = "") -> tuple:
        assert_text_clean(self.publish_text(localized), context=f"合规审查入参[{platform_key}×{language_key}]")
        market_rules_slim = {k: v for k, v in (market_rules or {}).items()
                             if k in ("markets", "regulator", "rules")}
        system = (
            "你是医疗广告合规审查官。对文案做合规裁决并产出「修改后安全版」。\n"
            "两层输入：正则引擎检出的待审信号（signal_verdicts 逐条裁决）+ 你的语义层补充审查。\n"
            "裁决规则（重要）：\n"
            "1. 每个信号必须给出 verdict：violation（确认违规，安全版必须消除）或 benign（否定语境/"
            "引用/有据事实宣称，如 Not a cure / do not guarantee results / 不保证治愈——这些不是违规，"
            "安全版保留原文，不要删除或改写）；\n"
            "2. 除信号外你发现的夸大暗示、文化禁忌、疗效承诺等记入 llm_findings；\n"
            "3. risk_level 是你对「修复前文案」的综合风险判断；confirmed 违规会与它取最高级合并。\n"
            "4. 安全版要求：只修改风险表述，不改变卖点与平台结构；确认违规的信号必须全部消除；"
            "   安全版必须满足平台字段合同（数量与长度）；免责声明由系统统一追加，你不要自己写。\n"
            "输出 JSON schema（字段名精确一致）：\n"
            "{\n"
            '  "risk_level": "high|medium|low|pass",\n'
            '  "signal_verdicts": [{"term": "信号命中词", "verdict": "violation|benign", "reason": "裁决理由"}],\n'
            '  "llm_findings": [{"term": "风险表述", "severity": "high|medium|low", "suggestion": "改成什么"}],\n'
            '  "safe_titles": ["安全版标题"],\n'
            '  "safe_descriptions": ["安全版正文"],\n'
            '  "safe_bullets": ["安全版要点"],\n'
            '  "safe_hashtags": ["安全版标签"],\n'
            '  "safe_extras": {"平台特有字段": "安全版值"}\n'
            "}\n"
            "不得把'辅助决策'改成任何形式的'替代医生'。"
        )
        user = (
            f"目标语言: {language_key}（{lang_spec.get('name')}）\n"
            f"平台: {platform_key}\n"
            f"平台字段合同:\n{contract_prompt(fields_cfg)}\n\n"
            f"适用市场规则: {json.dumps(market_rules_slim, ensure_ascii=False)}\n"
            f"合规等级配置: {json.dumps(level_cfg, ensure_ascii=False)}\n"
            f"正则待审信号（必须逐条裁决，term 用命中词原文）: {json.dumps(signals, ensure_ascii=False)}\n"
            f"待审文案（全部发布字段）:\n{json.dumps(localized, ensure_ascii=False)}"
        )
        data, usage = self._chat_json(system, user, model_key="review",
                                      temperature=0.2,
                                      label=f"合规-{language_key}-{platform_key}", tag=tag)
        # P0-3：schema 校验（空报告/缺字段 → SchemaError → 上游判 review_failed）
        merged = merge_compliance(data, signals)
        # 残留二次校验：安全版全文（含 hashtags/extras）再扫一遍（P1-9）
        safe_copy = {
            "titles": data["safe_titles"], "descriptions": data["safe_descriptions"],
            "bullets": data["safe_bullets"], "hashtags": data["safe_hashtags"],
            "extras": data["safe_extras"],
        }
        residual_all = self.regex_scan(self.publish_text(safe_copy))
        benign_terms = {str(v["term"]).strip().lower()
                        for v in data["signal_verdicts"] if v["verdict"] == "benign"}
        residual_blocked = [r for r in residual_all
                            if str(r["term"]).strip().lower() not in benign_terms]
        residual_ok = [r for r in residual_all
                       if str(r["term"]).strip().lower() in benign_terms]
        merged["safe_copy"] = safe_copy
        merged["residual_findings"] = residual_blocked      # 未获 benign 裁决的残留 → 阻断
        merged["residual_benign"] = residual_ok             # 否定语境保留导致的命中，已裁决
        return merged, usage


# ---------------------------------------------------------------- 5. 素材 Prompt
class AssetPromptAgent(BaseAgent):
    name = "asset_prompt_agent"

    def run(self, safe_copy: dict, platform_key: str, platform_spec: dict,
            language_key: str, tag: str = "") -> tuple:
        aspect = PLATFORM_ASPECT.get(platform_key, "1:1")
        system = (
            "你是 AI 生图提示词工程师。为一条营销文案生成配套封面图的生图 Prompt。\n"
            "输出 JSON schema：\n"
            "{\n"
            '  "prompt_en": "英文生图提示词（主体+场景+风格+构图+光线，50-90词，含医疗科技质感）",\n'
            '  "prompt_zh": "中文生图提示词（与英文同义）",\n'
            '  "negative_prompt": "英文负向提示词（避免的元素）",\n'
            '  "aspect_ratio": "画幅"\n'
            "}\n"
            "医疗题材红线：画面不得出现真实患者面容/血腥/病痛惨状；不得出现可识别的医院 logo；"
            "设备界面截图类画面用抽象化 UI；不得出现监护仪数值/病历画面。"
        )
        user = (
            f"平台: {platform_key}（画幅 {aspect}）\n"
            f"平台视觉建议: {platform_spec.get('visual_suggestion', '')}\n"
            f"目标语言: {language_key}\n"
            f"文案（审核后安全版）:\n{json.dumps(safe_copy, ensure_ascii=False)}"
        )
        data, usage = self._chat_json(system, user, model_key="fast",
                                      temperature=0.6,
                                      label=f"素材-{platform_key}-{language_key}", tag=tag)
        if not isinstance(data, dict) or not str(data.get("prompt_en", "")).strip():
            raise SchemaError("素材 Prompt 输出 prompt_en 缺失或为空")
        data.setdefault("aspect_ratio", aspect)
        return data, usage


# ---------------------------------------------------------------- 工具函数
def extract_json(text: str):
    """从模型输出中稳健提取 JSON：剥思考块/代码围栏 -> 直接解析 -> 括号配平截取。"""
    if not text:
        return None
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(t)
    except (json.JSONDecodeError, ValueError):
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        if start < 0:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    candidate = t[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        break
    return None
