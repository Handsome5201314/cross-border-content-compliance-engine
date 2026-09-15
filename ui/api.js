/* ═══════════════════════════════════════════════════════
   api.js — 轻量 API 封装（SPA 与后端同源同端口）
   会话令牌双通道：
   ① Cookie（第一方场景自动携带）
   ② X-Session-Token 头（iframe 内浏览器拒存第三方 Cookie 时的主通道，
      令牌存 localStorage——存储分区不影响同帧读写）
   SSE 的 EventSource 无法带自定义头 → 令牌走 ?token= 查询参数。
   仅用原生 fetch / EventSource，绝不使用 Authorization /
   X-modelscope-* / X-studio-* 等平台保留头（§7-1）。
   ═══════════════════════════════════════════════════════ */
const API = (function () {
  "use strict";

  var TOKEN_KEY = "cce_session_token";

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
  }
  function setToken(t) {
    try {
      if (t) localStorage.setItem(TOKEN_KEY, t);
      else localStorage.removeItem(TOKEN_KEY);
    } catch (e) { /* 无痕模式等存储不可用：静默降级到 Cookie 通道 */ }
  }

  async function request(method, url, opts) {
    opts = opts || {};
    const init = { method: method, credentials: "include", headers: {} };
    const t = getToken();
    if (t) init.headers["X-Session-Token"] = t; // 后端优先级：头 > ?token= > Cookie
    if (opts.body !== undefined) {
      if (opts.isForm) {
        init.body = opts.body; // FormData，浏览器自动设 boundary
      } else {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(opts.body);
      }
    }
    const resp = await fetch(url, init);
    let data = null;
    const ct = resp.headers.get("content-type") || "";
    if (ct.indexOf("application/json") >= 0) {
      try { data = await resp.json(); } catch (e) { data = null; }
    }
    // 登录/注册响应携带令牌：写入 localStorage 供后续请求与 SSE 使用
    if (resp.ok && data && data.session_token) setToken(data.session_token);
    if (!resp.ok) {
      let detail = "请求失败 (" + resp.status + ")";
      if (data) {
        const d = data.detail || data.error || data.message;
        if (typeof d === "string") detail = d;
        else if (d) detail = JSON.stringify(d);
      }
      const err = new Error(detail);
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  return {
    get: function (url) { return request("GET", url); },
    post: function (url, body) { return request("POST", url, { body: body }); },
    postForm: function (url, form) { return request("POST", url, { body: form, isForm: true }); },
    clearToken: function () { setToken(""); },
    // SSE 订阅：EventSource 无法带自定义头 → 令牌走查询参数（后端 deps 兼容）
    stream: function (url) {
      const t = getToken();
      const sep = url.indexOf("?") >= 0 ? "&" : "?";
      const full = t ? (url + sep + "token=" + encodeURIComponent(t)) : url;
      return new EventSource(full, { withCredentials: true });
    },
  };
})();
