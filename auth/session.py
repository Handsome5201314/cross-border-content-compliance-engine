# -*- coding: utf-8 -*-
"""Streamlit 会话管理（ARCHITECTURE_V2.md §6.1 / §6.2）。

登录态跨 rerun 持久（同一浏览器会话）。所有键以 `auth_` 前缀统一，避免与现有
session_state（如 `result`、`client`）冲突。本模块仅在 Streamlit 运行时被调用。
"""
import streamlit as st

_KEY_ID = "auth_user_id"
_KEY_USERNAME = "auth_username"
_KEY_ROLE = "auth_role"
_KEY_CREDITS = "auth_credits"


def get_current_user() -> dict | None:
    """返回当前登录用户摘要字典；未登录返回 None。"""
    uid = st.session_state.get(_KEY_ID)
    if uid is None:
        return None
    return {
        "user_id": uid,
        "username": st.session_state.get(_KEY_USERNAME),
        "role": st.session_state.get(_KEY_ROLE, "user"),
        "credits": st.session_state.get(_KEY_CREDITS, 0),
    }


def set_current_user(user: dict) -> None:
    """写入登录态（注册自动登录 / 登录成功时调用）。"""
    st.session_state[_KEY_ID] = user["user_id"]
    st.session_state[_KEY_USERNAME] = user["username"]
    st.session_state[_KEY_ROLE] = user.get("role", "user")
    st.session_state[_KEY_CREDITS] = user.get("credits", 0)


def refresh_credits(credits: int) -> None:
    """余额变动后刷新缓存（供侧栏胶囊实时显示）。"""
    st.session_state[_KEY_CREDITS] = credits


def logout() -> None:
    """清除登录态，回到访客态。"""
    for k in (_KEY_ID, _KEY_USERNAME, _KEY_ROLE, _KEY_CREDITS):
        st.session_state.pop(k, None)


def is_admin() -> bool:
    return st.session_state.get(_KEY_ROLE) == "admin"


def is_authenticated() -> bool:
    return st.session_state.get(_KEY_ID) is not None
