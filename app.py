# -*- coding: utf-8 -*-
"""Streamlit Web Demo：跨境卖家 AI 多平台文案智造引擎 v3。

运行：C:/Python314/python.exe -m streamlit run app.py

本次返工要点（对应评审编号）：
- P0-2：顶部明示「被营销产品=全本地部署」vs「本引擎调用云端模型」；产品输入过字段白名单+本地隐私门禁；
- P0-4：三种演示模式——小批次真跑（默认 3 平台×2 语种）/ 加载预生成全量 / 单条重跑；
  完成一条显示一条；失败项如实展示并可单独重跑；默认不生图；
- P1-7：价值看板零成功时显示「无有效产出，无法估算」；
- P2-14：看板参数修改后，页面展示与 JSON/Markdown 导出使用同一份快照；
- P2-16：RTL 语种（ar）提供 dir=rtl 的安全转义阅读预览，保留纯文本复制。
"""
import html as _html
import json
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import load_yaml_config                                 # noqa: E402
from core.config_check import validate_all_configs                # noqa: E402
from core.exporter import to_markdown, to_export_json             # noqa: E402
from uuid import uuid4
from core.usage import update_result_usage
from core.gateway_policy import validate_ui_gateway
from core.llm_client import LLMClient, mask_key, IMAGE_MODEL_CANDIDATES  # noqa: E402
from core.pipeline import Pipeline, STATUS_LABELS                 # noqa: E402
from core.privacy_gate import PrivacyViolation, validate_product  # noqa: E402
from core.value_calc import DEFAULT_PARAMS, compute_value         # noqa: E402
from core.package_view import show_packages

