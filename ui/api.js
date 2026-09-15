/* ═══════════════════════════════════════════════════════
   api.js — 轻量 API 封装（SPA 与后端同源同端口，Cookie 自动携带）
   仅用原生 fetch / EventSource，绝不使用 Authorization /
   X-modelscope-* / X-studio-* 等平台保留头（§7-1）。
   ═══════════════════════════════════════════════════════ */
const API = (function () {
  "use strict";

  async function request(method, url, opts) {
    opts = opts || {};
    const init = { method: method, credentials: "include", headers: {} };
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
    // SSE 订阅：返回原生 EventSource（GET + 同源 Cookie，天然满足合规约束）
    stream: function (url) {
      return new EventSource(url, { withCredentials: true });
    },
  };
})();
