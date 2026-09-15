/* ═══════════════════════════════════════════════════════
   app.js — 三视图调度 + 全部前端接线（V3 像素还原版）
   视图：#app-guest（访客）/ #app-user（用户）/ #app-admin（管理）
   皮肤：?skin=slate|amber|paper，切换只改 data-skin（8 个 CSS 变量）
   会话：itsdangerous 签名 Cookie（cce_session），后端判定角色
   ═══════════════════════════════════════════════════════ */
(function () {
  "use strict";

  /* ---------- 工具 ---------- */
  const $ = (id) => document.getElementById(id);
  const qsa = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        if (k === "text") node.textContent = attrs[k];
        else if (k === "html") node.innerHTML = attrs[k];
        else if (k === "class") node.className = attrs[k];
        else if (k.startsWith("on") && typeof attrs[k] === "function")
          node.addEventListener(k.slice(2), attrs[k]);
        else if (attrs[k] !== null && attrs[k] !== undefined)
          node.setAttribute(k, attrs[k]);
      }
    }
    if (children) {
      (Array.isArray(children) ? children : [children]).forEach((c) => {
        if (c == null) return;
        node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
      });
    }
    return node;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  let _toastTimer = null;
  function toast(msg, type) {
    const t = $("toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "toast on" + (type ? " " + type : "");
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => { t.className = "toast"; }, 3200);
  }

  function fmtInt(n) {
    n = Number(n) || 0;
    return n.toLocaleString("en-US");
  }
  function fmtSize(bytes) {
    bytes = Number(bytes) || 0;
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  /* ---------- 图标注入 ---------- */
  function injectIcons() {
    qsa("[data-icon]").forEach((node) => {
      const name = node.getAttribute("data-icon");
      node.innerHTML = (typeof mxicon === "function") ? mxicon(name) : "";
    });
  }

  /* ---------- 皮肤 ---------- */
  const SKINS = ["slate", "amber", "paper"];
  function applySkin(skin) {
    if (SKINS.indexOf(skin) < 0) skin = "slate";
    document.documentElement.setAttribute("data-skin", skin);
    qsa("[data-skinbtn]").forEach((b) => {
      b.classList.toggle("on", b.getAttribute("data-skinbtn") === skin);
    });
    try { localStorage.setItem("cce_skin", skin); } catch (e) {}
  }
  function initSkin() {
    const params = new URLSearchParams(location.search);
    let skin = params.get("skin");
    if (!skin) { try { skin = localStorage.getItem("cce_skin"); } catch (e) {} }
    applySkin(skin || "slate");
    qsa("[data-skinbtn]").forEach((b) => {
      b.addEventListener("click", () => applySkin(b.getAttribute("data-skinbtn")));
    });
  }

  /* ---------- 视图切换 ---------- */
  function showView(name) {
    ["guest", "user", "admin"].forEach((v) => {
      const node = $("app-" + v);
      if (node) node.classList.toggle("on", v === name);
    });
    window.scrollTo(0, 0);
  }

  /* ---------- 全局状态 ---------- */
  const State = {
    user: null,          // {user_id, username, role, credits, status}
    perLanguage: 5,      // 预热自 /api/value-board/params
    valueParams: null,
    activeTaskId: null,
    es: null,            // 当前 EventSource
  };

  /* ════════════ 会话 / 角色 ════════════ */
  async function bootstrap() {
    injectIcons();
    initSkin();
    await loadValueParams();
    try {
      const me = await API.get("/api/auth/me");
      State.user = me;
      onSessionReady(me);
    } catch (e) {
      // 401 → 访客
      State.user = null;
      enterGuest();
    }
  }

  function onSessionReady(me) {
    if (me.role === "admin") {
      enterAdmin(me);
    } else {
      enterUser(me);
    }
  }

  /* ════════════ 访客视图 ════════════ */
  let _guestLoaded = false;
  function enterGuest() {
    showView("guest");
    if (!_guestLoaded) {
      _guestLoaded = true;
      loadGuestData();
    }
    // 访客注册 / 新建任务 → 打开注册弹层
    const register = () => openAuth("register");
    const hook = (id) => { const n = $(id); if (n) n.addEventListener("click", register); };
    hook("btn-guest-register");
    hook("btn-guest-register2");
    hook("btn-guest-newtask");
    // 查看合规裁决示例 → 滚动到裁决区
    const verdictNav = $("guest-item-verdict");
    if (verdictNav) verdictNav.addEventListener("click", () => {
      const v = $("guest-verdicts"); if (v) v.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  async function loadGuestData() {
    try {
      const [pre, params] = await Promise.all([
        API.get("/api/demo/precomputed"),
        API.get("/api/value-board/params").catch(() => null),
      ]);
      renderGuestValueBoard(pre, params);
      renderGuestVerdicts(pre);
      const pill = $("guest-pill");
      if (pill) {
        const total = (pre.counts && pre.counts.total) || (pre.value && pre.value.counts && pre.value.counts.total) || 0;
        pill.textContent = "真实案例 · " + total + " 组合";
      }
    } catch (e) {
      console.warn("访客数据加载失败", e);
    }
  }

  function renderGuestValueBoard(pre, params) {
    const c = (pre.counts) || (pre.value && pre.value.counts) || {};
    const set = (id, val) => { const n = $(id); if (n) n.textContent = (val == null ? "–" : fmtInt(val)); };
    set("gv-delivered", c.delivered);
    set("gv-blocked", c.review_blocked);
    set("gv-failed", c.failed);
    let markets = 0;
    if (pre.product && Array.isArray(pre.product.target_markets)) markets = pre.product.target_markets.length;
    else if (pre.value && pre.value.markets_count != null) markets = pre.value.markets_count;
    set("gv-markets", markets);
  }

  function renderGuestVerdicts(pre) {
    const box = $("guest-verdicts");
    if (!box) return;
    const results = (pre.results || []).slice();
    // 选有裁决（命中）与已交付（放行）各若干，呈现「同 100% 两种判决」对比
    const blocked = results.filter((r) => (r.compliance && (r.compliance.confirmed_findings || []).length));
    const passed = results.filter((r) => r.status === "delivered" && (!r.compliance || !(r.compliance.confirmed_findings || []).length));
    const picks = blocked.slice(0, 5).concat(passed.slice(0, 3));
    if (!picks.length) { box.innerHTML = ""; return; }
    box.innerHTML = "";
    picks.forEach((r) => box.appendChild(verdictCard(r.platform, r.language, r.compliance || {}, { live: false })));
  }

  /* ════════════ 统一裁决卡渲染（访客 + 实时共用） ════════════ */
  function verdictCard(platform, language, compliance, opts) {
    opts = opts || {};
    const findings = (compliance.confirmed_findings || []);
    const benign = (compliance.benign_verdicts || compliance.benign || []);
    const safe = compliance.safe_copy || {};
    const blocked = findings.length > 0;

    const card = el("div", { class: "verdict " + (blocked ? "block" : "pass") });
    // 头部：平台 / 语种 + 状态名
    const vname = el("span", { class: "vname", text: (platform || "?") + " · " + (language || "?").toUpperCase() });
    const vtitle = el("div", { class: "vtitle", text: blocked ? "拦截：命中需人工复核表述" : "放行：未发现违规表述" });
    card.appendChild(el("div", { class: "vh" }, [vname, vtitle]));

    if (blocked) {
      findings.slice(0, 4).forEach((f) => {
        const term = f.term || f.source || "—";
        const sev = (f.severity || "medium").toUpperCase();
        const reason = f.reason || "";
        const adj = f.adjudication || "";
        const sent = el("div", { class: "vsent" });
        sent.appendChild(el("span", { class: "hl", text: String(term) }));
        const meta = el("div", { class: "vmeta" });
        meta.appendChild(el("div", {}, [el("b", { text: "严重性 " }), document.createTextNode(sev)]));
        if (reason) meta.appendChild(el("div", { text: "原因：" + reason }));
        if (adj) meta.appendChild(el("div", { class: "vfix", text: "裁定：" + adj }));
        card.appendChild(sent);
        card.appendChild(meta);
      });
    } else {
      const title = (safe.titles && safe.titles[0]) || "";
      const desc = (safe.descriptions && safe.descriptions[0]) || "";
      if (title || desc) {
        const sent = el("div", { class: "vsent", text: (title ? "✓ " + title + "\n" : "") + desc });
        card.appendChild(sent);
      }
      benign.slice(0, 2).forEach((b) => {
        const txt = (b.term || b.reason || b.text || "");
        if (txt) card.appendChild(el("div", { class: "vmeta", text: "无风险：" + txt }));
      });
      if (compliance.disclaimer)
        card.appendChild(el("div", { class: "vmeta", text: "免责：" + compliance.disclaimer }));
    }
    return card;
  }

  /* ════════════ 登录 / 注册 弹层 ════════════ */
  function openAuth(tab) {
    const ov = $("auth-overlay");
    if (!ov) return;
    setAuthTab(tab || "login");
    ov.classList.add("on");
  }
  function closeAuth() { const ov = $("auth-overlay"); if (ov) ov.classList.remove("on"); }
  function setAuthTab(tab) {
    const isReg = tab === "register";
    const tLogin = $("tab-login"), tReg = $("tab-register");
    if (tLogin) tLogin.classList.toggle("on", !isReg);
    if (tReg) tReg.classList.toggle("on", isReg);
    const title = $("auth-title"); if (title) title.textContent = isReg ? "注册" : "登录";
    const sub = $("auth-sub");
    if (sub) sub.textContent = isReg
      ? "注册即送 100 积分，立即解锁实时生成 / 我的空间"
      : "登录后解锁实时生成 / 积分 / 我的空间 / 任务历史";
    const submit = $("btn-auth-submit"); if (submit) submit.textContent = isReg ? "注册并登录" : "登录";
    hideAuthErr();
  }
  function showAuthErr(msg) {
    const e = $("auth-err"), t = $("auth-err-text");
    if (t) t.textContent = msg || "操作失败";
    if (e) e.classList.add("on");
  }
  function hideAuthErr() { const e = $("auth-err"); if (e) e.classList.remove("on"); }

  async function submitAuth() {
    const tab = $("tab-register") && $("tab-register").classList.contains("on") ? "register" : "login";
    const username = ($("auth-username") || {}).value || "";
    const password = ($("auth-password") || {}).value || "";
    hideAuthErr();
    if (!username.trim() || !password) { showAuthErr("用户名和密码不能为空"); return; }
    const btn = $("btn-auth-submit"); if (btn) btn.disabled = true;
    try {
      const data = await API.post("/api/auth/" + tab, { username: username.trim(), password: password });
      State.user = data;
      closeAuth();
      toast(tab === "register" ? "注册成功，已登录" : "登录成功", "ok");
      onSessionReady(data);
    } catch (e) {
      showAuthErr(e.message || "操作失败");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function logout() {
    try { await API.post("/api/auth/logout", {}); } catch (e) {}
    State.user = null;
    State.es = null;
    enterGuest();
    toast("已登出");
  }

  /* ════════════ 用户视图 ════════════ */
  let _userLoaded = false;
  function enterUser(me) {
    showView("user");
    const av = $("u-avatar"); if (av) av.textContent = (me.username || "?").slice(0, 1).toUpperCase();
    if (!_userLoaded) {
      _userLoaded = true;
      wireUser();
    }
    refreshUser();
  }

  async function refreshUser() {
    try {
      const [bal, stats, recent] = await Promise.all([
        API.get("/api/credits/balance"),
        API.get("/api/space/stats"),
        API.get("/api/tasks/recent?limit=20"),
      ]);
      const cred = (bal && bal.credits != null) ? bal.credits : (State.user ? State.user.credits : 0);
      setCredits(cred);
      setSpaceStats(stats);
      renderRecent(recent && recent.tasks);
    } catch (e) { console.warn("用户数据刷新失败", e); }
    updateCostHint();
  }

  function setCredits(c) {
    const n = $("u-credits");
    if (n) {
      n.textContent = fmtInt(c);
      n.classList.remove("low", "out");
      if (c <= 0) n.classList.add("out");
      else if (c < State.perLanguage) n.classList.add("low");
    }
  }
  function setSpaceStats(stats) {
    if (!stats) return;
    const set = (id, v) => { const n = $(id); if (n) n.textContent = fmtInt(v); };
    set("cnt-files", (stats.inputs || 0) + (stats.outputs || 0));
    set("cnt-uploads", stats.uploads || 0);
    set("cnt-outputs", stats.outputs || 0);
  }

  function renderRecent(tasks) {
    const box = $("recent-tasks");
    if (!box) return;
    if (!tasks || !tasks.length) {
      box.innerHTML = "";
      box.appendChild(el("div", { class: "item" }, [el("span", { class: "nm", text: "暂无任务，点上方「新建生成任务」开始" })]));
      return;
    }
    box.innerHTML = "";
    tasks.forEach((t) => {
      const st = t.status;
      const tagCls = st === "completed" || st === "delivered" ? "pass" : (st === "failed" || st === "cancelled" ? "block" : "warn");
      const label = st === "completed" ? "已交付" : st === "failed" ? "失败" : st === "cancelled" ? "已取消" : st === "running" ? "进行中" : st;
      const item = el("div", { class: "item" }, [
        el("span", { class: "dot " + (tagCls === "pass" ? "pass" : tagCls === "block" ? "block" : "") }),
        el("span", { class: "nm", text: t.product_name || ("任务 #" + t.task_id) }),
        el("span", { class: "tag " + tagCls, text: label }),
      ]);
      if (st === "completed" || st === "cancelled" || st === "failed") {
        item.style.cursor = "pointer";
        item.addEventListener("click", () => { window.location.href = "#task-" + t.task_id; toast("任务 #" + t.task_id + "（" + label + "）"); });
      }
      box.appendChild(item);
    });
  }

  /* ----- 用户视图事件接线 ----- */
  function wireUser() {
    ["btn-focus-form", "btn-recharge-hint"].forEach((id) => {
      const n = $(id); if (n) n.addEventListener("click", () => {
        const f = $("p-name"); if (f) f.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    });
    const logoutBtn = $("btn-logout-user"); if (logoutBtn) logoutBtn.addEventListener("click", logout);

    // 语种选择 chip
    qsa("#lang-chips .chip, #plat-chips .chip").forEach((chip) => {
      chip.addEventListener("click", () => { chip.classList.toggle("on"); updateCostHint(); });
    });

    const genBtn = $("btn-generate"); if (genBtn) genBtn.addEventListener("click", startGeneration);
    const cancelBtn = $("btn-cancel"); if (cancelBtn) cancelBtn.addEventListener("click", cancelGeneration);
    const draftBtn = $("btn-draft"); if (draftBtn) draftBtn.addEventListener("click", () => toast("草稿功能暂未开放（演示）"));

    // 文件抽屉
    const viewFiles = (kind) => openFiles(kind);
    const vf = $("btn-view-files"); if (vf) vf.addEventListener("click", () => viewFiles("inputs"));
    const vu = $("btn-view-uploads"); if (vu) vu.addEventListener("click", () => viewFiles("inputs"));
    const vo = $("btn-view-outputs"); if (vo) vo.addEventListener("click", () => viewFiles("outputs"));
    const fClose = $("btn-files-close"); if (fClose) fClose.addEventListener("click", closeFiles);
    const fileInput = $("file-input");
    if (fileInput) fileInput.addEventListener("change", uploadFile);

    // 积分明细抽屉
    ["btn-ledger", "btn-ledger-top", "btn-ledger-recharge"].forEach((id) => {
      const n = $(id); if (n) n.addEventListener("click", () => {
        if (id === "btn-ledger-recharge") openLedger("充值记录"); else openLedger("积分明细");
      });
    });
    const lClose = $("btn-ledger-close"); if (lClose) lClose.addEventListener("click", () => { $("ledger-drawer").classList.remove("on"); });

    const priv = $("btn-privacy"); if (priv) priv.addEventListener("click", () => toast("你的数据存于独立空间，仅你本人可读写"));
  }

  function selectedLangChips() {
    return qsa("#lang-chips .chip.on").map((c) => c.getAttribute("data-value"));
  }
  function selectedPlatChips() {
    return qsa("#plat-chips .chip.on").map((c) => c.getAttribute("data-value"));
  }

  function updateCostHint() {
    const langs = selectedLangChips();
    const cost = langs.length * State.perLanguage;
    const hint = document.querySelector("#cost-hint b");
    const kvBal = $("kv-balance");
    const bal = State.user ? State.user.credits : 0;
    const nodes = qsa("#cost-hint b");
    if (nodes.length >= 2) {
      nodes[0].textContent = fmtInt(cost) + " 积分";
      nodes[1].textContent = fmtInt(bal) + " 积分";
    }
    if (kvBal) kvBal.textContent = fmtInt(bal);
  }

  async function loadValueParams() {
    try {
      const p = await API.get("/api/value-board/params");
      if (p) {
        State.valueParams = p;
        if (p.per_language) State.perLanguage = p.per_language;
      }
    } catch (e) { /* 使用默认 5 */ }
  }

  /* ----- 生成流程 + SSE ----- */
  async function startGeneration() {
    const langs = selectedLangChips();
    const plats = selectedPlatChips();
    if (!langs.length || !plats.length) { toast("请至少选择一个语种与一个平台", "err"); return; }

    const marketRaw = ($("p-market") || {}).value || "";
    const markets = marketRaw
      .split(/[·/，、,]/).map((s) => s.trim()).filter(Boolean);

    const body = {
      product_name: ($("p-name") || {}).value || "",
      product_name_en: ($("p-name-en") || {}).value || "",
      form: ($("p-form") || {}).value || "",
      deployment: "",
      marketing_notes: ($("p-notes") || {}).value || "",
      target_markets: markets,
      languages: langs,
      platforms: plats,
      compliance_level: "strict",
      deadline_s: 900,
      models: {},
    };
    if (!body.product_name) { toast("请填写产品名", "err"); return; }

    const btn = $("btn-generate"); if (btn) btn.disabled = true;
    try {
      const r = await API.post("/api/generate", body);
      State.activeTaskId = r.task_id;
      toast("已创建任务 #" + r.task_id + "，开始生成", "ok");
      beginStream(r.task_id, langs.length * plats.length, r.cost);
    } catch (e) {
      if (e.status === 402) toast("积分不足：" + e.message, "err");
      else if (e.status === 409) toast(e.message, "err");
      else if (e.status === 422) toast(e.message, "err");
      else toast(e.message || "生成创建失败", "err");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function beginStream(taskId, totalCombos, cost) {
    // 重置进度区
    const prog = $("prog-lines"); if (prog) prog.innerHTML = "";
    const verdicts = $("verdicts");
    if (verdicts) verdicts.innerHTML = "";
    const genStatus = $("gen-status"); if (genStatus) genStatus.textContent = "进行中…";
    const cancelBtn = $("btn-cancel"); if (cancelBtn) cancelBtn.classList.remove("hide");
    const kvDone = $("kv-done"); if (kvDone) kvDone.textContent = "0 / " + (totalCombos || 0);
    const kvCost = $("kv-cost"); if (kvCost) kvCost.textContent = fmtInt(cost || 0);

    let doneCombos = 0;
    if (State.es) { State.es.close(); State.es = null; }
    const es = API.stream("/api/generate/" + taskId + "/stream");
    State.es = es;

    es.addEventListener("open", () => {});
    es.onmessage = (ev) => {
      let payload;
      try { payload = JSON.parse(ev.data); } catch (e) { return; }
      if (!payload || !payload.type) return;
      if (payload.type === "progress") {
        renderProgress(payload);
      } else if (payload.type === "item") {
        doneCombos += 1;
        const kv = $("kv-done"); if (kv) kv.textContent = doneCombos + " / " + (totalCombos || doneCombos);
        const box = $("verdicts");
        if (box && payload.item) box.appendChild(verdictCard(payload.platform, payload.language, payload.item.compliance || {}, { live: true }));
      } else if (payload.type === "done") {
        finishStream(true, payload);
      } else if (payload.type === "cancelled") {
        finishStream(false, payload, "已取消");
      } else if (payload.type === "failed") {
        finishStream(false, payload, payload.error || "生成失败");
      } else if (payload.type === "bye") {
        // 流结束
        if (es.readyState === 1) es.close();
      }
    };
    es.onerror = () => {
      // EventSource 断线：尝试用终态接口兜底
      es.close();
      State.es = null;
      fetchTerminal(taskId);
    };
  }

  function renderProgress(p) {
    const prog = $("prog-lines");
    if (!prog) return;
    const line = el("div", { class: "line" });
    const st = el("span", { class: "st", text: (p.stage || "…").slice(0, 6) });
    const bar = el("span", { class: "bar" });
    const fill = el("i", { class: p.done >= p.total ? "done" : "run" });
    bar.appendChild(fill);
    const msg = el("span", { class: "", text: p.message || "" });
    line.appendChild(st); line.appendChild(bar); line.appendChild(msg);
    prog.appendChild(line);
    // 仅保留最近 12 行
    while (prog.children.length > 12) prog.removeChild(prog.firstChild);
  }

  function finishStream(ok, payload, errMsg) {
    const es = State.es;
    if (es) { es.close(); State.es = null; }
    const cancelBtn = $("btn-cancel"); if (cancelBtn) cancelBtn.classList.add("hide");
    const genStatus = $("gen-status");
    if (genStatus) genStatus.textContent = ok ? "已完成" : (errMsg === "已取消" ? "已取消" : "失败");
    if (payload && payload.balance != null) {
      setCredits(payload.balance);
      const kvBal = $("kv-balance"); if (kvBal) kvBal.textContent = fmtInt(payload.balance);
    }
    if (!ok && errMsg && errMsg !== "已取消") toast(errMsg, "err");
    else toast(ok ? "生成完成" : (errMsg || "任务结束"), ok ? "ok" : "err");
    // 刷新用户侧数据
    refreshUser();
  }

  async function fetchTerminal(taskId) {
    try {
      const t = await API.get("/api/tasks/" + taskId);
      const status = t.status;
      finishStream(status === "completed", { balance: State.user ? State.user.credits : 0 },
        status === "cancelled" ? "已取消" : status === "failed" ? "生成失败" : null);
    } catch (e) { console.warn("终态兜底失败", e); }
  }

  async function cancelGeneration() {
    if (!State.activeTaskId) return;
    try {
      await API.post("/api/generate/" + State.activeTaskId + "/cancel", {});
      toast("已发送取消请求", "ok");
    } catch (e) {
      toast(e.message || "取消失败", "err");
    }
  }

  /* ----- 积分明细抽屉 ----- */
  async function openLedger(title) {
    const dr = $("ledger-drawer"); if (!dr) return;
    const t = $("ledger-title"); if (t) t.textContent = title || "积分明细";
    const body = $("ledger-body"); if (body) body.innerHTML = '<div class="note info"><span>加载中…</span></div>';
    dr.classList.add("on");
    try {
      const data = await API.get("/api/credits/ledger?limit=100");
      const rows = (data && data.ledger) || [];
      if (!rows.length) { body.innerHTML = '<div class="note warn"><span>暂无积分变动记录</span></div>'; return; }
      body.innerHTML = "";
      const table = el("table");
      table.appendChild(el("thead", {}, [el("tr", {}, [
        el("th", { text: "时间" }), el("th", { text: "类型" }),
        el("th", { class: "mono", text: "变动" }), el("th", { text: "余额" }),
        el("th", { text: "备注" }),
      ])]));
      const tb = el("tbody");
      rows.forEach((r) => {
        const change = Number(r.change) || 0;
        const cls = change >= 0 ? "pass" : "block";
        tb.appendChild(el("tr", {}, [
          el("td", { text: (r.created_at || "").replace("T", " ").slice(0, 19) }),
          el("td", { text: r.type || "" }),
          el("td", { class: "mono " + cls, text: (change >= 0 ? "+" : "") + fmtInt(change) }),
          el("td", { class: "mono", text: fmtInt(r.balance_after != null ? r.balance_after : "") }),
          el("td", { text: r.note || "" }),
        ]));
      });
      table.appendChild(tb);
      body.appendChild(table);
    } catch (e) {
      body.innerHTML = '<div class="note danger"><span>' + escapeHtml(e.message || "加载失败") + '</span></div>';
    }
  }

  /* ----- 文件抽屉 ----- */
  let _fileKind = "inputs";
  async function openFiles(kind) {
    _fileKind = kind || "inputs";
    const dr = $("files-drawer"); if (!dr) return;
    const t = $("files-title"); if (t) t.textContent = _fileKind === "outputs" ? "生成产物" : "我上传的资料";
    const body = $("files-body"); if (body) body.innerHTML = '<div class="note info"><span>加载中…</span></div>';
    dr.classList.add("on");
    try {
      const data = await API.get("/api/space/files?kind=" + _fileKind);
      const files = (data && data.files) || [];
      if (!files.length) {
        body.innerHTML = '<div class="note warn"><span>' + (_fileKind === "outputs" ? "暂无生成产物" : "暂无上传资料") + '</span></div>';
        return;
      }
      body.innerHTML = "";
      const table = el("table");
      table.appendChild(el("thead", {}, [el("tr", {}, [
        el("th", { text: "文件名" }), el("th", { class: "mono", text: "大小" }),
        el("th", { text: "时间" }), el("th", { text: "操作" }),
      ])]));
      const tb = el("tbody");
      files.forEach((f) => {
        const dl = el("button", { class: "btn sm", text: "下载" });
        dl.addEventListener("click", () => {
          window.location.href = "/api/space/file/" + encodeURIComponent(f.name) + "?kind=" + _fileKind;
        });
        tb.appendChild(el("tr", {}, [
          el("td", { text: f.name }),
          el("td", { class: "mono", text: fmtSize(f.size) }),
          el("td", { text: f.created_at || "" }),
          el("td", {}, [dl]),
        ]));
      });
      table.appendChild(tb);
      body.appendChild(table);
    } catch (e) {
      body.innerHTML = '<div class="note danger"><span>' + escapeHtml(e.message || "加载失败") + '</span></div>';
    }
  }
  function closeFiles() { const dr = $("files-drawer"); if (dr) dr.classList.remove("on"); }

  async function uploadFile(ev) {
    const input = ev.target;
    const file = input.files && input.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    try {
      await API.postForm("/api/space/upload", form);
      toast("上传成功", "ok");
      openFiles(_fileKind);
      refreshUser();
    } catch (e) {
      toast(e.message || "上传失败", "err");
    } finally {
      input.value = "";
    }
  }

  /* ════════════ 管理员视图 ════════════ */
  let _adminLoaded = false;
  function enterAdmin(me) {
    showView("admin");
    const pill = $("a-pill"); if (pill) pill.textContent = "ADMIN";
    if (!_adminLoaded) { _adminLoaded = true; wireAdmin(); }
    refreshAdmin();
  }

  async function refreshAdmin() {
    try {
      const [users, audit, ledger] = await Promise.all([
        API.get("/api/admin/users"),
        API.get("/api/admin/audit?limit=50"),
        API.get("/api/admin/task-ledger?limit=200"),
      ]);
      renderAdminUsers(users && users.users);
      renderAdminAudit(audit && audit.audit);
      renderAdminLedger(ledger && ledger.tasks);
      renderAdminStats(users && users.users, ledger && ledger.tasks, audit && audit.audit);
      renderStorageTree(users && users.users);
    } catch (e) {
      console.warn("管理数据刷新失败", e);
      toast("管理数据加载失败：" + (e.message || ""), "err");
    }
  }

  function renderAdminStats(users, tasks, audit) {
    if (users) {
      setText("s-users", users.length);
      setText("s-credits", fmtInt(users.reduce((s, u) => s + (Number(u.credits) || 0), 0)));
      const cnt = $("a-usercount"); if (cnt) cnt.textContent = users.length;
    }
    if (tasks) setText("s-tasks", tasks.length);
    if (audit) setText("s-audit", audit.length);
  }
  function setText(id, v) { const n = $(id); if (n) n.textContent = (v == null ? "–" : v); }

  function renderAdminUsers(users) {
    const tb = $("admin-users");
    if (!tb) return;
    if (!users || !users.length) { tb.innerHTML = '<tr><td colspan="6">暂无用户</td></tr>'; return; }
    tb.innerHTML = "";
    users.forEach((u) => {
      const disabled = u.status === "disabled";
      const roleTag = u.role === "admin" ? "pass" : "plain";
      const statusTag = disabled ? "block" : "pass";
      const op = el("div", {});
      if (!disabled) {
        const dis = el("button", { class: "btn ghost sm", text: "禁用" });
        dis.addEventListener("click", () => doDisable(u.user_id, u.username));
        op.appendChild(dis);
      } else {
        const en = el("button", { class: "btn sm", text: "启用" });
        en.addEventListener("click", () => doEnable(u.user_id, u.username));
        op.appendChild(en);
      }
      tb.appendChild(el("tr", {}, [
        el("td", { text: u.username + (u.role === "admin" ? "（管理员）" : "") }),
        el("td", {}, [el("span", { class: "pill " + roleTag, text: u.role })]),
        el("td", { class: "mono", text: fmtInt(u.credits) }),
        el("td", {}, [el("span", { class: "pill " + statusTag, text: disabled ? "已禁用" : "正常" })]),
        el("td", { class: "mono", text: (u.inputs_count || 0) + " / " + (u.outputs_count || 0) }),
        el("td", {}, [op]),
      ]));
    });
  }

  async function doDisable(uid, name) {
    if (!confirm("确认禁用用户「" + name + "」？")) return;
    try { await API.post("/api/admin/users/" + uid + "/disable", {}); toast("已禁用 " + name, "ok"); refreshAdmin(); }
    catch (e) { toast(e.message || "禁用失败", "err"); }
  }
  async function doEnable(uid, name) {
    try { await API.post("/api/admin/users/" + uid + "/enable", {}); toast("已启用 " + name, "ok"); refreshAdmin(); }
    catch (e) { toast(e.message || "启用失败", "err"); }
  }

  function renderAdminAudit(audit) {
    const tb = $("admin-audit");
    if (!tb) return;
    if (!audit || !audit.length) { tb.innerHTML = '<tr><td colspan="4">暂无审计记录</td></tr>'; return; }
    tb.innerHTML = "";
    audit.forEach((a) => {
      tb.appendChild(el("tr", {}, [
        el("td", { text: (a.created_at || "").replace("T", " ").slice(0, 19) }),
        el("td", { text: a.action || "" }),
        el("td", { text: a.target_username || ("#" + (a.target_user_id != null ? a.target_user_id : "—")) }),        el("td", { text: auditDetail(a.detail) }),
      ]));
    });
  }
  function auditDetail(d) {
    if (!d) return "";
    if (typeof d === "string") return d;
    try { return Object.keys(d).map((k) => k + "=" + JSON.stringify(d[k])).join("，"); }
    catch (e) { return ""; }
  }

  function renderAdminLedger(tasks) {
    const tb = $("admin-ledger");
    if (!tb) return;
    if (!tasks || !tasks.length) { tb.innerHTML = '<tr><td colspan="6">暂无任务</td></tr>'; return; }
    tb.innerHTML = "";
    tasks.forEach((t) => {
      const cost = Number(t.credit_cost) || 0;
      const ref = Number(t.credit_refunded) || 0;
      const st = t.status === "completed" ? "pass" : (t.status === "failed" || t.status === "cancelled") ? "block" : "warn";
      const net = cost - ref;
      tb.appendChild(el("tr", {}, [
        el("td", { text: "#" + t.task_id }),
        el("td", { text: t.username || ("#" + t.user_id) }),
        el("td", { class: "mono", text: (t.language_count || 0) + "×" + (t.platform_count || 0) }),
        el("td", {}, [el("span", { class: "pill " + st, text: t.status })]),
        el("td", { class: "mono", text: (net > 0 ? "-" : "+") + fmtInt(Math.abs(net)) }),
        el("td", { class: "mono", text: (t.created_at || "").replace("T", " ").slice(0, 19) }),
      ]));
    });
  }

  function renderStorageTree(users) {
    const tree = $("admin-tree");
    if (!tree) return;
    tree.innerHTML = "";
    const root = el("div", { class: "row" }, [
      el("span", { class: "fold", text: "📁 data/" }),
      el("span", { class: "sz", text: "各用户独立目录" }),
    ]);
    tree.appendChild(root);
    (users || []).forEach((u) => {
      const uRow = el("div", { class: "row ind" }, [
        el("span", { class: "fold", text: "📁 user_" + u.user_id + "/  （" + u.username + "）" }),
      ]);
      tree.appendChild(uRow);
      tree.appendChild(el("div", { class: "row ind2" }, [
        el("span", { class: "file", text: "📂 inputs/  (" + (u.inputs_count || 0) + " 个文件)" }),
        el("span", { class: "lock", text: "🔒沙箱" }),
      ]));
      tree.appendChild(el("div", { class: "row ind2" }, [
        el("span", { class: "file", text: "📂 outputs/  (" + (u.outputs_count || 0) + " 个产物)" }),
        el("span", { class: "lock", text: "🔒沙箱" }),
      ]));
    });
  }

  /* ----- 管理视图事件 ----- */
  function wireAdmin() {
    const logoutBtn = $("btn-logout-admin"); if (logoutBtn) logoutBtn.addEventListener("click", logout);
    const rechargeOpen = $("btn-admin-recharge");
    if (rechargeOpen) rechargeOpen.addEventListener("click", openRecharge);
    // 导航：滚动到对应卡片
    const navMap = {
      "a-nav-users": null, "a-nav-recharge": "card-ledger",
      "a-nav-storage": "admin-tree", "a-nav-audit": "card-audit",
    };
    Object.keys(navMap).forEach((id) => {
      const n = $(id); if (!n) return;
      n.addEventListener("click", () => {
        qsa("#app-admin .item").forEach((i) => i.classList.remove("on"));
        n.classList.add("on");
        const target = navMap[id];
        if (target) { const t = $(target); if (t) t.scrollIntoView({ behavior: "smooth", block: "start" }); }
      });
    });
    const exportBtn = $("btn-export-csv");
    if (exportBtn) exportBtn.addEventListener("click", exportUsersCsv);

    // 充值弹层
    const reCancel = $("btn-recharge-cancel");
    if (reCancel) reCancel.addEventListener("click", closeRecharge);
    const reSubmit = $("btn-recharge-submit");
    if (reSubmit) reSubmit.addEventListener("click", submitRecharge);
  }

  function openRecharge() {
    const ov = $("recharge-overlay"); if (!ov) return;
    const u = $("recharge-username"), a = $("recharge-amount"), n = $("recharge-note");
    if (u) u.value = ""; if (a) a.value = ""; if (n) n.value = "";
    hideRechargeErr();
    ov.classList.add("on");
  }
  function closeRecharge() { const ov = $("recharge-overlay"); if (ov) ov.classList.remove("on"); }
  function showRechargeErr(msg) { const e = $("recharge-err"), t = $("recharge-err-text"); if (t) t.textContent = msg || "操作失败"; if (e) e.classList.add("on"); }
  function hideRechargeErr() { const e = $("recharge-err"); if (e) e.classList.remove("on"); }

  async function submitRecharge() {
    const username = ($("recharge-username") || {}).value || "";
    const amount = parseInt(($("recharge-amount") || {}).value || "", 10);
    const note = ($("recharge-note") || {}).value || "";
    hideRechargeErr();
    if (!username.trim()) { showRechargeErr("请填写用户名"); return; }
    if (!amount || amount < 1) { showRechargeErr("充值额须为正整数"); return; }
    if (!note.trim()) { showRechargeErr("备注必填"); return; }
    const btn = $("btn-recharge-submit"); if (btn) btn.disabled = true;
    try {
      await API.post("/api/admin/recharge", { username: username.trim(), amount: amount, note: note.trim() });
      closeRecharge();
      toast("已为 " + username + " 充值 " + amount + " 积分", "ok");
      refreshAdmin();
    } catch (e) {
      showRechargeErr(e.message || "充值失败");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function exportUsersCsv() {
    try {
      const data = await API.get("/api/admin/users");
      const users = (data && data.users) || [];
      const header = ["user_id", "username", "role", "status", "credits", "inputs", "outputs", "created_at"];
      const lines = [header.join(",")];
      users.forEach((u) => lines.push([
        u.user_id, u.username, u.role, u.status, u.credits,
        u.inputs_count || 0, u.outputs_count || 0, u.created_at || "",
      ].map((x) => '"' + String(x).replace(/"/g, '""') + '"').join(",")));
      const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = el("a", { href: url, download: "users_" + Date.now() + ".csv" });
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e) { toast(e.message || "导出失败", "err"); }
  }

  /* ════════════ 弹层全局事件（openAuth / 取消） ════════════ */
  function wireGlobalOverlays() {
    const tabLogin = $("tab-login"), tabReg = $("tab-register");
    if (tabLogin) tabLogin.addEventListener("click", () => setAuthTab("login"));
    if (tabReg) tabReg.addEventListener("click", () => setAuthTab("register"));
    const submit = $("btn-auth-submit"); if (submit) submit.addEventListener("click", submitAuth);
    const cancel = $("btn-auth-cancel"); if (cancel) cancel.addEventListener("click", closeAuth);
    // 点击遮罩关闭
    qsa(".overlay").forEach((ov) => {
      ov.addEventListener("click", (e) => { if (e.target === ov) ov.classList.remove("on"); });
    });
  }

  /* ════════════ 启动 ════════════ */
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => { wireGlobalOverlays(); bootstrap(); });
  } else {
    wireGlobalOverlays(); bootstrap();
  }
})();
