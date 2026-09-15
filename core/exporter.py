# -*- coding: utf-8 -*-
"""导出模块（对应评审 P1-9 / P2-14 / P2-16）。

- P1-9：Markdown 发布稿只消费「审核后对象」（final_copy / safe_*），绝不把原始
  localized 字段（含未修复的 hashtags）拼回去；残留扫描覆盖完整导出内容；
- P2-14：导出使用与看板同一份快照（result + 重算后的 value + 参数），
  UI 改参数后导出与页面展示一致；
- P2-16：RTL 语种（ar）的段落用 <div dir="rtl"> 包裹（Markdown 允许内联 HTML），
  纯文本复制内容不受影响。

JSON 导出保留完整 run_result（含原始字段，供调试/单条重跑），在文件头标注用途区别。
"""
import html
import json
from datetime import datetime


def _rtl(text: str, rtl: bool) -> str:
    """RTL 语种安全包裹（html 转义防注入）。"""
    if not text:
        return ""
    if rtl:
        return f'<div dir="rtl" lang="ar" style="text-align:right; unicode-bidi:plaintext">{html.escape(text).replace(chr(10), "<br>")}</div>'
    return text.replace("\n\n", "  \n")  # markdown 段内换行


def export_publish_texts(result: dict) -> list:
    """收集「发布稿导出内容」的全部文本（P1-9 回归用）：只取 final_copy，不含原始字段。"""
    texts = []
    for r in result.get("results", []):
        fc = r.get("final_copy")
        if r.get("status") == "delivered" and isinstance(fc, dict):
            for field in ("titles", "descriptions", "bullets", "hashtags"):
                v = fc.get(field) or []
                if isinstance(v, list):
                    texts.extend(str(x) for x in v)
            for k, v in (fc.get("extras") or {}).items():
                texts.append(f"{k}: {v}")
            if fc.get("cta"):
                texts.append(fc["cta"])
            if fc.get("disclaimer"):
                texts.append(fc["disclaimer"])
    return texts


