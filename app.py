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
import logging
import os
import queue
import sys
import threading
import time
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

# ===== 产品化升级：认证 / 持久化 / 积分 / 隔离 / 管理（ARCHITECTURE_V2.md）=====
# 注意：以下模块在 import 阶段绝不连接数据库；DB 懒加载，仅在登录/注册/等受信路径触发。
from auth.hashing import hash_password, verify_password
from auth import session as auth_session
from dao.db import Database
from dao import users_dao, credits_dao, tasks_dao, audit_dao
from credits import pricing, service as credits_service
from tasks import service as tasks_service
from storage import space as storage_space
from admin import service as admin_service


# ================================================================ 认证 / 数据库 辅助
def ensure_db() -> None:
    """确保表结构已建 + 种子管理员已创建。仅在登录/注册/等受信路径调用，演示模式不触碰。"""
    Database.get().init_schema()
    ensure_seed_admin()


def ensure_seed_admin() -> None:
    """首次启动自动建种子管理员（环境变量 ADMIN_USERNAME/ADMIN_PASSWORD）。

    未设置环境变量时本地使用默认值 admin/admin 仅限本地，并在管理后台显示告警
    （ARCHITECTURE_V2.md §11-5 / 任务书硬约束 #7）。
    """
    username = os.environ.get("ADMIN_USERNAME")
    password = os.environ.get("ADMIN_PASSWORD")
    if not username or not password:
        username = "admin"
        password = "admin"
        st.session_state["_seed_default_admin"] = True
    else:
        st.session_state["_seed_default_admin"] = False
    if not users_dao.UserDAO.exists_username(username):
        users_dao.UserDAO.create(username, hash_password(password), role="admin")


def do_register(username: str, password: str) -> None:
    """注册：唯一性校验 + bcrypt 存密 + 注册赠送 + 自动登录。"""
    ensure_db()
    if not username or not password:
        st.error("用户名和密码不能为空")
        return
    if users_dao.UserDAO.exists_username(username):
        st.error("该用户名已被注册")
        return
    uid = users_dao.UserDAO.create(username, hash_password(password), role="user")
    credits_service.grant_signup(uid)
    auth_session.set_current_user(users_dao.UserDAO.get_by_id(uid))
    st.success(f"注册成功，已赠送 {pricing.SIGNUP_BONUS} 积分并自动登录")
    st.rerun()


def do_login(username: str, password: str) -> None:
    """登录：bcrypt 校验；错误统一提示；禁用用户拦截并落审计。"""
    ensure_db()
    user = users_dao.UserDAO.get_by_username(username)
    if user is None or not verify_password(password, user["password_hash"]):
        st.error("用户名或密码错误")
        return
    if user["status"] == "disabled":
        audit_dao.AuditDAO.append(user["user_id"], "login_blocked", user["user_id"],
                                  detail={"reason": "disabled"})
        st.error("该账号已被禁用，请联系管理员")
        return
    auth_session.set_current_user(user)
    st.rerun()


# ================================================================ 已登录用户视图
def render_recent_tasks(current: dict) -> None:
    """最近任务真实列表（R18 / D2）：来自 tasks 表，跨会话持久。"""
    ensure_db()
    st.header("最近任务")
    rows = tasks_service.recent_tasks(current["user_id"], limit=20)
    if not rows:
        st.info("暂无任务记录。在「新建生成任务」中完成一次生成后，这里会显示真实历史（跨会话保留）。")
        if st.button("← 返回工作台", key="back_tasks_empty"):
            st.session_state["_view"] = "generate"
            st.rerun()
        return
    for t in rows:
        summary = t.get("result_summary") or {}
        finished = t.get("finished_at") or t.get("created_at")
        with st.container(border=True):
            st.markdown(f"**{_html.escape(t['product_name'] or '未命名产品')}**"
                       f" · {len(t['languages'])} 语种 × {len(t['platforms'])} 平台")
            st.caption(f"状态：{t['status']} ｜ 完成时间：{finished} ｜ "
                       f"消耗积分：{t['credit_cost']} ｜ 退还：{t['credit_refunded']}")
            st.caption(f"语种：{', '.join(t['languages'])} ｜ 平台：{', '.join(t['platforms'])} ｜ "
                       f"概览：可交付 {summary.get('delivered', 0)} / 拦截 {summary.get('review_blocked', 0)}"
                       f" / 失败 {summary.get('failed', 0)} / 跳过 {summary.get('skipped_deadline', 0)}")
    if st.button("← 返回工作台", key="back_tasks"):
        st.session_state["_view"] = "generate"
        st.rerun()