st.set_page_config(page_title="跨境文案智造引擎 v3", page_icon="·", layout="wide")
THEME_PATH = Path(__file__).resolve().parent / "_theme.css"
st.markdown(f"<style>{THEME_PATH.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)
st.markdown('<div class="topbar"><div class="topbrand"><span class="topmark">AI</span><span>跨境内容合规引擎</span><span class="topsub">生成工作台</span></div><div class="topspacer"></div><div class="topcredits">积分 <b>1,240</b><span>充值</span></div><div class="top-avatar">帅</div></div>', unsafe_allow_html=True)

PLATFORMS_CFG = load_yaml_config("platforms")
LANGUAGES_CFG = load_yaml_config("languages")
COMPLIANCE_CFG = load_yaml_config("compliance")
SAMPLE_PRODUCT = json.loads(
    (Path(__file__).resolve().parent / "samples" / "product_medical_appliance.json")
    .read_text(encoding="utf-8"))
PRECOMPUTED_PATH = Path(__file__).resolve().parent / "samples" / "precomputed_full.json"

MODEL_CANDIDATES = [
    "qwen3.7-plus", "qwen3.7-max", "qwen3.8-max", "qwen3.6-plus", "qwen3.6-flash",
    "deepseek-v4-pro", "deepseek-v4-flash", "kimi-k2.7-code", "glm-5.2", "MiniMax-M2.5",
]
STATUS_ICON = {"delivered": "PASS", "review_blocked": "BLOCKED", "failed": "FAILED", "skipped_deadline": "QUEUED"}
RISK_COLOR = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW", "pass": "PASS"}

st.markdown('<div class="prototype-kicker">SLATE PRO / COMPLIANCE ENGINE</div>', unsafe_allow_html=True)

# P0-2：本地产品 vs 云端引擎的显式区分（不得让人误以为引擎也不上云）
notices = COMPLIANCE_CFG.get("engine_notices", {})
st.markdown(f'<div class="notice warn">{_html.escape(notices.get("deployment_clarification", ""))}</div>', unsafe_allow_html=True)
st.markdown(f'<div class="notice info">{_html.escape(notices.get("privacy", ""))}<br><br>{_html.escape(notices.get("legal", ""))}</div>', unsafe_allow_html=True)

# 深水内容包浏览器在结果区末尾展示，避免打断新任务工作台。


# ================================================================ 工具
def rtl_preview(text: str, lang: str) -> None:
    """RTL 语种的安全转义阅读预览 + 纯文本复制（P2-16）。"""
    spec = LANGUAGES_CFG.get(lang, {})
    if spec.get("rtl") and text:
        st.markdown(
            f'<div class="rtl-copy" dir="rtl" lang="{lang}">'
            f'{_html.escape(text).replace(chr(10), "<br>")}</div>',
            unsafe_allow_html=True)
    st.code(text or "", language=None)


def render_result(result: dict) -> None:
    """渲染完整结果（真跑 / 预生成 / 重跑后共用）。"""
    counts = result.get("counts", {})
    u = result.get("usage") or {}
    stats = [("可交付", counts.get("delivered", 0)), ("审核未通过", counts.get("review_blocked", 0)),
             ("生成失败", counts.get("failed", 0)), ("超时跳过", counts.get("skipped_deadline", 0)),
             ("总 Tokens", f"{u.get('total_tokens', 0):,}")]
    st.markdown('<div class="stat-grid">' + ''.join(
        f'<div class="stat-card"><div class="stat-label">{_html.escape(str(label))}</div>'
        f'<div class="stat-value">{_html.escape(str(value))}</div></div>'
        for label, value in stats) + '</div>', unsafe_allow_html=True)
    st.caption(f"耗时 {u.get('wall_time_s', '?')}s ｜ 调用 {u.get('total_calls', '?')} 次 ｜ "
               f"API 成本估算 ¥{u.get('cost_estimate_cny', '?')}（估算值，非实际成本）")
    if result.get("meta", {}).get("degraded_reason"):
        st.markdown(f'<div class="notice warn">降级：{_html.escape(result["meta"]["degraded_reason"])}</div>', unsafe_allow_html=True)

    st.subheader("文案结果")
    ok_items = [r for r in result["results"] if r["status"] == "delivered"]
    other_items = [r for r in result["results"] if r["status"] != "delivered"]
    st.caption(f"成功 {len(ok_items)} / 总 {len(result['results'])} 条"
               + (f"｜未交付: {', '.join(r['platform'] + '×' + r['language'] for r in other_items)}"
                  f"（原因见下方卡片，不隐藏）" if other_items else ""))

    fcol1, fcol2 = st.columns(2)
    with fcol1:
        f_plat = st.multiselect("筛选平台", result["meta"]["platforms"], default=result["meta"]["platforms"],
                                key="f_plat")
    with fcol2:
        f_lang = st.multiselect("筛选语言", result["meta"]["languages"], default=result["meta"]["languages"],
                                key="f_lang")
    shown = [r for r in result["results"] if r["platform"] in f_plat and r["language"] in f_lang]

    # 未交付项排在最前，如实展示（P0-4：失败不许藏）
    shown.sort(key=lambda r: 0 if r["status"] != "delivered" else 1)
    for r in shown:
        icon = STATUS_ICON.get(r["status"], "STATUS")
        plat = PLATFORMS_CFG.get(r["platform"], {}).get("name", r["platform"])
        lang = LANGUAGES_CFG.get(r["language"], {}).get("name", r["language"])
        stats = r.get("stats", {})
        header = (f"{icon} {plat} × {lang}（{r['language']}）｜{STATUS_LABELS.get(r['status'], r['status'])}"
                  f"｜{stats.get('latency_s', '?')}s / {stats.get('tokens', 0)} tok")
        with st.expander(header, expanded=(r["status"] != "delivered")):
            if r.get("error"):
                st.markdown(f'<div class="verdict-card block"><div class="verdict-head">{_html.escape(r["status"])}</div><div class="verdict-reason">{_html.escape(str(r["error"]))}</div></div>', unsafe_allow_html=True)
            # 单条重跑按钮（P0-4）
            if r["status"] != "delivered" and st.button("单条重跑", key=f"rerun_{r['combo_id']}"):
                _do_rerun(r["platform"], r["language"])
            comp = r.get("compliance") or {}
            fc = r.get("final_copy") or {}
            if r["status"] == "delivered":
                t1, t2, t3, t4 = st.tabs(["发布稿（安全版）", "合规报告", "素材 Prompt", "源文案与本地化"])
                with t1:
                    for i, t in enumerate(fc.get("titles") or []):
                        st.markdown(f"**标题{i + 1}**")
                        rtl_preview(t, r["language"])
                    for i, d in enumerate(fc.get("descriptions") or []):
                        st.markdown(f"**正文/描述{i + 1}**")
                        rtl_preview(d, r["language"])
                    if fc.get("bullets"):
                        st.markdown("**要点**")
                        for b in fc["bullets"]:
                            rtl_preview(b, r["language"])
                    if fc.get("hashtags"):
                        st.markdown("**标签（审核后安全版）**：" + " ".join(fc["hashtags"]))
                    for k, v in (fc.get("extras") or {}).items():
                        if isinstance(v, list):
                            v = ", ".join(str(x) for x in v)
                        st.markdown(f"**{k}**：{v}")
                    if fc.get("cta"):
                        st.markdown("**CTA**：")
                        rtl_preview(fc["cta"], r["language"])
                    if fc.get("disclaimer"):
                        st.caption("免责声明（系统确定性追加，非模型生成）")
                        rtl_preview(fc["disclaimer"], r["language"])
                with t2:
                    risk = comp.get("merged_risk_level", "n/a")
                    risk_class = "block" if risk in {"high", "medium"} else "pass"
                    st.markdown(f'<div class="verdict-card {risk_class}"><div class="verdict-head">裁决 / {_html.escape(RISK_COLOR.get(risk, risk))}</div><div class="verdict-reason">修复前文案口径，以下结果来自双层审查。</div></div>', unsafe_allow_html=True)
                    f_sig = comp.get("signal_findings", [])
                    f_conf = comp.get("confirmed_findings", [])
                    f_ben = comp.get("benign_verdicts", [])
                    f_res = comp.get("residual_findings", [])
                    f_lim = comp.get("limit_findings", [])
                    st.write(f"正则待审信号 {len(f_sig)} ｜ 确认违规 {len(f_conf)} ｜ benign 保留 {len(f_ben)} "
                             f"｜ 安全版残留 {len(f_res)} ｜ 合同违规 {len(f_lim)}")
                    if f_sig:
                        st.markdown("**第 1 层：正则待审信号（命中≠违规，语义层裁决）**")
                        for f in f_sig:
                            st.markdown(f'<div class="verdict-card block"><div class="verdict-head">待审信号 / {_html.escape(str(f.get("severity", "")))}</div><div class="verdict-source">{_html.escape(str(f.get("term", "")))}</div><div class="verdict-reason">{_html.escape(str(f.get("reason", "")))}</div></div>', unsafe_allow_html=True)
                    if f_conf:
                        st.markdown("**确认违规（安全版已消除）**")
                        for f in f_conf:
                            st.markdown(f'<div class="verdict-card block"><div class="verdict-head">确认违规 / {_html.escape(str(f.get("severity", "")))}</div><div class="verdict-source">{_html.escape(str(f.get("term", "")))}</div><div class="verdict-reason">{_html.escape(str(f.get("adjudication", "")))}</div></div>', unsafe_allow_html=True)
                    if f_ben:
                        st.markdown("**benign 保留（否定语境等，未删除原文）**")
                        for b in f_ben:
                            st.markdown(f'<div class="verdict-card pass"><div class="verdict-head">BENIGN / 保留</div><div class="verdict-source">{_html.escape(str(b.get("term", "")))}</div><div class="verdict-reason">{_html.escape(str(b.get("reason", "")))}</div></div>', unsafe_allow_html=True)
                    if comp.get("llm_findings"):
                        st.markdown("**第 2 层：LLM 语义发现**")
                        for f in comp["llm_findings"]:
                            st.markdown(f'<div class="verdict-card block"><div class="verdict-head">语义发现 / {_html.escape(str(f.get("severity", "")))}</div><div class="verdict-source">{_html.escape(str(f.get("term", "")))}</div><div class="verdict-fix">建议：{_html.escape(str(f.get("suggestion", "")))}</div></div>', unsafe_allow_html=True)
                    if f_res:
                        st.error(f"安全版仍有 {len(f_res)} 项未裁决残留 → 已阻断交付")
                    if f_lim:
                        st.error("平台字段合同违规 → 已阻断交付：" +
                                 "; ".join(f"{v['field']} {v['issue']}" for v in f_lim))
                with t3:
                    ap = r.get("asset_prompt") or {}
                    if ap.get("prompt_en"):
                        st.markdown("**英文生图 Prompt**")
                        st.code(ap["prompt_en"], language=None)
                        st.markdown("**中文生图 Prompt**")
                        st.code(ap.get("prompt_zh", ""), language=None)
                        st.markdown("**负向 Prompt**")
                        st.code(ap.get("negative_prompt", ""), language=None)
                        st.caption(f"建议画幅：{ap.get('aspect_ratio', '')}")
                    if r.get("asset_error"):
                        st.warning(f"素材 Prompt 生成失败（不影响文案交付）: {r['asset_error']}")
                with t4:
                    src = r.get("copy") or {}
                    st.markdown("**平台适配源文案（中文）**")
                    for i, t in enumerate(src.get("titles") or []):
                        st.write(f"标题{i + 1}：{t}")
                    st.write((src.get("descriptions") or [""])[0])
                    loc = r.get("localized") or {}
                    st.markdown(f"**本地化决策说明**：{loc.get('localization_notes', '')}")
            elif comp:
                st.write(f"综合风险等级：{comp.get('merged_risk_level', '?')}；"
                         f"待审信号 {len(comp.get('signal_findings', []))} / "
                         f"确认违规 {len(comp.get('confirmed_findings', []))} / "
                         f"残留 {len(comp.get('residual_findings', []))}。修复后可用「单条重跑」。")

    # 市场报告
    mr = result.get("market_report") or {}
    if mr:
        st.subheader("市场双合规报告（准入/数据主权为待核清单，不构成法律意见）")
        rows = []
        for code, m in mr.items():
            rows.append({"市场": f"{code} {m.get('name', '')}", "监管机构(待核)": m.get("regulator", ""),
                         "可交付物料": m.get("delivered_pieces", 0),
                         "准入待核": "；".join(m.get("access_pending", []) or ["—"])[:80],
                         "数据主权待核": "；".join(m.get("data_sovereignty_pending", []) or ["—"])[:80],
                         "状态": m.get("status", "")})
        st.dataframe(rows, use_container_width=True, hide_index=True)

    # 运行统计
    st.subheader("运行统计")
    if u:
        st.dataframe([{"模型": m, "调用次数": s["calls"], "tokens": s["total_tokens"]}
                      for m, s in (u.get("by_model") or {}).items()],
                     use_container_width=True, hide_index=True)
        st.caption(u.get("price_note", ""))


def _get_client(base_url: str = None, api_key: str = None):
    """获取 LLMClient。支持传入自定义凭证；凭证变化时自动重建。

    优先级：显式传入的自定义凭证 > 平台默认（环境变量 secrets > 本地 .env）。
    """
    if base_url is None and api_key is None and st.session_state.get("u_force"):
        base_url = st.session_state.get("u_base")
        api_key = st.session_state.get("u_key")
        if not (base_url or "").strip() or not (api_key or "").strip():
            raise RuntimeError("已选择自定义凭证，请同时填写网关和凭证")
    sig = ((base_url or "").strip(), (api_key or "").strip())
    if sig[0] or sig[1]:
        try:
            validate_ui_gateway(sig[0])
        except ValueError:
            raise RuntimeError("自定义网关不在管理员允许列表中，或 URL 格式不合法") from None
    if st.session_state.get("_client_sig") != sig or "client" not in st.session_state:
        previous = st.session_state.get("client")
        if previous is not None:
            previous.client.close()
        st.session_state["client"] = LLMClient(
            base_url=(base_url or "").strip() or None,
            api_key=(api_key or "").strip() or None,
        )
        st.session_state["_client_sig"] = sig
    return st.session_state["client"]


def _rebuild_pipeline(result: dict) -> Pipeline:
    """从结果快照重建流水线（单条重跑用）。"""
    meta = result.get("meta", {})
    client = _get_client()
    return Pipeline(client, result.get("product") or {}, meta.get("languages", []),
                    meta.get("platforms", []), meta.get("compliance_level", "strict"),
                    models=meta.get("models"))


def _do_rerun(platform: str, language: str) -> None:
    result = st.session_state.get("result")
    if not result:
        return
    try:
        pipeline = _rebuild_pipeline(result)
        ctx = result.get("context") or {}
        base = (ctx.get("platform_copies") or {}).get(platform)
        with st.spinner(f"重跑 {platform}×{language} …"):
            item = pipeline._run_combo(platform, language, base)
        for i, r in enumerate(result["results"]):
            if r["combo_id"] == item["combo_id"]:
                result["results"][i] = item
        result = pipeline.finalize(result)
        st.session_state["result"] = result
        st.rerun()
    except Exception as e:  # noqa: BLE001
        st.error(f"重跑失败: {type(e).__name__}: {e}")


# ================================================================ 顶部与侧栏壳层
with st.sidebar:
    st.markdown('<div class="sidebar-brand"><span class="sidebar-mark">AI</span><span>跨境内容合规引擎</span></div>', unsafe_allow_html=True)
    st.button("+ 新建生成任务", use_container_width=True, type="primary")
    st.markdown('<div class="nav-group"><div class="nav-label">我的空间 <span>私有</span></div><div class="nav-item">□　我的文件 <b>12</b></div><div class="nav-item">↥　我上传的资料 <b>3</b></div><div class="nav-item">▤　生成产物 <b>9</b></div></div><div class="nav-group"><div class="nav-label">最近任务</div><div class="nav-item active"><i></i>医疗出海 · 6 语种 <b class="pass-tag">6/6</b></div><div class="nav-item"><i></i>消费品对照 · 2 语种 <b class="pass-tag">2/2</b></div><div class="nav-item"><i class="block-dot"></i>印尼直播脚本 <b class="block-tag">1 拦截</b></div></div><div class="nav-group"><div class="nav-label">账户</div><div class="nav-item">◷　积分明细</div><div class="nav-item">▱　充值记录</div><div class="nav-item">◇　隐私与权限</div></div>', unsafe_allow_html=True)
    st.markdown('<div class="space-note">你的空间仅你可见，AI 只在你自己的目录内读写</div>', unsafe_allow_html=True)
    st.markdown('<div class="credits-pill"><span class="lbl">积分</span><span class="val">1,240</span><span class="hint">注册赠送 666 · 按 token 倍率扣费</span></div>', unsafe_allow_html=True)

# ================================================================ 主区任务表单
name = name_en = form = deploy = notes = ""
st.markdown('<div class="task-head"><div><h1>新建生成任务</h1><p>输入产品资料，输出多语种合规内容包；发布前自动拦截违规表述。</p></div><span class="isolation">空间已隔离</span></div>', unsafe_allow_html=True)
with st.container(border=True):
    st.markdown('<h3><span class="section-number">01</span> 产品输入</h3>', unsafe_allow_html=True)
    if st.button("填充内置真实产品", key="fill_product"):
        st.session_state.update({"p_name": SAMPLE_PRODUCT["product_name"], "p_name_en": SAMPLE_PRODUCT["product_name_en"], "p_form": SAMPLE_PRODUCT["form"], "p_deploy": SAMPLE_PRODUCT["deployment"], "p_notes": SAMPLE_PRODUCT["marketing_notes"]})
    c1, c2 = st.columns(2)
    with c1:
        name = st.text_input("产品名（中文）", value=SAMPLE_PRODUCT["product_name"], key="p_name")
        form = st.text_input("产品形态", value=SAMPLE_PRODUCT["form"], key="p_form")
    with c2:
        name_en = st.text_input("产品名（英文）", value=SAMPLE_PRODUCT["product_name_en"], key="p_name_en")
        deploy = st.text_input("部署方式", value=SAMPLE_PRODUCT["deployment"], key="p_deploy")
    notes = st.text_area("营销注意事项 / 红线", value=SAMPLE_PRODUCT["marketing_notes"], key="p_notes", height=90)
    st.caption("输入会先过字段白名单与本地隐私门禁，命中患者标识直接阻断。")

lang_defaults = [l for l in ["en", "ar"] if l in LANGUAGES_CFG]
plat_defaults = [p for p in ["tiktok", "instagram", "landing_page"] if p in PLATFORMS_CFG]
with st.container(border=True):
    st.markdown('<h3><span class="section-number">02</span> 目标语种与平台</h3>', unsafe_allow_html=True)
    languages = st.multiselect("目标语言", list(LANGUAGES_CFG.keys()), default=lang_defaults, format_func=lambda k: f"{k} · {LANGUAGES_CFG[k]['name']}" + ("（RTL）" if LANGUAGES_CFG[k].get("rtl") else ""))
    platforms = st.multiselect("目标平台", list(PLATFORMS_CFG.keys()), default=plat_defaults, format_func=lambda k: PLATFORMS_CFG[k]["name"])
    level = st.radio("合规等级", ["strict", "standard", "loose"], index=0, format_func=lambda k: COMPLIANCE_CFG["levels"][k]["name"], horizontal=True)
    deadline = st.slider("整批时间预算（秒）", 120, 1800, 600, 60, help="超时未执行的组合标记为「超时跳过」并如实展示")
    st.markdown('<div class="cost-hint">本次预计消耗 <b>180 积分</b>（6 语种 × 独立站）。当前余额 <b>1,240</b>，执行后剩余 <b>1,060</b>。</div>', unsafe_allow_html=True)

with st.expander("模型与凭证高级设置", expanded=False):
    st.caption("以下留空即用平台内置默认配置，无需填写即可体验。")
    _CUSTOM = "输入自定义模型名"
    def _model_picker(label: str, options: list, default_idx: int, key: str) -> str:
        sel = st.selectbox(label, list(options) + [_CUSTOM], index=default_idx, key=key)
        if sel == _CUSTOM:
            custom = st.text_input(f"{label} · 自定义名称", key=key + "_custom", placeholder="填写模型标识，如 qwen3.7-max")
            return custom.strip() or options[min(default_idx, len(options) - 1)]
        return sel
    m_main = _model_picker("主力模型（拆解/适配）", MODEL_CANDIDATES, 0, "m_main")
    m_fast = _model_picker("快速模型（本地化/素材）", MODEL_CANDIDATES, 4, "m_fast")
    m_review = _model_picker("合规审查模型（跨厂商交叉审）", MODEL_CANDIDATES, 8, "m_review")
    m_image = _model_picker("图像模型（显式生成时用）", IMAGE_MODEL_CANDIDATES, 0, "m_image")
    st.divider()
    user_key = st.text_input("API Key", type="password", key="u_key", placeholder="留空使用平台默认额度")
    user_base = st.text_input("网关地址", key="u_base", placeholder="留空使用平台默认网关")
    use_custom = st.checkbox("强制使用以上自定义凭证", key="u_force")
    if use_custom and not (user_key.strip() and user_base.strip()):
        st.warning("已勾选自定义凭证，请同时填写 Key 和允许的 HTTPS 网关；填写完整前禁止调用。")


# ================================================================ 演示模式（P0-4）
result = st.session_state.get("result")
st.header("03 / 演示模式")
mode = st.radio("选择模式", ["small", "precomputed"],
                format_func=lambda k: {"small": "小批次真跑（默认 3 平台 × 2 语种）",
                                       "precomputed": "加载预生成全量"}[k],
                horizontal=True)

if mode == "precomputed":
    if PRECOMPUTED_PATH.exists():
        reload_precomputed = st.button("重新加载预生成文件（替换本会话结果）")
        if result is None or st.session_state.get("_result_source") != "precomputed" or reload_precomputed:
            try:
                result = json.loads(PRECOMPUTED_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                st.error("预生成文件暂不可读或格式损坏，请重新生成或稍后重试。")
                st.stop()
            st.session_state["result"] = result
            st.session_state["_result_source"] = "precomputed"
        loaded = result
        meta = loaded.get("meta", {})
        st.success(f"已加载离线预生成全量产物：生成时间 {meta.get('generated_at')}｜"
                   f"{len(meta.get('platforms', []))} 平台 × {len(meta.get('languages', []))} 语种｜"
                   f"耗时 {loaded.get('usage', {}).get('wall_time_s', '?')}s｜"
                   f"tokens {loaded.get('usage', {}).get('total_tokens', 0):,}｜"
                   f"API 估算 ¥{loaded.get('usage', {}).get('cost_estimate_cny', '?')}")
        st.caption("预生成 = 非现场时段离线真跑并存盘；现场展示缓存产物，需要新鲜结果请用「小批次真跑」或单条重跑。")
        st.session_state["result"] = loaded
        result = loaded
    else:
        st.warning(f"未找到预生成产物 {PRECOMPUTED_PATH}。请先离线执行一次：\n\n"
                   "```\nC:/Python314/python.exe precompute_full.py\n```\n\n"
                   "（预计数分钟到十几分钟；完成后再回到本页加载。）")
        if result is None:
            st.stop()

run_btn = st.button("开始生成", type="primary", use_container_width=True,
                    disabled=not (languages and platforms)) if mode == "small" else False

if run_btn:
    # 产品输入门禁（P0-2：白名单 + 本地敏感扫描；UI 自由文本也要过）
    product = {**SAMPLE_PRODUCT,
               "product_name": name, "product_name_en": name_en,
               "form": form, "deployment": deploy, "marketing_notes": notes,
               "target_languages": languages, "target_platforms": platforms,
               "compliance_level": level}
    try:
        product = validate_product(product)
    except PrivacyViolation as e:
        st.error(str(e))
        st.stop()
    if validate_all_configs():
        st.error("配置校验失败，请检查 config/*.yaml")
        st.stop()
    try:
        # 自定义凭证（可选）：勾选了「强制使用」且已填写时才覆盖平台默认
        _use_custom = bool(st.session_state.get("u_force")) and \
            bool((st.session_state.get("u_key") or "").strip()) and \
            bool((st.session_state.get("u_base") or "").strip())
        client = _get_client(
            base_url=st.session_state.get("u_base") if _use_custom else None,
            api_key=st.session_state.get("u_key") if _use_custom else None,
        )
    except RuntimeError as e:
        st.error(str(e))
        st.stop()

    st.subheader("生成进度（完成一条显示一条）")
    status = st.status(f"引擎运行中：{len(platforms)} 平台 × {len(languages)} 语言 …", expanded=True)
    live_box = st.container()
    with st.sidebar.status("环境", expanded=False):
        st.write(f"网关: {client.base_url}")
        st.write(f"Key: {mask_key(client.api_key)}")

    def cb(stage, done, total, msg):
        status.write(f"[{stage}] ({done}/{total}) {msg}")

    def icb(platform, language, item):
        icon = STATUS_ICON.get(item["status"], "STATUS")
        stats = item.get("stats", {})
        live_box.markdown(
            f"{icon} **{platform} × {language}** → {STATUS_LABELS.get(item['status'], item['status'])}"
            f"（{stats.get('latency_s', '?')}s / {stats.get('tokens', 0)} tok"
            + (f"｜{str(item.get('error'))[:100]}" if item.get("error") else "") + "）")

    try:
        pipeline = Pipeline(client, product, languages, platforms, level,
                            models={"main": m_main, "fast": m_fast, "review": m_review},
                            batch_deadline_s=float(deadline))
        result = pipeline.run(progress_cb=cb, item_cb=icb)
        u = result["usage"]
        status.update(label=f"运行结束（{u['wall_time_s']}s / {u['total_tokens']:,} tokens / "
                            f"≈¥{u['cost_estimate_cny']}）——可交付 {result['counts']['delivered']}/"
                            f"{result['counts']['total']}", state="complete", expanded=False)
        st.session_state["result"] = result
        st.session_state["_result_source"] = "small"
    except Exception as e:  # noqa: BLE001
        status.update(label="生成失败", state="error", expanded=True)
        st.error(f"{type(e).__name__}: {e}")
        st.stop()
    result = st.session_state.get("result")

# ================================================================ 价值看板（P2-14 同一快照）
st.header("④ 业务价值看板（参数为可调假设）")
if result:
    with st.expander("假设参数（可调）", expanded=False):
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            in_research = st.number_input("人工：产品研究（分钟/批）", 0, 480, DEFAULT_PARAMS["research_minutes"])
            in_prep = st.number_input("AI侧：资料准备（分钟/批）", 0, 480, DEFAULT_PARAMS["prep_minutes"])
        with col2:
            in_write = st.number_input("人工：平台基础撰写（分钟/平台）", 5, 240, DEFAULT_PARAMS["write_minutes_per_platform"])
            in_rework = st.number_input("AI侧：返修（分钟/未过项）", 0, 120, DEFAULT_PARAMS["rework_minutes_per_item"])
        with col3:
            in_l10n = st.number_input("人工：增量本地化（分钟/条）", 5, 240, DEFAULT_PARAMS["l10n_minutes_per_piece"])
            in_rate = st.number_input("综合时薪（元/小时）", 30, 2000, DEFAULT_PARAMS["hourly_rate_cny"])
        with col4:
            in_review = st.number_input("逐条终审（分钟/条，两侧同标准）", 5, 240, DEFAULT_PARAMS["review_minutes_per_piece"])
            in_monthly = st.number_input("月文案需求（条，假设外推）", 20, 600, DEFAULT_PARAMS["monthly_pieces"])
    params = {"research_minutes": in_research, "write_minutes_per_platform": in_write,
              "l10n_minutes_per_piece": in_l10n, "review_minutes_per_piece": in_review,
              "prep_minutes": in_prep, "rework_minutes_per_item": in_rework,
              "hourly_rate_cny": in_rate, "monthly_pieces": in_monthly}
    counts = result.get("counts", {})
    u = result.get("usage", {})
    v = compute_value(
        delivered_count=counts.get("delivered", 0), total_combos=counts.get("total", 0),
        blocked_count=counts.get("review_blocked", 0),
        failed_count=counts.get("failed", 0) + counts.get("skipped_deadline", 0),
        num_platforms=len(result.get("meta", {}).get("platforms", [])),
        num_languages=len(result.get("meta", {}).get("languages", [])),
        engine_seconds=u.get("wall_time_s", 0), engine_cost_cny=u.get("cost_estimate_cny", 0),
        markets_covered=(result.get("value") or {}).get("markets_covered", []),
        params=params)
    if not v.get("estimable"):
        st.error(v['note'])
    else:
        value_stats = [("人工侧工时（本批）", f"{v['manual_side']['hours_total']} h"),
                       ("AI 侧人工工时", f"{v['ai_side']['manual_hours_total']} h"),
                       ("节省工时", f"{v['savings']['hours_saved']} h"),
                       ("节省成本", f"¥{v['savings']['cost_saved_cny']:,.0f}"),
                       ("产能提升", f"×{v['savings']['capacity_multiplier']}")]
        st.markdown('<div class="stat-grid">' + ''.join(
            f'<div class="stat-card"><div class="stat-label">{_html.escape(str(label))}</div><div class="stat-value">{_html.escape(str(value))}</div></div>'
            for label, value in value_stats) + '</div>', unsafe_allow_html=True)
        st.caption(f"API 估算 ¥{v['ai_side']['api_cost_cny_estimate']}（估算值，非实际成本）｜"
                   f"市场覆盖 {v['markets_count']} 个（按实际可交付计，Global 不算国家）｜"
                   f"月度假设外推：省 ¥{v['monthly_projection']['cost_saved_cny_per_month']:,.0f}/月")
    with st.expander("口径说明（P1-6/P1-7 重做后的口径）"):
        st.write(v.get("assumption_note", ""))

    # 导出：与看板同一份快照（result + 当前参数 + 重算 value）
    ecol1, ecol2 = st.columns(2)
    with ecol1:
        st.download_button("⬇️ 导出 JSON（完整调试数据+当前参数）",
                           data=to_export_json(result, value=v, params=params),
                           file_name=f"copy_engine_result_{datetime.now():%Y%m%d_%H%M%S}.json",
                           mime="application/json", use_container_width=True)
    with ecol2:
        st.download_button("⬇️ 导出 Markdown 发布稿（只含审核后安全版）",
                           data=to_markdown(result, value=v, languages_cfg=LANGUAGES_CFG),
                           file_name=f"copy_engine_result_{datetime.now():%Y%m%d_%H%M%S}.md",
                           mime="text/markdown", use_container_width=True)
else:
    st.info("选择演示模式并生成或加载结果后，本看板出账。零成功时将如实显示「无有效产出，无法估算」。")

# ================================================================ 结果与统计
if result:
    if not result.get("usage", {}).get("usage_complete", False):
        st.markdown('<div class="notice warn">用量存在未知或历史未计量部分；当前金额只含已知文本估算，不可用于积分结算。</div>', unsafe_allow_html=True)
    render_result(result)

    # 图像生成（显式开启，默认不生，P0-4）
    st.header("⑤ 图像生成（显式开启，默认不生图）")
    if st.button("用首条可交付文案生成 1 张示例图（30-120s）"):
        try:
            client = _get_client().new_task()
            delivered = [r for r in result["results"] if r["status"] == "delivered"]
            if not delivered:
                st.warning("没有可交付文案，跳过生图")
            else:
                prompt = (delivered[0].get("asset_prompt") or {}).get("prompt_en") or \
                    "A pediatric resident doctor wearing a small smart badge device in a hospital ward"
                with st.spinner("生成中…"):
                    from core import ensure_output_dir
                    img = client.generate_image(prompt, ensure_output_dir() / f"image_{uuid4().hex}.png",
                                                model=m_image)
                update_result_usage(result, client)
                if img["ok"]:
                    st.success(f"成功：{img['model']} → {img['path']}")
                    st.image(img["path"], caption="示例生成图", use_container_width=True)
                else:
                    st.error(f"失败（如实标注）: {img['error']}")
        except Exception as e:  # noqa: BLE001
            st.error(f"{type(e).__name__}: {e}")

st.markdown('<div class="workspace-section">内容包浏览器</div>', unsafe_allow_html=True)
show_packages()
