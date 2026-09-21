"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const SVG_NS = "http://www.w3.org/2000/svg";
  const GRAPH = { nodeWidth: 220, nodeHeight: 108, edgeGap: 144, rowGap: 32, paddingX: 32, paddingTop: 64, paddingBottom: 36, minScale: 1, maxScale: 1.1 };
  let graphResizeFrame = 0, graphObservedWidth = 0;
  const ICONS = {
    topology: ["M3 5h6v5H3z M15 14h6v5h-6z M3 14h6v5H3z M6 10v4 M9 7.5h9V14"],
    server: ["M4 3h16v7H4z M4 14h16v7H4z M7 6.5h.01 M7 17.5h.01 M11 6.5h6 M11 17.5h6"],
    laptop: ["M5 4h14v12H5z M3 16h18l1 4H2z M9 20h6"],
    terminal: ["M3 4h18v16H3z M7 9l3 3-3 3 M13 15h4"],
    route: ["M5 5h10a4 4 0 0 1 0 8H9a4 4 0 0 0 0 8h10 M16 18l3 3-3 3 M5 2v6 M2 5h6"],
    plus: ["M12 5v14 M5 12h14"],
    refresh: ["M20 7v5h-5 M4 17v-5h5 M5.5 7A7.5 7.5 0 0 1 18 5l2 3 M18.5 17A7.5 7.5 0 0 1 6 19l-2-3"],
    activity: ["M2 12h5l3-8 4 16 3-8h5"],
    alert: ["M12 3 2 21h20L12 3z M12 9v5 M12 18h.01"],
    info: ["M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0 M12 11v6 M12 7h.01"],
    check: ["M5 12l4 4L19 6"],
    checkCircle: ["M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0 M7 12l3 3 7-7"],
    arrowRight: ["M4 12h16 M14 6l6 6-6 6"],
    close: ["M6 6l12 12 M6 18 18 6"],
    play: ["M7 4v16l13-8L7 4z"],
    stop: ["M6 6h12v12H6z"],
    edit: ["m14 5 5 5 M4 15 16 3l5 5L9 20l-6 1 1-6z"],
    trash: ["M3 6h18 M5 6l1 15h12l1-15 M9 6V3h6v3 M10 10v7 M14 10v7"],
    search: ["M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0 M15 15l6 6"],
    maximize: ["M8 3H3v5 M16 3h5v5 M3 16v5h5 M21 16v5h-5"],
    lock: ["M5 10h14v11H5z M8 10V6a4 4 0 0 1 8 0v4 M12 14v3"],
    cursor: ["m4 3 5 18 4-7 7-4L4 3z M13 14l5 6"],
    power: ["M12 2v10 M6 5a9 9 0 1 0 12 0"],
    file: ["M5 2h9l5 5v15H5z M14 2v6h5 M9 12h6 M9 16h6"],
    settings: ["M4 7h16 M4 17h16 M8 4v6 M16 14v6"],
    clock: ["M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0 M12 6v6l4 2"],
    copy: ["M8 8h13v13H8z M16 8V3H3v13h5"],
    chevron: ["m9 5 7 7-7 7"],
  };
  const STATUS = { stopped: "已停止", starting: "启动中", running: "运行中", stopping: "停止中", error: "启动异常", degraded: "需要检查" };
  const ROLES = { source: "访问入口", relay: "本机中转", local: "本机中转", gateway: "SSH 跳板", jump: "SSH 跳板", target: "目标服务", destination: "目标服务" };
  const state = {
    data: { hosts: [], mappings: [], ssh_config: "" }, token: null, sessionPromise: null,
    selected: null, graphMode: "ssh", view: "overview", busy: new Map(), probes: new Map(),
    connected: false, closed: false, refreshing: false, editing: null, previewKey: null,
    previewRequest: 0, formBusy: false, confirmResolver: null, renderSignature: "", graphSignature: "",
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function svgEl(tag, attrs = {}, text) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function icon(name, className = "") {
    const node = svgEl("svg", { viewBox: "0 0 24 24", class: `icon ${className}`, "aria-hidden": "true" });
    for (const path of ICONS[name] || ICONS.info) node.append(svgEl("path", { d: path }));
    return node;
  }
  function mountIcons() {
    document.querySelectorAll("[data-icon]").forEach((node) => node.replaceChildren(icon(node.dataset.icon)));
  }
  function button(label, iconName, className, handler, title) {
    const node = el("button", className);
    node.type = "button";
    if (iconName) node.append(icon(iconName));
    if (label) node.append(el("span", "", label));
    if (title) { node.title = title; node.setAttribute("aria-label", title); }
    if (handler) node.addEventListener("click", handler);
    return node;
  }
  function hostById(id) { return state.data.hosts.find((h) => h.id === id || h.alias === id); }
  function hostLabel(id) { const h = hostById(id); return id === "local" ? "本机" : h?.label || h?.alias || id || "未知设备"; }
  function selectedMapping() { return state.data.mappings.find((m) => m.id === state.selected) || null; }
  function routeFor(mapping) {
    const route = mapping?.plan?.route || mapping?.route;
    if (Array.isArray(route) && route.length) return route.map((n) => typeof n === "string" ? { host: n, label: hostLabel(n) } : n);
    if (!mapping) return [];
    const result = [{ host: mapping.source_host, label: hostLabel(mapping.source_host), port: mapping.source_port, role: "source" }];
    if (mapping.source_host !== "local" && mapping.target_host !== "local") result.push({ host: "local", label: "本机", role: "relay", port: mapping.relay_port });
    result.push({ host: mapping.target_host, label: hostLabel(mapping.target_host), port: mapping.target_port, role: "target" });
    return result;
  }
  function formatTime(value, full = false) {
    if (!value) return "尚未检查";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("zh-CN", full ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false } : { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }
  function statusBadge(status) { return el("span", `status-badge ${STATUS[status] ? status : "stopped"}`, STATUS[status] || status || "未知状态"); }
  function setBusyButton(node, busy, text) {
    node.disabled = busy;
    node.classList.toggle("spin", busy);
    if (text) { node.replaceChildren(icon(busy ? "refresh" : "check"), el("span", "", text)); }
  }
  function showToast(message, error = false) {
    const toast = el("div", `toast${error ? " error" : ""}`);
    toast.setAttribute("role", error ? "alert" : "status");
    toast.append(icon(error ? "alert" : "checkCircle"), el("span", "", message), button("", "close", "icon-button", () => toast.remove(), "关闭提示"));
    $("toastRegion").append(toast);
    while ($("toastRegion").children.length > 4) $("toastRegion").firstElementChild.remove();
    setTimeout(() => toast.remove(), error ? 14000 : 6000);
  }
  async function sessionToken() {
    if (state.token) return state.token;
    if (!state.sessionPromise) state.sessionPromise = fetch("/api/session", { credentials: "same-origin", cache: "no-store" }).then(async (response) => {
      if (!response.ok) throw new Error(`无法获取本机会话（HTTP ${response.status}）`);
      const data = await response.json();
      if (!data.token) throw new Error("本机会话未返回验证令牌，请重新启动程序。");
      state.token = data.token;
      return data.token;
    }).finally(() => { state.sessionPromise = null; });
    return state.sessionPromise;
  }
  async function api(path, method = "GET", body, retry = true) {
    const options = { method, credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } };
    if (method !== "GET") options.headers["X-Jumper-Token"] = await sessionToken();
    if (body !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    let response;
    try { response = await fetch(path, options); }
    catch { throw new Error("无法连接本机 JumperManager 服务，请确认程序仍在运行。"); }
    let data;
    try { data = await response.json(); }
    catch { throw new Error(`服务返回了无法解析的响应（HTTP ${response.status}）。`); }
    if (!response.ok) {
      if (response.status === 403 && method !== "GET" && retry) { state.token = null; await sessionToken(); return api(path, method, body, false); }
      throw new Error(data.error || `请求失败（HTTP ${response.status}）`);
    }
    return data;
  }
  function setConnection(ok, message) {
    state.connected = ok;
    $("serviceDot").className = `status-dot${ok ? "" : " offline"}`;
    $("serviceText").textContent = ok ? "本机服务已连接" : state.closed ? "程序已关闭" : "本机服务未连接";
    $("connectionBanner").hidden = ok;
    $("connectionBanner").textContent = message || "与本机服务的连接暂时中断，正在尝试重新连接。请勿依赖当前缓存状态。";
    $("shutdownButton").disabled = !ok;
  }
  async function refreshState(silent = true) {
    if (state.refreshing || state.closed) return;
    state.refreshing = true;
    $("refreshButton").classList.add("spin");
    try {
      const data = await api("/api/state");
      const previousHosts = JSON.stringify(state.data.hosts);
      state.data = { ...data, hosts: Array.isArray(data.hosts) ? data.hosts : [], mappings: Array.isArray(data.mappings) ? data.mappings : [] };
      if (previousHosts !== JSON.stringify(state.data.hosts)) syncOpenDialogHosts();
      if (state.selected && !state.data.mappings.some((m) => m.id === state.selected)) state.selected = null;
      if (!state.selected && state.data.mappings.length) { state.selected = state.data.mappings[0].id; if (!state.renderSignature) state.graphMode = "mapping"; }
      setConnection(true);
      render();
      $("lastRefresh").textContent = `更新于 ${formatTime(new Date().toISOString())}`;
      if (!silent) showToast("状态已更新");
    } catch (error) {
      setConnection(false);
      if (!silent) showToast(error.message, true);
      if (!state.renderSignature) render();
    } finally { state.refreshing = false; $("refreshButton").classList.remove("spin"); }
  }

  function showView(view) {
    if (!["overview", "hosts", "activity"].includes(view)) return;
    state.view = view;
    document.querySelectorAll(".view").forEach((node) => { const active = node.id === `view-${view}`; node.hidden = !active; node.classList.toggle("active", active); });
    document.querySelectorAll("[data-view]").forEach((node) => { const active = node.dataset.view === view; node.classList.toggle("active", active); if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current"); });
    $("pageTitle").textContent = { overview: "映射总览", hosts: "设备与路由", activity: "运行日志" }[view];
    history.replaceState(null, "", `#${view}`);
    if (view === "activity") renderActivity();
    if (view === "hosts") renderHosts();
    if (view === "overview") scheduleGraphLayout();
  }
  function render() {
    const { hosts, mappings } = state.data;
    $("statHosts").textContent = hosts.filter((h) => h.id !== "local").length;
    $("statTotal").textContent = mappings.length;
    $("statRunning").textContent = mappings.filter((m) => m.status === "running").length;
    const attention = mappings.filter((m) => ["error", "degraded"].includes(m.status)).length;
    $("statAttention").textContent = attention;
    $("statAttention").style.color = attention ? "var(--danger)" : "";
    $("attentionHint").textContent = attention ? "查看错误信息或运行连接检查" : "暂无异常映射";
    $("navMappingCount").textContent = mappings.length;
    $("navHostCount").textContent = hosts.filter((h) => h.id !== "local").length;
    $("mappingCount").textContent = mappings.length;
    $("sshConfigPath").textContent = state.data.ssh_config || "未找到 SSH 配置文件";
    renderDiscovery();
    try { $("serviceEndpoint").textContent = new URL(state.data.server?.url || location.href).host; }
    catch { $("serviceEndpoint").textContent = location.host; }
    const signature = JSON.stringify([hosts, mappings, state.selected, [...state.busy], [...state.probes]]);
    if (signature !== state.renderSignature) {
      state.renderSignature = signature;
      renderMappings(); renderDetails(); renderGraph(); renderHosts(); renderLogFilter(); renderActivity();
    }
  }
  function renderDiscovery() {
    const discovery = state.data.ssh_discovery;
    const error = discovery?.error;
    const refreshing = discovery?.refreshing;
    const automatic = discovery?.automatic === true;
    $("discoveryTitle").textContent = automatic ? "自动监测 SSH 配置变化" : "SSH 配置同步";
    $("discoveryState").textContent = error ? "同步异常" : refreshing ? "正在更新设备" : automatic ? "自动监测中" : "等待监测状态";
    $("discoveryDot").className = `status-dot${error ? " offline" : refreshing || !automatic ? " pending" : ""}`;
    $("discoveryStatus").classList.toggle("failed", !!error);
    const meta = [];
    if (automatic) meta.push(`每 ${discovery.interval_seconds || 2} 秒监测 SSH 配置及 Include 文件`);
    else meta.push("SSH 配置及 Include 文件的变化会同步到设备列表");
    if (discovery?.last_refresh) meta.push(`最近同步 ${formatTime(discovery.last_refresh, true)}`);
    $("discoveryMeta").textContent = meta.join(" · ");
    $("discoveryError").hidden = !error;
    $("discoveryError").textContent = error ? `同步失败：${error}。当前显示最近一次成功读取的设备，请检查 SSH 配置。` : "";
  }
  function emptyState(title, message, iconName = "route") {
    const node = el("div", "empty-state");
    const mark = el("span", "empty-icon"); mark.append(icon(iconName));
    node.append(mark, el("strong", "", title), el("p", "", message));
    return node;
  }
  function selectMapping(id) {
    state.selected = id; state.graphMode = "mapping"; state.renderSignature = "";
    render();
  }
  function endpoint(host, port) {
    const chip = el("span", "endpoint-chip");
    chip.append(el("span", "host-label", hostLabel(host)), el("span", "port", `:${port}`));
    chip.title = `${hostLabel(host)}:${port}`;
    return chip;
  }
  function renderMappings() {
    const list = $("mappingList"); list.replaceChildren();
    const search = $("mappingSearch").value.trim().toLowerCase();
    const filter = $("statusFilter").value;
    const mappings = state.data.mappings.filter((m) => {
      const matchesSearch = [m.name, m.source_host, m.target_host, m.source_port, m.target_port, hostLabel(m.source_host), hostLabel(m.target_host)].join(" ").toLowerCase().includes(search);
      return matchesSearch && (filter === "all" || filter === m.status || (filter === "attention" && ["error", "degraded"].includes(m.status)));
    });
    if (!mappings.length) {
      const noMappings = !state.data.mappings.length;
      const empty = emptyState(noMappings ? "还没有端口映射" : "没有匹配的映射", noMappings ? "连接已在 SSH 配置中准备好的设备。指定入口与目标端口，交给程序规划路径。" : "试试其他关键词，或切换状态筛选。");
      if (noMappings) {
        empty.append(button("创建第一条映射", "plus", "button secondary", () => openMappingDialog()));
        empty.append(button("填入当前设备示例", "arrowRight", "text-button", () => { openMappingDialog(); fillExample(); }));
      }
      list.append(empty); return;
    }
    for (const mapping of mappings) {
      const row = el("article", `mapping-row${state.selected === mapping.id ? " selected" : ""}`);
      row.setAttribute("aria-label", `${mapping.name || "未命名映射"}，${STATUS[mapping.status] || mapping.status}`);
      row.addEventListener("click", (event) => { if (!event.target.closest("button")) selectMapping(mapping.id); });
      const top = el("div", "mapping-row-top");
      const mark = el("span", "mapping-icon"); mark.append(icon("route"));
      const name = el("h3", "mapping-name");
      name.append(button(mapping.name || `${hostLabel(mapping.source_host)} → ${hostLabel(mapping.target_host)}`, "", "mapping-name-button", () => selectMapping(mapping.id)));
      top.append(mark, name, statusBadge(mapping.status));
      const endpoints = el("div", "mapping-endpoints");
      const sep = el("span", "endpoint-separator"); sep.append(icon("arrowRight"), el("span", "relay-label", "经本机"), icon("arrowRight"));
      endpoints.append(endpoint(mapping.source_host, mapping.source_port), sep, endpoint(mapping.target_host, mapping.target_port));
      const bottom = el("div", "mapping-row-bottom");
      const busy = state.busy.get(mapping.id);
      const label = busy ? { start: "正在建立隧道…", stop: "正在停止隧道…", check: "正在检查连接…", delete: "正在删除…" }[busy] : mapping.last_checked ? `检查于 ${formatTime(mapping.last_checked)}` : "尚未运行连接检查";
      bottom.append(el("span", "mapping-meta", label));
      const actions = el("div", "mapping-actions");
      const active = ["running", "starting", "degraded", "stopping"].includes(mapping.status);
      const toggle = button(active ? "停止" : "启动", busy === "start" || busy === "stop" ? "refresh" : active ? "stop" : "play", `button small ${active ? "secondary" : "primary"}${busy === "start" || busy === "stop" ? " spin" : ""}`, () => mappingAction(mapping, active ? "stop" : "start"));
      toggle.disabled = !!busy || ["starting", "stopping"].includes(mapping.status);
      const check = button("检查", busy === "check" ? "refresh" : "activity", `button small ghost${busy === "check" ? " spin" : ""}`, () => mappingAction(mapping, "check"));
      check.disabled = !!busy || ["starting", "stopping"].includes(mapping.status);
      const edit = button("", "edit", "icon-button", () => openMappingDialog(mapping), active ? "先停止映射再编辑" : "编辑映射");
      edit.disabled = !!busy || !["stopped", "error"].includes(mapping.status);
      const remove = button("", "trash", "icon-button", () => deleteMapping(mapping), active ? "先停止映射再删除" : "删除映射");
      remove.disabled = !!busy || active;
      actions.append(toggle, check, edit, remove); bottom.append(actions);
      row.append(top, endpoints, bottom);
      if (mapping.error) row.append(el("div", "mapping-error", mapping.error));
      list.append(row);
    }
  }
  function kv(label, value) { const row = el("div", "detail-kv"); row.append(el("span", "", label), el("code", "", value)); return row; }
  function renderDetails() {
    const container = $("mappingDetails"); container.replaceChildren();
    const mapping = selectedMapping();
    if (!mapping) {
      const empty = emptyState("连接信息，一目了然", "选择一条映射，即可查看实际中转路径、连接状态和最近日志。", "topology");
      empty.style.minHeight = "235px"; empty.style.padding = "25px 3px"; container.append(empty); return;
    }
    const top = el("div", "detail-name-row"); top.append(el("h3", "", mapping.name), statusBadge(mapping.status));
    container.append(top, el("p", "detail-desc", mapping.plan?.description || "通过本机建立 SSH 隧道，将访问入口转发到目标服务。"));
    container.append(kv("访问地址", `${mapping.bind_address || "127.0.0.1"}:${mapping.source_port}`));
    container.append(kv("目标地址", `${mapping.target_address || "127.0.0.1"}:${mapping.target_port}`));
    if (mapping.relay_port) container.append(kv("本机中转端口", String(mapping.relay_port)));
    container.append(el("h4", "detail-section-label", "数据经过的设备"));
    const route = el("div", "detail-route");
    routeFor(mapping).forEach((hop) => {
      const row = el("div", "detail-route-step");
      row.append(el("strong", "", `${hop.label || hostLabel(hop.host)}${hop.port ? `:${hop.port}` : ""}`), el("span", "", ROLES[hop.role] || hop.role || "SSH 连接"));
      route.append(row);
    }); container.append(route);
    if (mapping.health) {
      const health = el("div", `health-box${mapping.health.ok ? "" : " unhealthy"}`);
      const title = el("div", "health-title"); title.append(icon(mapping.health.ok ? "checkCircle" : "alert"), el("span", "", mapping.health.summary || (mapping.health.ok ? "连接检查通过" : "连接检查未通过")));
      health.append(title);
      for (const item of Array.isArray(mapping.health.details) ? mapping.health.details : []) health.append(el("p", "", typeof item === "string" ? item : JSON.stringify(item)));
      health.append(el("small", "", "检查涵盖隧道与目标可达性，业务健康需由应用自身验证。"));
      container.append(health);
    }
    if (mapping.plan?.warnings?.length) for (const warning of mapping.plan.warnings) container.append(el("p", "host-warning", warning));
    const logHeading = el("h4", "detail-section-label", "最近日志"); container.append(logHeading);
    const logs = el("div", "detail-log");
    const recent = Array.isArray(mapping.logs) ? mapping.logs.slice(-4) : [];
    if (!recent.length) logs.append(el("p", "detail-log-entry", "暂无日志。启动映射后将在此记录连接过程。"));
    for (const entry of recent) { const row = el("div", `detail-log-entry ${safeLevel(entry.level)}`); row.append(el("span", "log-time", formatTime(entry.time)), document.createTextNode(entry.message || "")); logs.append(row); }
    container.append(logs);
    const footer = el("div", "detail-footer");
    footer.append(button("查看全部日志", "terminal", "button small ghost", () => { $("logFilter").value = mapping.id; showView("activity"); }));
    const copy = button("复制入口", "copy", "button small ghost", async () => {
      const address = `${mapping.bind_address === "::1" ? "[::1]" : mapping.bind_address || "127.0.0.1"}:${mapping.source_port}`;
      try { await navigator.clipboard.writeText(address); showToast(`已复制 ${address}（在 ${hostLabel(mapping.source_host)} 上使用）`); }
      catch { showToast(`访问入口：${address}（在 ${hostLabel(mapping.source_host)} 上使用）`); }
    }); footer.append(copy); container.append(footer);
  }

  async function mappingAction(mapping, action) {
    if (state.busy.has(mapping.id)) return;
    state.busy.set(mapping.id, action); state.renderSignature = ""; render();
    try {
      const updated = await api(`/api/mappings/${encodeURIComponent(mapping.id)}/${action}`, "POST", {});
      const index = state.data.mappings.findIndex((m) => m.id === mapping.id);
      if (index >= 0) state.data.mappings[index] = updated;
      state.selected = mapping.id;
      if (action === "start") state.graphMode = "mapping";
      const failed = action === "check" ? updated.health?.ok === false : ["error", "degraded"].includes(updated.status);
      const message = action === "check" ? updated.health?.summary || "连接检查已完成" : action === "start" ? failed ? updated.error || "隧道未能成功启动，请查看诊断信息。" : `已启动「${mapping.name}」` : `已停止「${mapping.name}」`;
      showToast(message, failed);
    } catch (error) { showToast(error.message, true); }
    finally { state.busy.delete(mapping.id); state.renderSignature = ""; render(); await refreshState(); }
  }
  function confirmAction(title, message, label) {
    if (state.confirmResolver) state.confirmResolver(false);
    $("confirmTitle").textContent = title; $("confirmMessage").textContent = message; $("confirmOkButton").textContent = label;
    $("confirmDialog").showModal();
    return new Promise((resolve) => { state.confirmResolver = resolve; });
  }
  function resolveConfirm(answer) {
    const resolve = state.confirmResolver; state.confirmResolver = null; $("confirmDialog").close(); if (resolve) resolve(answer);
  }
  async function deleteMapping(mapping) {
    if (!await confirmAction("删除这条映射？", `将删除「${mapping.name}」的配置及其记录。设备的 SSH 配置不受影响。`, "删除映射")) return;
    state.busy.set(mapping.id, "delete"); renderMappings();
    try { await api(`/api/mappings/${encodeURIComponent(mapping.id)}`, "DELETE"); showToast("映射已删除"); }
    catch (error) { showToast(error.message, true); }
    finally { state.busy.delete(mapping.id); await refreshState(); }
  }

  function renderHosts() {
    const list = $("hostList"); list.replaceChildren();
    const hosts = state.data.hosts;
    if (!hosts.length) { list.append(emptyState("尚未识别设备", "请在本机 SSH 配置中添加 Host 别名，设备列表会自动同步。", "server")); return; }
    for (const host of hosts) {
      const local = host.id === "local";
      const card = el("article", `host-card${local ? " local" : ""}`); card.dataset.host = host.id;
      const top = el("div", "host-card-top"); const mark = el("span", "host-icon"); mark.append(icon(local ? "laptop" : "server"));
      const name = el("div"); name.append(el("h3", "", host.label || host.alias), el("small", "", local ? "LOCAL WORKSTATION" : host.alias));
      top.append(mark, name, el("span", "host-role", local ? "RELAY" : "SSH")); card.append(top);
      for (const [label, value] of local ? [["主机地址", "127.0.0.1"], ["连接方式", "本机访问"]] : [["主机地址", host.hostname || "—"], ["SSH 登录", `${host.user || "默认用户"} · 端口 ${host.port || 22}`]]) {
        const row = el("div", "host-row"); row.append(el("span", "", label), el("code", "", value)); card.append(row);
      }
      card.append(el("div", "host-route-label", local ? "中转节点" : "SSH 访问路径"));
      const route = el("div", "host-route"); const hops = hostPath(host);
      hops.forEach((id, index) => { if (index) route.append(icon("chevron")); route.append(el("span", id === "local" ? "route-local" : "", hostLabel(id))); }); card.append(route);
      if (host.warning) card.append(el("p", "host-warning", host.warning));
      const actions = el("div", "host-actions");
      const count = state.data.mappings.filter((m) => m.source_host === host.id || m.target_host === host.id || routeFor(m).some((hop) => hop.host === host.id)).length;
      actions.append(el("span", "mapping-meta", `${count} 条关联映射`));
      if (!local) {
        const busy = state.busy.has(`host:${host.id}`);
        const probe = button(busy ? "连接测试中" : "测试 SSH", busy ? "refresh" : "activity", `button small ghost${busy ? " spin" : ""}`, () => probeHost(host)); probe.disabled = busy; actions.append(probe);
      } else actions.append(el("span", "live-label", "本机中转"));
      card.append(actions);
      const result = state.probes.get(host.id);
      if (result) card.append(el("p", `host-probe-result ${result.ok ? "ok" : "failed"}`, result.message));
      list.append(card);
    }
  }
  async function probeHost(host) {
    const key = `host:${host.id}`; if (state.busy.has(key)) return;
    state.busy.set(key, "probe"); renderHosts();
    try { const result = await api("/api/hosts/probe", "POST", { alias: host.alias || host.id }); state.probes.set(host.id, result); showToast(result.message || (result.ok ? `${hostLabel(host.id)} SSH 连接成功` : "SSH 连接测试失败"), !result.ok); }
    catch (error) { state.probes.set(host.id, { ok: false, message: error.message }); showToast(error.message, true); }
    finally { state.busy.delete(key); renderHosts(); }
  }
  async function refreshHosts() {
    setBusyButton($("refreshHostsButton"), true);
    try { await api("/api/hosts/refresh", "POST", {}); await refreshState(); state.graphSignature = ""; renderGraph(); showToast("已重新读取 SSH 配置与设备路由"); }
    catch (error) { showToast(error.message, true); }
    finally { setBusyButton($("refreshHostsButton"), false); }
  }
  function hostPath(host) {
    if (host.id === "local") return ["local"];
    const route = Array.isArray(host.route) ? host.route.map((entry) => typeof entry === "string" ? entry : entry.host || entry.id || entry.alias).filter(Boolean) : [];
    const path = ["local", ...route.filter((id) => id !== "local")];
    if (path[path.length - 1] !== host.id) path.push(host.id);
    return path.filter((id, index) => index === 0 || id !== path[index - 1]);
  }
  function safeLevel(level) { return ["error", "warning", "info", "debug", "stderr", "success"].includes(level) ? level : "info"; }
  function renderLogFilter() {
    const select = $("logFilter"); const current = select.value; select.replaceChildren(new Option("全部映射", "all"));
    for (const m of state.data.mappings) select.add(new Option(m.name || m.id, m.id));
    select.value = state.data.mappings.some((m) => m.id === current) ? current : "all";
  }
  function renderActivity() {
    const list = $("activityList"); list.replaceChildren();
    const filter = $("logFilter").value;
    const logs = state.data.mappings.filter((m) => filter === "all" || m.id === filter).flatMap((m) => (Array.isArray(m.logs) ? m.logs : []).map((log) => ({ ...log, mapping: m.name || m.id })));
    logs.sort((a, b) => (new Date(b.time).getTime() || 0) - (new Date(a.time).getTime() || 0));
    $("activityCount").textContent = logs.length;
    if (!logs.length) { list.append(emptyState("尚无运行记录", "启动映射、停止映射或执行检查后，事件会记录在这里。", "terminal")); return; }
    for (const log of logs.slice(0, 500)) {
      const row = el("div", "activity-row"); const time = el("time", "", formatTime(log.time, true)); if (log.time) time.dateTime = log.time;
      const name = el("span", "log-mapping", log.mapping); name.title = log.mapping;
      row.append(time, el("span", `log-level ${safeLevel(log.level)}`, log.level || "info"), name, el("span", "log-message", log.message || "")); list.append(row);
    }
  }

  function graphViewportWidth() {
    const viewport = $("graphViewport"), width = viewport.getBoundingClientRect().width;
    // Reserve a scrollbar gutter even before a tall diagram creates its scrollbar.
    // Measuring the fixed outer viewport avoids feedback from the SVG's size.
    return Math.max(0, Math.floor(width - 20));
  }
  function graphCanvasSize(naturalWidth, naturalHeight, availableWidth) {
    const scale = Math.max(GRAPH.minScale, Math.min(GRAPH.maxScale, availableWidth / naturalWidth || 1));
    const width = Math.max(naturalWidth, availableWidth / scale);
    return { width, height: naturalHeight, scale, pixelWidth: width * scale, pixelHeight: naturalHeight * scale };
  }
  function mappingGraphLayout(route, availableWidth) {
    const count = Math.max(1, route.length);
    const canvas = graphCanvasSize(count * GRAPH.nodeWidth + (count - 1) * GRAPH.edgeGap + 2 * GRAPH.paddingX, 300, availableWidth);
    const gap = count > 1 ? (canvas.width - 2 * GRAPH.paddingX - GRAPH.nodeWidth) / (count - 1) : 0;
    const coordinates = route.map((hop, index) => ({ ...hop, x: count === 1 ? canvas.width / 2 : GRAPH.paddingX + GRAPH.nodeWidth / 2 + index * gap, y: 140, step: index }));
    return { ...canvas, coordinates };
  }
  function sshGraphLayout(hosts, availableWidth) {
    const nodes = new Map([["local", { id: "local", depth: 0 }]]), edges = new Map();
    for (const host of hosts) {
      const path = hostPath(host);
      path.forEach((id, index) => {
        const existing = nodes.get(id);
        if (!existing) nodes.set(id, { id, depth: index });
        else if (id !== "local") existing.depth = Math.max(existing.depth, index);
        if (index && id !== path[index - 1]) edges.set(`${path[index - 1]}\u0000${id}`, { from: path[index - 1], to: id });
      });
    }
    const maxDepth = Math.max(0, ...Array.from(nodes.values()).map((node) => node.depth));
    const columns = Array.from({ length: maxDepth + 1 }, () => []);
    nodes.forEach((node) => columns[node.depth].push(node));
    const maxRows = Math.max(1, ...columns.map((column) => column.length));
    const naturalWidth = (maxDepth + 1) * GRAPH.nodeWidth + maxDepth * GRAPH.edgeGap + 2 * GRAPH.paddingX;
    const naturalHeight = Math.max(300, GRAPH.paddingTop + maxRows * GRAPH.nodeHeight + (maxRows - 1) * GRAPH.rowGap + GRAPH.paddingBottom);
    const canvas = graphCanvasSize(naturalWidth, naturalHeight, availableWidth);
    const gap = maxDepth ? (canvas.width - 2 * GRAPH.paddingX - GRAPH.nodeWidth) / maxDepth : 0;
    const coords = new Map(), gateways = new Set(Array.from(edges.values(), (edge) => edge.from));
    columns.forEach((column, depth) => {
      // Keep small upstream columns visible near the first rows of a tall graph.
      const offset = (Math.min(maxRows, 3) - Math.min(column.length, 3)) * (GRAPH.nodeHeight + GRAPH.rowGap) / 2;
      column.forEach((node, index) => coords.set(node.id, {
        host: node.id, x: maxDepth ? GRAPH.paddingX + GRAPH.nodeWidth / 2 + depth * gap : canvas.width / 2,
        y: maxRows === 1 ? 140 : GRAPH.paddingTop + GRAPH.nodeHeight / 2 + offset + index * (GRAPH.nodeHeight + GRAPH.rowGap),
        role: node.id === "local" ? "relay" : gateways.has(node.id) ? "gateway" : "SSH 设备",
      }));
    });
    return { ...canvas, nodes, edges, columns, coords, maxDepth };
  }
  function makeSvgBase({ width, height, pixelWidth, pixelHeight }) {
    const svg = $("topologySvg"); svg.replaceChildren(); svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("preserveAspectRatio", "xMinYMin meet");
    svg.setAttribute("width", String(pixelWidth)); svg.setAttribute("height", String(pixelHeight));
    svg.style.width = `${pixelWidth}px`; svg.style.height = `${pixelHeight}px`;
    const defs = svgEl("defs");
    for (const [id, color] of [["arrow-ssh", "#42546f"], ["arrow-flow", "#36d4b7"], ["arrow-muted", "#6d829a"]]) {
      const marker = svgEl("marker", { id, markerWidth: 7, markerHeight: 7, refX: 6, refY: 3.5, orient: "auto", markerUnits: "userSpaceOnUse" });
      marker.append(svgEl("path", { d: "M0 0 7 3.5 0 7", fill: "none", stroke: color, "stroke-width": 1.1 })); defs.append(marker);
    }
    svg.append(defs); return svg;
  }
  function truncate(value, length = 23) { const text = String(value || ""); return text.length > length ? text.slice(0, length - 1) + "…" : text; }
  function nodeLabel(value, maxWidth, fontSize) {
    const text = String(value || ""), characters = Array.from(text);
    // Conservative fallback for non-rendering DOMs; the live SVG is measured below.
    const advance = (char) => fontSize * (/^[\x00-\x7F]$/.test(char) ? 1.1 : 2);
    if (characters.reduce((width, char) => width + advance(char), 0) <= maxWidth) return text;
    let label = "", width = fontSize;
    for (const char of characters) { if (width + advance(char) > maxWidth) break; label += char; width += advance(char); }
    return `${label}…`;
  }
  function fitSvgNodeLabel(node, value, maxWidth, fontSize) {
    const text = String(value || ""), characters = Array.from(text);
    const fallback = () => { node.textContent = nodeLabel(text, maxWidth, fontSize); };
    if (typeof node.getComputedTextLength !== "function") { fallback(); return; }
    try {
      node.textContent = text;
      const fullWidth = node.getComputedTextLength();
      if (!Number.isFinite(fullWidth) || (text && fullWidth <= 0)) { fallback(); return; }
      if (fullWidth <= maxWidth) return;
      // Measure after attachment, using the actual CSS font, weight and spacing.
      let low = 0, high = characters.length;
      while (low < high) {
        const middle = Math.ceil((low + high) / 2);
        node.textContent = `${characters.slice(0, middle).join("")}…`;
        if (node.getComputedTextLength() <= maxWidth) low = middle;
        else high = middle - 1;
      }
      node.textContent = `${characters.slice(0, low).join("")}…`;
    } catch { fallback(); }
  }
  function drawNode(svg, info) {
    const { x, y, host, role, port, step, status, titleOverride } = info;
    const local = host === "local"; const h = hostById(host) || { id: host, alias: host, hostname: host };
    const group = svgEl("g", { class: `graph-node${local ? " local" : ""}`, transform: `translate(${x - GRAPH.nodeWidth / 2},${y - GRAPH.nodeHeight / 2})`, tabindex: "0", role: "button", "aria-label": `查看设备 ${hostLabel(host)}${port ? `，端口 ${port}` : ""}` });
    const fittedLabels = [];
    const addLabel = (attrs, value, maxWidth, fontSize) => {
      const node = svgEl("text", attrs, nodeLabel(value, maxWidth, fontSize));
      group.append(node); fittedLabels.push({ node, value, maxWidth, fontSize });
    };
    group.append(svgEl("title", {}, `${hostLabel(host)}${port ? `:${port}` : ""}\n${local ? "本机中转" : `${h.user ? h.user + "@" : ""}${h.hostname}:${h.port || 22}`}`));
    group.append(svgEl("rect", { class: "node-background", width: GRAPH.nodeWidth, height: GRAPH.nodeHeight, rx: 11, fill: local ? "#17312f" : "#162333", stroke: local ? "#32675c" : "#30435b", "stroke-width": 1 }));
    group.append(svgEl("rect", { x: 15, y: 16, width: 32, height: 32, rx: 7, fill: local ? "#215145" : "#213348", stroke: local ? "#31775f" : "#33485f", "stroke-width": .6 }));
    const smallIcon = svgEl("svg", { x: 23, y: 24, width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: local ? "#59d8bc" : "#8aabce", "stroke-width": 1.6, "stroke-linecap": "round", "stroke-linejoin": "round" });
    for (const d of ICONS[local ? "laptop" : "server"]) smallIcon.append(svgEl("path", { d })); group.append(smallIcon);
    addLabel({ x: 59, y: 31, class: "node-title" }, titleOverride || h.label || hostLabel(host), 135, 15);
    addLabel({ x: 59, y: 50, class: "node-meta" }, local ? "127.0.0.1" : h.hostname || h.alias, 147, 12);
    group.append(svgEl("line", { x1: 14, y1: 67, x2: 206, y2: 67, stroke: local ? "#2b4c44" : "#273b50", "stroke-width": .7 }));
    addLabel({ x: 14, y: 89, class: "node-kind" }, ROLES[role] || role || (local ? "本机中转" : "SSH 设备"), 92, 12);
    group.append(svgEl("text", { x: 206, y: 89, class: "port-label", "text-anchor": "end" }, port ? `:${port}` : local ? "RELAY" : `SSH :${h.port || 22}`));
    if (step !== undefined) group.append(svgEl("text", { x: 2, y: -11, class: "graph-step" }, String(step + 1).padStart(2, "0")));
    if (status === "running") group.append(svgEl("circle", { cx: 204, cy: 24, r: 3, fill: "#36d4b7" }));
    const navigate = () => { showView("hosts"); const card = Array.from($("hostList").children).find((n) => n.dataset.host === host); if (card) { card.scrollIntoView({ block: "center", behavior: "smooth" }); card.animate([{ borderColor: "#36d4b7" }, { borderColor: "" }], { duration: 1200 }); } };
    group.addEventListener("click", navigate); group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); navigate(); } });
    svg.append(group);
    fittedLabels.forEach(({ node, value, maxWidth, fontSize }) => fitSvgNodeLabel(node, value, maxWidth, fontSize));
  }
  function renderGraph() {
    const availableWidth = graphViewportWidth();
    if (availableWidth <= 0) return;
    const signature = JSON.stringify([availableWidth, state.graphMode, state.selected, state.data.hosts, state.data.mappings.map((m) => ({ id: m.id, status: m.status, route: routeFor(m), relay: m.relay_port }))]);
    if (signature === state.graphSignature) return;
    state.graphSignature = signature;
    document.querySelectorAll("[data-graph]").forEach((node) => { const active = node.dataset.graph === state.graphMode; node.classList.toggle("active", active); node.setAttribute("aria-pressed", String(active)); });
    $("graphTag").textContent = state.graphMode === "ssh" ? "SSH 路由" : selectedMapping()?.name || "映射链路";
    $("graphTag").style.maxWidth = "230px"; $("graphTag").style.overflow = "hidden"; $("graphTag").style.textOverflow = "ellipsis";
    $("graphEmpty").hidden = true;
    if (state.graphMode === "mapping") renderMappingGraph(); else renderSSHGraph();
  }
  function renderMappingGraph() {
    const mapping = selectedMapping();
    if (!mapping) { makeSvgBase(graphCanvasSize(280, 300, graphViewportWidth())); $("graphEmpty").hidden = false; $("graphCaption").textContent = "新建映射后，可在这里查看完整的数据路径。"; return; }
    const route = routeFor(mapping);
    const layout = mappingGraphLayout(route, graphViewportWidth());
    const svg = makeSvgBase(layout);
    const running = mapping.status === "running";
    const { coordinates, width } = layout;
    for (let i = 1; i < coordinates.length; i++) {
      const prev = coordinates[i - 1], next = coordinates[i]; const middle = (prev.x + next.x) / 2;
      svg.append(svgEl("path", { class: `graph-edge mapping ${running ? "running" : "inactive"}`, d: `M${prev.x + GRAPH.nodeWidth / 2} ${prev.y}H${next.x - GRAPH.nodeWidth / 2 - 7}`, "marker-end": `url(#${running ? "arrow-flow" : "arrow-muted"})` }));
      const label = prev.host === "local" ? "SSH 正向转发" : next.host === "local" ? "SSH 反向转发" : "SSH 连接";
      svg.append(svgEl("text", { x: middle, y: prev.y - 13, class: `graph-edge-label${running ? " mapping" : ""}`, "text-anchor": "middle" }, label));
    }
    coordinates.forEach((node) => drawNode(svg, { ...node, status: mapping.status }));
    svg.append(svgEl("text", { x: width / 2, y: 239, class: "graph-column-label", "text-anchor": "middle" }, running ? "TUNNEL ACTIVE  /  端口数据流" : "PLANNED ROUTE  /  规划的数据路径"));
    $("graphCaption").textContent = `${hostLabel(mapping.source_host)} 上访问 ${mapping.bind_address || "127.0.0.1"}:${mapping.source_port} → ${hostLabel(mapping.target_host)} 的 ${mapping.target_address || "127.0.0.1"}:${mapping.target_port}。${running ? "隧道已启动。" : "当前显示规划路径。"}`;
  }
  function renderSSHGraph() {
    const all = state.data.hosts.length ? state.data.hosts : [{ id: "local", alias: "local", label: "本机", hostname: "127.0.0.1" }];
    const layout = sshGraphLayout(all, graphViewportWidth());
    const { nodes, edges, columns, coords, maxDepth, width } = layout;
    const svg = makeSvgBase(layout);
    columns.forEach((col, depth) => { if (col.length) svg.append(svgEl("text", { x: coords.get(col[0].id).x, y: 28, class: "graph-column-label", "text-anchor": "middle" }, depth === 0 ? "LOCAL WORKSTATION" : depth === maxDepth ? "REMOTE DEVICES" : `SSH HOP ${String(depth).padStart(2, "0")}`)); });
    for (const edge of edges.values()) {
      const from = coords.get(edge.from), to = coords.get(edge.to); if (!from || !to) continue;
      const start = from.x + GRAPH.nodeWidth / 2, end = to.x - GRAPH.nodeWidth / 2 - 7, middle = (start + end) / 2;
      svg.append(svgEl("path", { class: "graph-edge", d: `M${start} ${from.y}C${middle} ${from.y},${middle} ${to.y},${end} ${to.y}`, "marker-end": "url(#arrow-ssh)" }));
      if (Math.abs(from.y - to.y) < 15) svg.append(svgEl("text", { x: middle, y: from.y - 11, class: "graph-edge-label", "text-anchor": "middle" }, `SSH :${hostById(edge.to)?.port || 22}`));
    }
    coords.forEach((node) => drawNode(svg, node));
    if (nodes.size === 1) svg.append(svgEl("text", { x: width / 2, y: 238, class: "graph-edge-label", "text-anchor": "middle" }, "添加 SSH 设备后显示连接路径"));
    $("graphCaption").textContent = `已解析 ${all.filter((h) => h.id !== "local").length} 台远端设备。虚线表示 SSH 配置中的访问路径，并不代表设备当前在线；点击设备可测试连接。`;
  }
  function scheduleGraphLayout() {
    if (graphResizeFrame) return;
    graphResizeFrame = requestAnimationFrame(() => {
      graphResizeFrame = 0;
      if ($("graphViewport").getBoundingClientRect().width > 0) renderGraph();
    });
  }
  function observeGraphViewport() {
    const viewport = $("graphViewport");
    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(() => {
        const width = Math.round(viewport.getBoundingClientRect().width);
        if (width > 0 && width !== graphObservedWidth) { graphObservedWidth = width; scheduleGraphLayout(); }
      });
      observer.observe(viewport);
    } else window.addEventListener("resize", scheduleGraphLayout);
  }

  function populateHostSelect(select, value) {
    select.replaceChildren();
    const hosts = state.data.hosts.length ? state.data.hosts : [{ id: "local", label: "本机", alias: "local" }];
    for (const host of hosts) select.add(new Option(host.id === "local" ? "本机 · local" : `${host.label || host.alias}${host.hostname && host.hostname !== host.alias ? ` · ${host.hostname}` : ""}`, host.id));
    if (value && !hosts.some((h) => h.id === value)) select.add(new Option(`${value} · 配置中未找到`, value));
    if (value) select.value = value;
  }
  function syncOpenDialogHosts() {
    if (!$("mappingDialog").open) return;
    const source = $("formSourceHost").value, target = $("formTargetHost").value;
    populateHostSelect($("formSourceHost"), source);
    populateHostSelect($("formTargetHost"), target);
    invalidatePreview();
    const missing = [source, target].filter((id, index, ids) => id && !hostById(id) && ids.indexOf(id) === index);
    $("formConfigNotice").textContent = missing.length
      ? `SSH 配置已更新；${missing.join("、")} 在配置中未找到。已保留当前输入，请重新选择设备并预览路径。`
      : "SSH 配置已更新，设备列表和路由已同步。已保留当前输入，请重新预览连接路径。";
    $("formConfigNotice").hidden = false;
  }
  function openMappingDialog(mapping = null) {
    state.editing = mapping?.id || null; state.previewKey = null; state.previewRequest++; state.formBusy = false;
    $("mappingForm").reset();
    $("dialogTitle").textContent = mapping ? "编辑端口映射" : "新建端口映射";
    $("formName").value = mapping?.name || "";
    populateHostSelect($("formSourceHost"), mapping?.source_host || "local");
    populateHostSelect($("formTargetHost"), mapping?.target_host || state.data.hosts.find((h) => h.id !== "local")?.id || "local");
    $("formSourcePort").value = mapping?.source_port || "";
    $("formTargetPort").value = mapping?.target_port || "";
    $("formBindAddress").value = mapping?.bind_address || "127.0.0.1";
    $("formTargetAddress").value = mapping?.target_address || "127.0.0.1";
    $("formAutoStart").checked = mapping?.auto_start === true;
    $("formError").hidden = true; $("previewPanel").hidden = true; $("formConfigNotice").hidden = true;
    $("saveMappingButton").disabled = true; $("saveMappingButton").textContent = "保存映射";
    $("previewButton").disabled = false; $("previewButton").classList.remove("spin");
    $("exampleButton").hidden = !!mapping; $("exampleButton").disabled = false;
    $("mappingDialog").showModal();
  }
  function closeMappingDialog() { if (state.formBusy) return; state.previewRequest++; $("mappingDialog").close(); }
  function fillExample() {
    const available = state.data.hosts.filter((h) => h.id !== "local");
    if (!available.length) { showFormError("SSH 配置中尚未识别到远端设备。添加 Host 别名后，设备列表会自动同步，也可以手动配置本机端口转发。"); return; }
    const selected = selectedMapping();
    const existing = selected && hostById(selected.source_host) && hostById(selected.target_host) ? selected : null;
    const target = available.find((h) => !available.some((other) => other.id !== h.id && hostPath(other).slice(1, -1).includes(h.id))) || available[0];
    const sourceId = existing?.source_host || "local", targetId = existing?.target_host || target.id;
    const sourcePort = existing?.source_port || 50051, targetPort = existing?.target_port || 50051;
    $("formName").value = `${hostLabel(sourceId)} → ${hostLabel(targetId)} · ${sourcePort} 示例`;
    $("formSourceHost").value = sourceId; $("formTargetHost").value = targetId;
    $("formSourcePort").value = sourcePort; $("formTargetPort").value = targetPort;
    $("formBindAddress").value = existing?.bind_address || "127.0.0.1";
    $("formTargetAddress").value = existing?.target_address || "127.0.0.1"; $("formAutoStart").checked = false;
    invalidatePreview(); $("formError").hidden = true; $("formConfigNotice").hidden = false;
    $("formConfigNotice").textContent = existing ? "已填入当前选中映射的设备与端口作为示例。请调整监听端口，避免与原映射冲突；尚未保存或启动。" : "已根据当前 SSH 设备填入示例。请将端口修改为实际服务端口；尚未保存或启动。";
  }
  function formPayload() {
    const sourcePort = Number($("formSourcePort").value), targetPort = Number($("formTargetPort").value);
    if (!Number.isInteger(sourcePort) || sourcePort < 1 || sourcePort > 65535 || !Number.isInteger(targetPort) || targetPort < 1 || targetPort > 65535) throw new Error("监听端口和服务端口必须是 1–65535 之间的整数。");
    const source = $("formSourceHost").value, target = $("formTargetHost").value, targetAddress = $("formTargetAddress").value.trim();
    if (!source || !target) throw new Error("请选择来源设备和目标设备。");
    if (!targetAddress) throw new Error("请填写目标设备上的服务地址。");
    return { name: $("formName").value.trim() || `${hostLabel(source)} → ${hostLabel(target)} · ${sourcePort}`, source_host: source, source_port: sourcePort, target_host: target, target_port: targetPort, bind_address: $("formBindAddress").value, target_address: targetAddress, auto_start: $("formAutoStart").checked };
  }
  function invalidatePreview() { state.previewKey = null; state.previewRequest++; $("saveMappingButton").disabled = true; $("previewPanel").hidden = true; }
  function showFormError(message) { $("formError").textContent = message; $("formError").hidden = false; }
  async function previewMapping() {
    if (state.formBusy) return;
    let payload; try { payload = formPayload(); } catch (error) { showFormError(error.message); return; }
    const requestId = ++state.previewRequest, key = JSON.stringify(payload);
    $("formError").hidden = true; $("previewButton").disabled = true; $("previewButton").classList.add("spin"); $("saveMappingButton").disabled = true;
    try {
      const preview = await api("/api/preview", "POST", payload);
      if (requestId !== state.previewRequest || !$("mappingDialog").open) return;
      const panel = $("previewPanel"); panel.replaceChildren();
      const title = el("div", "preview-title"); title.append(icon("checkCircle"), el("span", "", "路径规划完成")); panel.append(title);
      panel.append(el("p", "preview-description", preview.description || "已根据 SSH 配置规划映射路径。"));
      const route = el("div", "preview-route");
      (preview.route || []).forEach((hop, index) => { if (index) route.append(icon("arrowRight")); const node = typeof hop === "string" ? { host: hop } : hop; route.append(el("span", "", `${node.label || hostLabel(node.host)}${node.port ? `:${node.port}` : ""}`)); }); panel.append(route);
      if (preview.warnings?.length) { const warnings = el("div", "preview-warnings"); for (const message of preview.warnings) warnings.append(el("p", "preview-warning", message)); panel.append(warnings); }
      if (preview.steps?.length) {
        const steps = el("details", "preview-steps"); steps.append(el("summary", "", "查看连接步骤")); const list = el("ol");
        for (const step of preview.steps) list.append(el("li", "", typeof step === "string" ? step : step.description || step.label || JSON.stringify(step)));
        steps.append(list); panel.append(steps);
      }
      panel.hidden = false; state.previewKey = key; $("saveMappingButton").disabled = false; $("formConfigNotice").hidden = true;
      panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (error) { if (requestId === state.previewRequest) showFormError(error.message); }
    finally { $("previewButton").disabled = state.formBusy; $("previewButton").classList.remove("spin"); }
  }
  async function saveMapping(event) {
    event.preventDefault(); if (state.formBusy) return;
    let payload; try { payload = formPayload(); } catch (error) { showFormError(error.message); return; }
    if (JSON.stringify(payload) !== state.previewKey) { showFormError("配置已变更，请先重新预览连接路径。"); return; }
    state.formBusy = true; $("saveMappingButton").disabled = true; $("saveMappingButton").textContent = "保存中…"; $("previewButton").disabled = true; $("exampleButton").disabled = true;
    $("formError").hidden = true;
    try {
      const mapping = await api(state.editing ? `/api/mappings/${encodeURIComponent(state.editing)}` : "/api/mappings", state.editing ? "PUT" : "POST", payload);
      state.selected = mapping.id; state.graphMode = "mapping"; $("mappingDialog").close();
      showToast("映射已保存，点击“启动”即可建立隧道。"); await refreshState();
    } catch (error) { showFormError(error.message); }
    finally { state.formBusy = false; $("saveMappingButton").disabled = !state.previewKey; $("saveMappingButton").textContent = "保存映射"; $("previewButton").disabled = false; $("exampleButton").disabled = false; }
  }
  async function shutdown() {
    if (!await confirmAction("关闭 JumperManager？", "程序将停止由它管理的所有端口映射，然后关闭本机服务。\n\n关闭浏览器页面不会停止隧道；确认此操作才会结束程序。", "停止映射并关闭")) return;
    $("shutdownButton").disabled = true;
    try { await api("/api/shutdown", "POST", {}); state.closed = true; setConnection(false, "JumperManager 已关闭，由本程序管理的映射已停止。重新运行启动程序后刷新此页面即可。"); showToast("映射已停止，程序已关闭。"); }
    catch (error) { showToast(error.message, true); $("shutdownButton").disabled = false; }
  }
  function bindEvents() {
    document.querySelectorAll("[data-view]").forEach((node) => node.addEventListener("click", () => showView(node.dataset.view)));
    document.querySelector(".brand").addEventListener("click", (event) => { event.preventDefault(); showView("overview"); });
    document.querySelectorAll("[data-graph]").forEach((node) => node.addEventListener("click", () => { state.graphMode = node.dataset.graph; renderGraph(); }));
    $("refreshButton").addEventListener("click", () => refreshState(false));
    $("newMappingButton").addEventListener("click", () => openMappingDialog());
    $("graphNewButton").addEventListener("click", () => openMappingDialog());
    $("closeDialogButton").addEventListener("click", closeMappingDialog);
    $("mappingDialog").addEventListener("cancel", (event) => { event.preventDefault(); closeMappingDialog(); });
    $("mappingForm").addEventListener("input", invalidatePreview);
    $("mappingForm").addEventListener("change", invalidatePreview);
    $("mappingForm").addEventListener("submit", saveMapping);
    $("previewButton").addEventListener("click", previewMapping);
    $("exampleButton").addEventListener("click", fillExample);
    $("mappingSearch").addEventListener("input", renderMappings);
    $("statusFilter").addEventListener("change", renderMappings);
    $("refreshHostsButton").addEventListener("click", refreshHosts);
    $("logFilter").addEventListener("change", renderActivity);
    $("fitGraphButton").addEventListener("click", () => { state.graphSignature = ""; renderGraph(); $("graphViewport").scrollTo({ left: 0, top: 0, behavior: "smooth" }); });
    observeGraphViewport();
    $("confirmOkButton").addEventListener("click", () => resolveConfirm(true));
    $("confirmCancelButton").addEventListener("click", () => resolveConfirm(false));
    $("confirmCloseButton").addEventListener("click", () => resolveConfirm(false));
    $("confirmDialog").addEventListener("cancel", (event) => { event.preventDefault(); resolveConfirm(false); });
    $("shutdownButton").addEventListener("click", shutdown);
    window.addEventListener("hashchange", () => showView(location.hash.slice(1) || "overview"));
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshState(); });
  }
  async function start() {
    mountIcons(); bindEvents(); showView(location.hash.slice(1) || "overview");
    await refreshState();
    sessionToken().catch(() => {});
    setInterval(() => { if (!document.hidden) refreshState(); }, 4000);
  }
  start();
})();