def render_my_space(current: dict) -> None:
    """我的空间真实统计（R03 / R17 / E2）：上传数 / 产物数来自真实目录计数。"""
    ensure_db()
    st.header("我的空间")
    in_dir = storage_space.inputs_dir(current["user_id"])
    out_dir = storage_space.outputs_dir(current["user_id"])
    n_in = storage_space.count_files(in_dir)
    n_out = storage_space.count_files(out_dir)
    c1, c2, c3 = st.columns(3)
    c1.metric("上传资料", n_in)
    c2.metric("生成产物", n_out)
    c3.metric("当前积分", current["credits"])
    st.caption("你的上传与生成产物仅存于你自己的隔离目录（按整数用户 ID 拼装，用户名不进入路径）。")

    # E3 上传：经 storage.safe_join（secure_filename + 防穿越）落 inputs_dir，用户名/原文件名不进路径
    st.subheader("上传资料")
    _uploads = st.file_uploader(
        "上传产品资料（图片/文档，仅存于你的隔离目录）", accept_multiple_files=True,
        key="space_uploader")
    if _uploads:
        _saved = 0
        for _f in _uploads:
            try:
                storage_space.save_upload(current["user_id"], _f.name, _f.getvalue())
                _saved += 1
            except Exception as e:  # noqa: BLE001
                st.warning(f"文件「{_html.escape(_f.name)}」保存失败：{_friendly_error(e)}")
        if _saved:
            st.success(f"已保存 {_saved} 个文件到你的隔离目录")
            st.rerun()
    if st.button("← 返回工作台", key="back_space"):
        st.session_state["_view"] = "generate"
        st.rerun()


def render_admin(current: dict) -> None:
    """管理后台（R19 / R20 / G2）：用户管理 + 充值 + 审计。仅管理员可见。"""
    ensure_db()
    st.header("管理后台")
    if st.session_state.get("_seed_default_admin"):
        st.warning("⚠️ 当前使用默认管理员凭据（admin/admin）。生产环境必须通过环境变量 "
                   "ADMIN_USERNAME / ADMIN_PASSWORD 注入强口令，否则任何人可登录管理员。")

    # ---- 用户列表 + 禁用/启用 ----
    st.subheader("用户管理")
    users = admin_service.list_users()
    for u in users:
        col1, col2, col3, col4, col5 = st.columns([2, 2, 1, 1, 1])
        col1.write(u["username"])
        col2.write(u["created_at"])
        col3.write(u["status"])
        col4.write(u["credits"])
        if u["user_id"] != current["user_id"]:
            if u["status"] == "active":
                if col5.button("禁用", key=f"dis_{u['user_id']}"):
                    admin_service.disable_user(current["user_id"], u["user_id"], "管理员操作")
                    st.rerun()
            else:
                if col5.button("启用", key=f"ena_{u['user_id']}"):
                    admin_service.enable_user(current["user_id"], u["user_id"], "管理员操作")
                    st.rerun()

    # ---- 充值（用户名 + 额 + 备注必填） ----
    st.subheader("手动充值")
    with st.form("recharge_form", clear_on_submit=True):
        r_user = st.text_input("目标用户名", key="r_user")
        r_amount = st.number_input("充值积分（正整数）", min_value=1, step=1, value=100, key="r_amount")
        r_note = st.text_input("充值备注（必填）", key="r_note")
        submitted = st.form_submit_button("确认充值")
        if submitted:
            try:
                admin_service.recharge(current["user_id"], r_user, int(r_amount), r_note)
                st.success(f"已为 {r_user} 充值 {int(r_amount)} 积分")
                st.rerun()
            except ValueError as e:
                st.error(f"充值失败：{e}")

    # ---- 审计：任务流水 + 积分流水 ----
    st.subheader("审计 · 积分流水")
    ledger = credits_dao.CreditDAO.ledger_all(limit=200)
    if ledger:
        st.dataframe(
            [{"时间": r["created_at"], "用户ID": r["user_id"], "变动": r["change"],
              "类型": r["type"], "余额": r["balance_after"], "操作者": r.get("operator_id"),
              "备注": r.get("note") or ""} for r in ledger],
            use_container_width=True, hide_index=True)
    else:
        st.info("暂无积分流水")

    st.subheader("审计 · 操作日志")
    audits = admin_service.audit_list(limit=200)
    if audits:
        st.dataframe(
            [{"时间": a["created_at"], "操作者": a["operator_id"], "动作": a["action"],
              "目标用户": a.get("target_user_id"), "详情": _html.escape(str(a.get("detail") or ""))}
             for a in audits],
            use_container_width=True, hide_index=True)
    else:
        st.info("暂无管理操作记录")

    if st.button("← 返回工作台", key="back_admin"):
        st.session_state["_view"] = "generate"
        st.rerun()

