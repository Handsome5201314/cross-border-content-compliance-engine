# -*- coding: utf-8 -*-
"""业务价值计算器（对应评审 P1-6 / P1-7 重做）。

口径（两侧同质量、同验收标准，参数全部为「可调假设」，不是行业常见估值、不是实际成本）：

人工侧（一个团队从零交付同一批 平台×语言 文案）：
  共享产品研究(1次/批) + 各平台基础撰写(1次/平台，不按语言重复计)
  + 增量本地化(1次/平台×语言) + 逐条终审(1次/平台×语言)。

AI 侧（本引擎 + 人工，交付同一验收标准）：
  资料准备人工(1次/批) + 模型费用(API 估算) + 逐条事实/语言/合规复核人工(1次/条，与人工侧同标准)
  + 返修人工(每条审核未通过/失败计一次)。
  机器墙钟时长单独展示，不计入 AI 侧人工工时（并行发生）。

展示分离：人工工时 / AI 侧人工工时 / 引擎墙钟 / API 估算费用 / 两侧总交付成本。
零成功（可交付数为 0）：estimable=False，显示「无有效产出，无法估算」，禁止外推。
市场覆盖按实际通过审核（delivered）的结果计算，Global 不算国家。
"""

# 默认参数：全部为可调假设（UI 可改），无实测基线
DEFAULT_PARAMS = {
    "research_minutes": 60,            # 人工：共享产品研究（每批 1 次）
    "write_minutes_per_platform": 45,  # 人工：各平台基础撰写（每平台 1 次，不按语言重复计）
    "l10n_minutes_per_piece": 30,      # 人工：增量本地化（每 平台×语言 1 次）
    "review_minutes_per_piece": 15,    # 人工/AI 侧共用的逐条终审（同验收标准）
    "prep_minutes": 20,                # AI 侧：资料准备人工（每批 1 次）
    "rework_minutes_per_item": 10,     # AI 侧：返修人工（每条 审核未通过/失败 1 次）
    "hourly_rate_cny": 120,            # 综合时薪（可调假设）
    "monthly_pieces": 120,             # 月文案需求（条，用于假设性外推）
}

ASSUMPTION_NOTE = (
    "口径说明：人工与 AI 侧参数均为可调假设（非实测基线、非行业估值）；"
    "API 费用为按单价表的估算值（非实际成本）；月度数字为假设性外推。"
    "人工侧=共享研究+各平台基础撰写(不按语言重复计)+增量本地化+逐条终审；"
    "AI 侧=资料准备人工+API费用+逐条复核人工+返修人工。"
)


def compute_value(delivered_count: int, total_combos: int, blocked_count: int,
                  failed_count: int, num_platforms: int, num_languages: int,
                  engine_seconds: float, engine_cost_cny: float,
                  markets_covered: list, params: dict = None) -> dict:
    """计算一批文案的价值账。只有 delivered_count > 0 才可估算（P1-7）。"""
    p = {**DEFAULT_PARAMS, **(params or {})}
    counts = {
        "delivered": delivered_count, "review_blocked": blocked_count,
        "failed": failed_count, "total_combos": total_combos,
    }
    base = {
        "counts": counts,
        "num_platforms": num_platforms, "num_languages": num_languages,
        "engine_wall_seconds": round(engine_seconds or 0, 1),
        "engine_cost_cny_estimate": round(engine_cost_cny or 0, 4),
        "markets_covered": list(markets_covered or []),
        "markets_count": len(markets_covered or []),
        "params_used": p,
        "assumption_note": ASSUMPTION_NOTE,
    }
    if delivered_count <= 0:
        # P1-7：零成功 → 停止一切单位成本与月度外推
        return {**base, "estimable": False,
                "note": "无有效产出（可交付数为 0），无法估算节省与市场覆盖。"}

    total = max(total_combos, delivered_count)
    # ---- 人工侧 ----
    manual_minutes = (p["research_minutes"]
                      + p["write_minutes_per_platform"] * num_platforms
                      + (p["l10n_minutes_per_piece"] + p["review_minutes_per_piece"]) * total)
    manual_hours = manual_minutes / 60
    manual_cost = manual_hours * p["hourly_rate_cny"]

    # ---- AI 侧（同验收标准：逐条人工复核不可省） ----
    ai_manual_minutes = (p["prep_minutes"]
                         + p["review_minutes_per_piece"] * total
                         + p["rework_minutes_per_item"] * (blocked_count + failed_count))
    ai_manual_hours = ai_manual_minutes / 60
    ai_labor_cost = ai_manual_hours * p["hourly_rate_cny"]
    ai_total_cost = ai_labor_cost + (engine_cost_cny or 0)

    hours_saved = manual_hours - ai_manual_hours
    cost_saved = manual_cost - ai_total_cost
    capacity_multiplier = (manual_hours / ai_manual_hours) if ai_manual_hours > 0 else None

    # ---- 月度假设性外推（基于本批单位口径） ----
    monthly = p["monthly_pieces"]
    monthly_manual_minutes = (p["research_minutes"]
                              + p["write_minutes_per_platform"] * num_platforms
                              + (p["l10n_minutes_per_piece"] + p["review_minutes_per_piece"]) * monthly)
    rework_share = (blocked_count + failed_count) / max(total, 1)
    api_per_piece = (engine_cost_cny or 0) / delivered_count
    monthly_ai_manual_minutes = (p["prep_minutes"]
                                 + p["review_minutes_per_piece"] * monthly
                                 + p["rework_minutes_per_item"] * monthly * rework_share)
    monthly_api_cost = api_per_piece * monthly
    monthly_manual_cost = monthly_manual_minutes / 60 * p["hourly_rate_cny"]
    monthly_ai_cost = (monthly_ai_manual_minutes / 60 * p["hourly_rate_cny"]) + monthly_api_cost

    return {**base, "estimable": True,
            "manual_side": {
                "minutes_total": round(manual_minutes, 1),
                "hours_total": round(manual_hours, 1),
                "cost_cny": round(manual_cost, 0),
            },
            "ai_side": {
                "manual_minutes_total": round(ai_manual_minutes, 1),
                "manual_hours_total": round(ai_manual_hours, 1),
                "labor_cost_cny": round(ai_labor_cost, 0),
                "api_cost_cny_estimate": round(engine_cost_cny or 0, 4),
                "total_cost_cny": round(ai_total_cost, 0),
                "engine_wall_seconds": round(engine_seconds or 0, 1),
            },
            "savings": {
                "hours_saved": round(hours_saved, 1),
                "cost_saved_cny": round(cost_saved, 0),
                "capacity_multiplier": round(capacity_multiplier, 1) if capacity_multiplier else None,
            },
            "monthly_projection": {
                "monthly_pieces_assumed": monthly,
                "manual_cost_cny_per_month": round(monthly_manual_cost, 0),
                "ai_cost_cny_per_month": round(monthly_ai_cost, 0),
                "ai_api_cost_cny_per_month": round(monthly_api_cost, 4),
                "cost_saved_cny_per_month": round(monthly_manual_cost - monthly_ai_cost, 0),
                "note": "月度为假设性外推（可调参数），非实测",
            }}