def to_markdown(result: dict, value: dict = None, languages_cfg: dict = None) -> str:
    """把 run_result 转成可放进作品说明书的 Markdown（只消费审核后对象）。"""
    languages_cfg = languages_cfg or {}
    meta = result.get("meta", {})
    usage = result.get("usage", {})
    counts = result.get("counts", {})
    lines = [
        "# 跨境卖家 AI 多平台文案智造引擎 · 生成结果（发布稿）",
        "",
        f"- 生成时间：{meta.get('generated_at')}　引擎版本：{meta.get('engine')}",
        f"- 平台：{'、'.join(meta.get('platforms', []))}　语言：{'、'.join(meta.get('languages', []))}"
        f"　合规等级：{meta.get('compliance_level')}",
        f"- 结果：可交付 {counts.get('delivered', 0)} / 审核未通过 {counts.get('review_blocked', 0)}"
        f" / 失败 {counts.get('failed', 0)} / 超时 {counts.get('skipped_deadline', 0)}（共 {counts.get('total', 0)}）",
        f"- 耗时 {usage.get('wall_time_s')}s ｜ 总 Tokens {usage.get('total_tokens')}"
        f" ｜ API 成本估算 ¥{usage.get('cost_estimate_cny')}（估算值，非实际成本）",
        "",
    ]
    if meta.get("degraded_reason"):
        lines += [f"> ⚠️ 降级说明：{meta['degraded_reason']}", ""]

    a = result.get("analysis") or {}
    if a.get("selling_points"):
        lines += ["## 卖点拆解", ""]
        lines += [f"- **{s.get('point', '')}**：{s.get('evidence', '')}" for s in a["selling_points"]]
        lines.append("")

    for r in result.get("results", []):
        plat = r.get("platform", "?")
        lang = r.get("language", "?")
        lines += [f"## {plat} × {lang}", ""]
        if r.get("status") != "delivered":
            lines += [f"> ⚠️ 未交付（{r.get('status')}）：{r.get('error')}", ""]
            continue
        fc = r.get("final_copy") or {}
        rtl = bool((languages_cfg.get(lang) or {}).get("rtl"))
        for i, t in enumerate(fc.get("titles") or []):
            lines += [f"**标题{i + 1}**：{_rtl(t, rtl)}", ""]
        for i, d in enumerate(fc.get("descriptions") or []):
            lines += [f"**正文/描述{i + 1}**：", "", _rtl(d, rtl), ""]
        if fc.get("bullets"):
            lines += ["**要点**：", ""]
            lines += [f"- {_rtl(b, rtl)}" for b in fc["bullets"]]
            lines.append("")
        if fc.get("hashtags"):
            lines += [" ".join(fc["hashtags"]), ""]
        if fc.get("disclaimer"):
            lines += [f"> {_rtl(fc['disclaimer'], rtl)}", ""]
        for k, v in (fc.get("extras") or {}).items():
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            lines += [f"**{k}**：{v}", ""]
        if fc.get("cta"):
            lines += [f"**CTA**：{_rtl(fc['cta'], rtl)}", ""]
        comp = r.get("compliance") or {}
        lines += [f"**合规**：{comp.get('merged_risk_level', '')}"
                  f"（待审信号 {len(comp.get('signal_findings', []))}"
                  f" / 确认违规 {len(comp.get('confirmed_findings', []))}"
                  f" / benign 保留 {len(comp.get('benign_verdicts', []))}"
                  f" / 残留 {len(comp.get('residual_findings', []))}）", ""]
        ap = r.get("asset_prompt") or {}
        if ap.get("prompt_en"):
            lines += ["**生图 Prompt（EN）**：", "", f"```{ap['prompt_en']}```", ""]

    mr = result.get("market_report") or {}
    if mr:
        lines += ["## 市场合规报告（准入/数据主权为待核清单，不构成法律意见）", ""]
        for code, m in mr.items():
            lines += [f"### {code} {m.get('name', '')}", "",
                      f"- 监管机构（待核）：{m.get('regulator', '')}",
                      f"- 可交付物料：{m.get('delivered_pieces', 0)} 条",
                      f"- 准入待核：{'；'.join(m.get('access_pending', []) or ['—'])}",
                      f"- 数据主权待核：{'；'.join(m.get('data_sovereignty_pending', []) or ['—'])}",
                      f"- 状态：{m.get('status', '')}", ""]

    v = value if value is not None else result.get("value")
    if v:
        lines += ["## 业务价值（口径见文末说明）", ""]
        if not v.get("estimable"):
            lines += [f"> {v.get('note', '无有效产出，无法估算')}", ""]
        else:
            lines += [
                f"- 人工侧：{v['manual_side']['hours_total']} 小时 / ¥{v['manual_side']['cost_cny']:,.0f}（可调假设）",
                f"- AI 侧（含人工复核与返修）：{v['ai_side']['manual_hours_total']} 小时人工"
                f" + API ¥{v['ai_side']['api_cost_cny_estimate']}（估算）"
                f" = 总 ¥{v['ai_side']['total_cost_cny']:,.0f}",
                f"- 节省：{v['savings']['hours_saved']} 小时 / ¥{v['savings']['cost_saved_cny']:,.0f}"
                f"　产能 ×{v['savings']['capacity_multiplier']}",
                f"- 市场覆盖（按实际可交付计）：{v['markets_count']} 个（{', '.join(v['markets_covered'])}）",
                f"- 月度假设性外推：省 ¥{v['monthly_projection']['cost_saved_cny_per_month']:,.0f}/月",
                "",
                f"> {v.get('assumption_note', '')}", "",
            ]
    notices = result.get("engine_notices") or {}
    for k in ("deployment_clarification", "privacy", "legal"):
        if notices.get(k):
            lines += [f"> {notices[k]}", ""]
    lines += [f"\n*导出时间：{datetime.now().isoformat(timespec='seconds')}；"
              f"发布稿只包含审核后安全版内容。*", ""]
    return "\n".join(lines)


def to_export_json(result: dict, value: dict = None, params: dict = None) -> str:
    """JSON 导出：完整 run_result（调试/单条重跑用）+ 当前看板参数与重算价值（同一快照）。"""
    payload = dict(result)
    if value is not None:
        payload["value"] = value
    if params is not None:
        payload["value_params"] = params
    payload["export_note"] = ("JSON 含原始字段仅供调试与重跑；对外发布请用 Markdown 发布稿"
                              "（只含审核后安全版）。")
    return json.dumps(payload, ensure_ascii=False, indent=2)