st.set_page_config(page_title="跨境文案智造引擎 v3", page_icon="·", layout="wide")
THEME_PATH = Path(__file__).resolve().parent / "_theme.css"
st.markdown(f"<style>{THEME_PATH.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)
_top_cur = auth_session.get_current_user()
if _top_cur is None:
    _top_credits = ('<div class="topcredits">演示模式<span>登录解锁实时生成与积分</span></div>')
else:
    _top_credits = (f'<div class="topcredits">积分 <b>{_top_cur["credits"]:,}</b>'
                   f'<span>实时余额</span></div>')
st.markdown(f'<div class="topbar"><div class="topbrand"><span class="topmark">AI</span><span>跨境内容合规引擎</span><span class="topsub">生成工作台</span></div><div class="topspacer"></div>{_top_credits}<div class="top-avatar">帅</div></div>', unsafe_allow_html=True)

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
        st.error(_friendly_error(e))


# ================================================================ 顶部与侧栏壳层
with st.sidebar:
    st.markdown('<div class="sidebar-brand"><span class="sidebar-mark">AI</span><span>跨境内容合规引擎</span></div>', unsafe_allow_html=True)
    if st.button("+ 新建生成任务", use_container_width=True, type="primary"):
        st.session_state["_view"] = "generate"
        st.rerun()

    current = auth_session.get_current_user()
    if current is None:
        # 访客：仅演示入口 + 登录/注册引导（R21 / US-G1.AC3：不静默失败，引导登录）
        st.info("登录后解锁：实时生成 · 积分 · 我的空间 · 任务历史")
        with st.expander("登录 / 注册", expanded=False):
            _tab_login, _tab_reg = st.tabs(["登录", "注册"])
            with _tab_login:
                _lu = st.text_input("用户名", key="login_user")
                _lp = st.text_input("密码", type="password", key="login_pass")
                if st.button("登录", key="login_btn", use_container_width=True):
                    do_login(_lu, _lp)
            with _tab_reg:
                _ru = st.text_input("用户名", key="reg_user")
                _rp = st.text_input("密码", type="password", key="reg_pass")
                if st.button("注册并领取积分", key="reg_btn", use_container_width=True):
                    do_register(_ru, _rp)
    else:
        # 已登录：真实积分胶囊 + 导航 + 登出
        st.markdown(
            f'<div class="credits-pill"><span class="lbl">积分</span>'
            f'<span class="val">{current["credits"]:,}</span>'
            f'<span class="hint">实时余额</span></div>',
            unsafe_allow_html=True)
        if st.button("新建生成任务", key="nav_new", use_container_width=True):
            st.session_state["_view"] = "generate"
            st.rerun()
        if st.button("我的空间", key="nav_space", use_container_width=True):
            st.session_state["_view"] = "space"
            st.rerun()
        if st.button("最近任务", key="nav_tasks", use_container_width=True):
            st.session_state["_view"] = "tasks"
            st.rerun()
        if current["role"] == "admin":
            if st.button("管理后台", key="nav_admin", use_container_width=True):
                st.session_state["_view"] = "admin"
                st.rerun()
        if st.button("登出", key="nav_logout", use_container_width=True):
            auth_session.logout()
            st.session_state.pop("_view", None)
            st.rerun()

# ================================================================ 受信视图分页
# 已登录用户的可信功能（我的空间 / 最近任务 / 管理后台）以独立页面呈现，先于工作台渲染；
# 访客或默认工作台视图不进入此分支，保证演示模式零数据库访问。
_view = st.session_state.get("_view", "generate")
_current_user = auth_session.get_current_user()
if _current_user is not None and _view == "space":
    render_my_space(_current_user)
    st.stop()
if _current_user is not None and _view == "tasks":
    render_recent_tasks(_current_user)
    st.stop()
if _current_user is not None and _view == "admin":
    render_admin(_current_user)
    st.stop()

# ================================================================ 主区任务表单
name = name_en = form = deploy = notes = ""
st.markdown('<div class="task-head"><div><h1>新建生成任务</h1><p>输入产品资料，输出多语种合规内容包；发布前自动拦截违规表述。</p></div></div>', unsafe_allow_html=True)
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
    # 实时积分估算（R02 / C3）：按语种数 × 单价，真实余额来自会话
    _n_lang = len(languages)
    _estimate = _n_lang * pricing.PER_LANGUAGE
    _cur = auth_session.get_current_user()
    if _cur is None:
        st.markdown(
            f'<div class="cost-hint">预计消耗 <b>{_estimate} 积分</b>'
            f'（{_n_lang} 语种 × {pricing.PER_LANGUAGE}/语种）。'
            f'登录后解锁实时生成与积分账户。</div>',
            unsafe_allow_html=True)
    else:
        _after = _cur["credits"] - _estimate
        st.markdown(
            f'<div class="cost-hint">本次预计消耗 <b>{_estimate} 积分</b>'
            f'（{_n_lang} 语种 × {pricing.PER_LANGUAGE}/语种）。'
            f'当前余额 <b>{_cur["credits"]:,}</b>，执行后剩余 <b>{_after:,}</b>。</div>',
            unsafe_allow_html=True)

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
        # R07 预生成免责声明（含 generated_at，明确为离线缓存快照，非实时结果）
        st.warning("⚠️ 免责声明：当前展示的是离线预生成缓存快照（生成时间 "
                   f"{meta.get('generated_at') or '未知'}），仅供演示，不代表实时生成结果；"
                   "发布前请使用实时生成并复核合规口径。")
        st.session_state["result"] = loaded
        result = loaded
    else:
        st.warning(f"未找到预生成产物 {PRECOMPUTED_PATH}。请先离线执行一次：\n\n"
                   "```\nC:/Python314/python.exe precompute_full.py\n```\n\n"
                   "（预计数分钟到十几分钟；完成后再回到本页加载。）")
        if result is None:
            st.stop()


# ================================================================ 生成辅助（§8.2）
def _run_guest_generation(client, product, languages, platforms, level,
                          m_main, m_fast, m_review, deadline) -> None:
    """访客演示生成：同步跑 Pipeline，仅渲染进度，不扣积分/不落库（演示模式）。"""
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
        st.error(_friendly_error(e))
        st.stop()
    st.session_state["result"] = st.session_state.get("result")


def _start_logged_in_generation(current, client, product, languages, platforms, level,
                                m_main, m_fast, m_review, deadline) -> None:
    """登录用户：后台线程生成 + 预扣积分 + 取消 + 落库（ARCHITECTURE_V2.md §8.2）。"""
    ensure_db()
    cost = len(languages) * pricing.PER_LANGUAGE
    task_id = tasks_service.create_task(
        current["user_id"], product.get("product_name", ""), product.get("product_name_en", ""),
        languages, platforms, level)
    try:
        credits_service.pre_deduct(current["user_id"], cost, task_id=task_id)
    except credits_service.InsufficientCredits:
        # 余额不足：回滚建好的 pending 任务，避免脏数据
        tasks_dao.TaskDAO.finish(task_id, "failed", credit_cost=0, credit_refunded=0,
                                 result_summary=None, result_path=None)
        st.error(f"积分不足：本次预计消耗 {cost} 积分，当前余额 {current['credits']}。"
                 f"请到「管理后台」或联系管理员充值后重试。")
        st.stop()
    tasks_dao.TaskDAO.set_running(task_id)

    pipeline = Pipeline(client, product, languages, platforms, level,
                        models={"main": m_main, "fast": m_fast, "review": m_review},
                        batch_deadline_s=float(deadline))
    q: "queue.Queue" = queue.Queue()
    user_id = current["user_id"]

    def _q_cb(stage, done, total, msg):
        # 子线程禁止直接写 Streamlit 元素，仅投递队列（§8.2）
        q.put(f"[{stage}] ({done}/{total}) {msg}")

    def _q_icb(platform, language, item):
        icon = STATUS_ICON.get(item["status"], "STATUS")
        stats = item.get("stats", {})
        q.put(f"{icon} **{platform} × {language}** → {STATUS_LABELS.get(item['status'], item['status'])}"
              f"（{stats.get('latency_s', '?')}s / {stats.get('tokens', 0)} tok"
              + (f"｜{str(item.get('error'))[:100]}" if item.get("error") else "") + "）")

    def _worker():
        try:
            res = pipeline.run(progress_cb=_q_cb, item_cb=_q_icb)
            q.put(("done", res))
        except Exception as e:  # noqa: BLE001
            q.put(("error", e))

    st.session_state["_gen_queue"] = q
    st.session_state["_gen_pipeline"] = pipeline
    st.session_state["_gen_task_id"] = task_id
    st.session_state["_gen_cost"] = cost
    st.session_state["_gen_user_id"] = user_id
    st.session_state["_gen_log"] = []
    st.session_state["_gen_final"] = None
    thread = threading.Thread(daemon=True, target=_worker)
    st.session_state["_gen_thread"] = thread
    thread.start()


def _render_generation_monitor() -> None:
    """轮询后台生成队列并渲染；提供取消按钮；结束后落库 + 退还。"""
    thread = st.session_state.get("_gen_thread")
    if thread is None:
        return
    q = st.session_state["_gen_queue"]
    while not q.empty():
        msg = q.get()
        if isinstance(msg, tuple) and msg[0] in ("done", "error"):
            st.session_state["_gen_final"] = msg
        else:
            st.session_state["_gen_log"].append(msg)

    st.subheader("生成进度（后台运行中，完成一条显示一条）")
    with st.status("引擎运行中 …（可点「取消生成」中止，取消后全额退还积分）", expanded=True) as s:
        for m in st.session_state.get("_gen_log", []):
            s.write(m)

    final = st.session_state.get("_gen_final")
    if final is None:
        if st.button("取消生成", key="cancel_gen", type="primary", use_container_width=True):
            pl = st.session_state.get("_gen_pipeline")
            if pl is not None:
                pl.request_cancel()
            st.rerun()
        time.sleep(1.0)
        st.rerun()
        return

    _finalize_gen(final)


def _finalize_gen(final) -> None:
    """线程结束后落库 + 按状态退还积分（§8.2）。"""
    kind, payload = final
    user_id = st.session_state.get("_gen_user_id")
    task_id = st.session_state.get("_gen_task_id")
    cost = st.session_state.get("_gen_cost")
    try:
        if kind == "done":
            result = payload
            cancelled = bool(result.get("meta", {}).get("cancelled"))
            status = "cancelled" if cancelled else "completed"
            refunded = cost if cancelled else 0
            tasks_service.finalize_task(user_id, task_id, status,
                                        credit_cost=cost, credit_refunded=refunded, result=result)
            if cancelled:
                credits_service.refund(user_id, task_id, cost)
            st.session_state["result"] = result
            st.session_state["_result_source"] = "small"
            st.session_state["_gen_error_msg"] = None
        else:
            err = payload
            tasks_service.finalize_task(user_id, task_id, "failed",
                                        credit_cost=cost, credit_refunded=cost)
            credits_service.refund(user_id, task_id, cost)
            st.session_state["result"] = None
            st.session_state["_gen_error_msg"] = _friendly_error(err)
    except Exception as e:  # noqa: BLE001
        st.session_state["_gen_error_msg"] = _friendly_error(e)
    finally:
        for k in ("_gen_thread", "_gen_pipeline", "_gen_queue", "_gen_task_id",
                  "_gen_cost", "_gen_user_id", "_gen_log", "_gen_final"):
            st.session_state.pop(k, None)
        try:
            auth_session.refresh_credits(credits_service.get_balance(user_id))
        except Exception:
            pass
    st.rerun()


def _friendly_error(e: Exception) -> str:
    """把异常翻译成中文友好提示（R09：不向页面吐 traceback）。原始异常仅服务端 logging。"""
    logging.getLogger("app").exception("generation error")
    name = type(e).__name__
    msg = str(e).lower()
    if "callstopped" in name or "cancel" in msg:
        return "已取消生成。"
    if "connection" in name or "timeout" in name or "timeout" in msg or "timed out" in msg:
        return "生成失败：模型服务暂时不可用或网络超时，请稍后重试。"
    if "api_key" in msg or "401" in msg or "403" in msg or "unauthorized" in msg:
        return "生成失败：模型服务鉴权失败，请检查网关与凭证。"
    return "生成失败：服务暂时不可用，请稍后重试。"


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

    current = auth_session.get_current_user()
    if current is None:
        # 访客：同步演示生成（不触碰数据库 / 积分，演示模式零 DB 访问）
        _run_guest_generation(client, product, languages, platforms, level,
                              m_main, m_fast, m_review, deadline)
    else:
        # 登录用户：后台线程生成 + 预扣积分 + 取消 + 落库（§8.2）
        _start_logged_in_generation(current, client, product, languages, platforms, level,
                                    m_main, m_fast, m_review, deadline)
        st.rerun()


# ================================================================ 后台生成监控
if st.session_state.get("_gen_thread") is not None:
    _render_generation_monitor()
elif st.session_state.get("_gen_error_msg"):
    st.error(st.session_state["_gen_error_msg"])
    st.session_state.pop("_gen_error_msg", None)

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
    # R08 导出文件名：{product}_{langN}lang_{ts}.json/.md（文件名可区分、可复现）
    _export_langs = result.get("meta", {}).get("languages", [])
    _export_base = (f"{storage_space.secure_filename(name or 'product', max_len=40)}"
                    f"_{len(_export_langs)}lang_{datetime.now():%Y%m%d_%H%M%S}")
    ecol1, ecol2 = st.columns(2)
    with ecol1:
        st.download_button("⬇️ 导出 JSON（完整调试数据+当前参数）",
                           data=to_export_json(result, value=v, params=params),
                           file_name=f"{_export_base}.json",
                           mime="application/json", use_container_width=True)
    with ecol2:
        st.download_button("⬇️ 导出 Markdown 发布稿（只含审核后安全版）",
                           data=to_markdown(result, value=v, languages_cfg=LANGUAGES_CFG),
                           file_name=f"{_export_base}.md",
                           mime="text/markdown", use_container_width=True)
else:
    st.info("选择演示模式并生成或加载结果后，本看板出账。零成功时将如实显示「无有效产出，无法估算」。")

# ================================================================ 结果与统计
if result:
    if not result.get("usage", {}).get("usage_complete", False):
        st.markdown('<div class="notice warn">用量存在未知或历史未计量部分；当前金额只含已知文本估算，不可用于积分结算。</div>', unsafe_allow_html=True)
    render_result(result)

    # 图像生成（显式开启，默认不生，P0-4）
    # E3：登录用户产物落 outputs_dir（隔离目录），并按张预扣 PER_IMAGE（取消/失败退还）。
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
                _img_user = auth_session.get_current_user()
                _need_refund = False
                if _img_user is not None:
                    ensure_db()
                    try:
                        credits_service.pre_deduct(_img_user["user_id"], pricing.PER_IMAGE)
                    except credits_service.InsufficientCredits:
                        st.error(f"积分不足：生图需 {pricing.PER_IMAGE} 积分，请充值后重试。")
                        st.stop()
                    _img_path = storage_space.outputs_dir(_img_user["user_id"]) / f"image_{uuid4().hex}.png"
                    _need_refund = True
                else:
                    from core import ensure_output_dir
                    _img_path = ensure_output_dir() / f"image_{uuid4().hex}.png"
                with st.spinner("生成中…"):
                    img = client.generate_image(prompt, _img_path, model=m_image)
                update_result_usage(result, client)
                if img["ok"]:
                    if _need_refund:
                        # 成功：不退还（已消费 10 积分）
                        try:
                            auth_session.refresh_credits(
                                credits_service.get_balance(_img_user["user_id"]))
                        except Exception:
                            pass
                    st.success(f"成功：{img['model']} → {img['path']}")
                    st.image(img["path"], caption="示例生成图", use_container_width=True)
                else:
                    if _need_refund:
                        # 图像失败退还：task_id=None（图像未绑定任务），无法按任务去重，
                        # 保持单次行为；生图按钮单次触发，UI 侧保证不会重复执行。
                        credits_service.refund(_img_user["user_id"], None, pricing.PER_IMAGE)
                    st.error(f"失败（如实标注）: {img['error']}")
        except Exception as e:  # noqa: BLE001
            st.error(_friendly_error(e))

st.markdown('<div class="workspace-section">内容包浏览器</div>', unsafe_allow_html=True)
show_packages()
