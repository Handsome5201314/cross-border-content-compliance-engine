# -*- coding: utf-8 -*-
"""市场规则（对应评审 P1-11）：市场选择独立于语言，未知不视为通过。

- markets_for_language：语种 -> 适用市场 = (language_market_map[lang] ∩ 产品目标市场)；
  交集为空时回落到 GLOBAL 规则桶（通用英语物料规则）；GLOBAL 只是规则桶，不算国家市场。
- build_market_report：按产品目标市场逐个输出 广告风险 / 准入待核 / 数据主权待核，
  状态恒为 pending_verification（本引擎不联网核验监管原文，未知≠通过）。
"""


def markets_for_language(lang: str, product_markets: list, compliance_cfg: dict) -> list:
    """语种适用的市场规则集合。空结果由调用方在启动期报错（不允许静默跳过）。"""
    mapped = compliance_cfg.get("language_market_map", {}).get(lang, [])
    product_set = list(product_markets or [])
    applicable = [m for m in mapped if m in product_set]
    if not applicable and "GLOBAL" in mapped:
        applicable = ["GLOBAL"]
    return applicable


def merge_market_rules(market_keys: list, compliance_cfg: dict) -> dict:
    """把多个市场的广告规则合并为一份给合规 Agent 的规则包。"""
    markets = compliance_cfg.get("markets", {})
    names, regs, rules = [], [], []
    for k in market_keys or []:
        m = markets.get(k)
        if not m:
            continue  # 引用存在性已由 config_check 在启动期校验
        names.append(m.get("name", k))
        if m.get("regulator"):
            regs.append(f"{m.get('name', k)}: {m['regulator']}")
        rules.extend(m.get("rules", []) or [])
    return {"markets": " / ".join(names), "regulator": "；".join(regs), "rules": rules}


def build_market_report(product_markets: list, results: list, compliance_cfg: dict) -> dict:
    """按市场输出双合规报告（广告风险 + 准入待核 + 数据主权待核）。

    - delivered_pieces：该市场下「可交付」的文案条数（按实际通过审核的结果计）；
    - 状态恒为 pending_verification：本引擎不做监管原文核验，未知不视为通过。
    - GLOBAL 不出现在报告里（规则桶不是国家市场）。
    """
    markets = compliance_cfg.get("markets", {})
    report = {}
    for code in product_markets or []:
        if code == "GLOBAL":
            continue
        m = markets.get(code)
        if not m:
            report[code] = {"name": code, "status": "config_error",
                            "error": "市场未在 compliance.yaml 定义（启动校验应已拦截）"}
            continue
        delivered = sum(
            1 for r in results
            if r.get("status") == "delivered" and code in (r.get("markets") or [])
        )
        report[code] = {
            "name": m.get("name", code),
            "regulator": m.get("regulator", ""),
            "ad_rules_count": len(m.get("rules", []) or []),
            "delivered_pieces": delivered,
            "access_pending": m.get("access_notes", []) or [],
            "data_sovereignty_pending": m.get("data_sovereignty_notes", []) or [],
            "status": "pending_verification",
            "status_note": "准入与数据主权事项为待核清单，未联网核验监管原文，不构成法律意见，未知不视为通过",
        }
    return report


def covered_markets(results: list) -> list:
    """实际通过审核（可交付）覆盖的市场：按 delivered 结果统计，GLOBAL 不计。"""
    out = []
    for r in results:
        if r.get("status") != "delivered":
            continue
        for m in r.get("markets") or []:
            if m != "GLOBAL" and m not in out:
                out.append(m)
    return out
