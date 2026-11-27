"use strict";
const $ = (selector) => document.querySelector(selector);
let csrf = $('meta[name="csrf-token"]').content;
const changes = typeof BroadcastChannel === "function" ? new BroadcastChannel("mcm") : null;
const networkErrorMessage = "网络连接失败，请恢复连接后重试";
let configs = [], ready = false, busy = false, replacement = null, editing = null;
function notify(message, error = false, source = "action") {
  const el = $("#notice"); el.dataset.source = source; el.textContent = message; el.classList.toggle("error", error); el.hidden = false;
}
function requireLogin() {
  $("#login-notice").hidden = false;
  ready = false;
  $("#backup-new").disabled = true;
  $("#service-status").textContent = "需要重新登录";
  render();
}
async function api(path, options = {}, retryCsrf = true) {
  if (!$("#login-notice").hidden && options.method && options.method !== "GET") throw new Error("请重新登录后重试");
  const headers = new Headers(options.headers || {});
  if (options.method && !["GET", "HEAD"].includes(options.method)) headers.set("X-CSRF-Token", csrf);
  let response;
  try {
    response = await fetch(path, { ...options, headers, credentials: "same-origin", redirect: "manual" });
  } catch (error) {
    if (error instanceof TypeError) throw new Error(networkErrorMessage);
    throw error;
  }
  if (response.type === "opaqueredirect") {
    requireLogin();
    throw new Error("请重新登录后重试");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    if (response.status === 403 && body.code === "csrf" && retryCsrf) {
      csrf = (await (await api("/api/csrf", {}, false)).json()).csrf_token;
      return api(path, options, false);
    }
    if (response.status === 401) requireLogin();
    const message = body.code === "stale" || response.status === 428
      ? "配置状态已变化，请核对刷新后的内容再提交" : body.detail;
    const error = new Error(message || `请求失败（${response.status}）`);
    error.status = response.status;
    error.code = body.code;
    error.current = body.current;
    throw error;
  }
  if (options.method && options.method !== "GET" && !path.endsWith("/subscription-link")) changes?.postMessage("refresh");
  return response;
}
function bytes(size) { return size < 1024 ? `${size} B` : size < 1048576 ? `${(size / 1024).toFixed(1)} KiB` : `${(size / 1048576).toFixed(2)} MiB`; }
function render() {
  $("#total").textContent = configs.length;
  $("#enabled").textContent = configs.filter(c => c.enabled && c.status === "ready").length;
  $("#count-badge").textContent = configs.length;
  const query = $("#search").value.trim().toLocaleLowerCase();
  const filtered = configs.filter(c => `${c.name} ${c.filename}`.toLocaleLowerCase().includes(query));
  $("#list").replaceChildren();
  for (const config of filtered) {
    const card = $("#card-template").content.cloneNode(true);
    const field = (name, value) => { card.querySelector(`[data-field="${name}"]`).textContent = value; };
    field("name", config.name); field("filename", config.filename); field("size", bytes(config.size));
    field("version", `版本 ${config.version}`); field("sha256", config.sha256); field("short-sha", config.sha256.slice(0, 12));
    field("updated", new Date(config.updated_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }));
    card.querySelector("time").dateTime = config.updated_at;
    card.querySelector('[data-field="name"]').title = config.name;
    card.querySelector('[data-field="filename"]').title = config.filename;
    field("badge", config.status !== "ready" ? "待恢复" : config.enabled ? "已启用" : "已停用");
    card.querySelector(".badge").classList.toggle("disabled", !config.enabled);
    card.querySelector(".badge").classList.toggle("pending", config.status !== "ready");
    field("description", config.description); card.querySelector(".description").hidden = !config.description;
    card.querySelector('[data-action="toggle"]').textContent = config.enabled ? "停用" : "启用";
    for (const button of card.querySelectorAll("[data-action]")) {
      button.addEventListener("click", () => action(button.dataset.action, config).catch(e => notify(e.message, true)));
      button.disabled = busy || (button.hasAttribute("data-write") && (!ready || config.status !== "ready"));
    }
    $("#list").append(card);
  }
  $("#empty").hidden = configs.length !== 0;
  $("#no-results").hidden = configs.length === 0 || filtered.length !== 0;
  for (const id of ["#upload-top", "#upload-select"]) $(id).disabled = !ready || busy;
}
let loadGeneration = 0;
let statusTimer, maxUploadBytes;
async function load() {
  const generation = ++loadGeneration;
  try {
    const [items, state] = await Promise.all([api("/api/configs").then(r => r.json()), api("/api/status").then(r => r.json())]);
    if (generation !== loadGeneration) return;
    clearTimeout(statusTimer);
    if (state.snapshotting) statusTimer = setTimeout(load, 2000);
    maxUploadBytes = state.max_upload_bytes;
    const loginRestored = !$("#login-notice").hidden;
    $("#login-notice").hidden = true;
    configs = items; ready = state.ready && !state.snapshotting;
    if (loginRestored) loadBackups();
    $("#service-status").textContent = state.snapshotting ? "备份快照中" : ready ? "运行正常" : "需要恢复";
    if (state.snapshotting) notify("正在创建一致快照，配置写入暂缓。", false, "status");
    else if (!ready) notify("存储存在未完成操作。请检查服务器磁盘和权限，然后重启管理端恢复。", true, "status");
    else if ($("#notice").dataset.source === "status") $("#notice").hidden = true;
  } catch (error) {
    if (generation !== loadGeneration) return;
    ready = false; $("#service-status").textContent = $("#login-notice").hidden ? "连接异常" : "需要重新登录";
    notify(error.message, true, "status");
  }
  render();
}
function mutationBlocked() {
  if (busy) return "正在处理其他操作，请稍后重试。";
  if (!ready) return `${$("#service-status").textContent}，请等服务可写后重试。`;
  return "";
}
async function mutate(task, message, unchangedNotice = null, onError = err => notify(err.message, true)) {
  const reason = mutationBlocked();
  if (reason) { onError(new Error(reason)); return false; }
  busy = true; render();
  try { await task(); notify(message); return true; }
  catch (error) {
    if (unchangedNotice && error.code === "unchanged") notify(unchangedNotice);
    else onError(error);
    return false;
  }
  finally { busy = false; await load(); }
}
async function copySubscription(base) {
  const text = api(`${base}/subscription-link`, {method: "POST"})
    .then(response => response.json())
    .then(({subscription_url}) => {
      if (typeof subscription_url !== "string" || !subscription_url) throw new Error("未获取到订阅链接，请刷新页面后重试");
      return new Blob([subscription_url], {type: "text/plain"});
    });
  try {
    if (typeof ClipboardItem === "function" && navigator.clipboard?.write) {
      // WebKit requires write() during the click. The item can resolve after the request.
      const item = new ClipboardItem({"text/plain": text});
      await Promise.all([text, navigator.clipboard.write([item])]);
    } else {
      const blob = await text;
      await navigator.clipboard.writeText(await blob.text());
    }
    notify("订阅链接已复制，请妥善保管。");
  } catch (_) {
    const blob = await text;
    $("#link-value").value = await blob.text();
    $("#link-dialog").showModal();
    $("#link-value").select();
  }
}
async function action(type, config) {
  const base = `/api/configs/${config.id}`;
  const revision = new URLSearchParams({expected_updated_at: config.updated_at});
  if (type === "copy") {
    await copySubscription(base);
  } else if (type === "history") {
    await showHistory(config);
  } else if (type === "preview") {
    const response = await api(`${base}/preview`);
    $("#preview-content").textContent = await response.text(); $("#preview-title").textContent = config.name; $("#preview-dialog").showModal();
  } else if (type === "replace") {
    replacement = config; $("#replace-input").click();
  } else if (type === "rename") {
    $("#rename-error").textContent = "";
    editing = config; $("#rename-name").value = config.name; $("#rename-description").value = config.description; $("#rename-dialog").showModal();
  } else if (type === "toggle") {
    await mutate(() => api(`${base}/${config.enabled ? "disable" : "enable"}?${revision}`, { method: "POST" }), config.enabled ? "已停用，订阅链接不可访问。" : "已启用，订阅链接恢复可用。");
  } else if (type === "rotate" && window.confirm(`重置「${config.name}」的 Token？\n旧订阅链接将失效，你需要在客户端更新链接。`)) {
    await mutate(() => api(`${base}/rotate-token?${revision}`, { method: "POST" }), "Token 已重置，请点击复制新链接。");
  } else if (type === "delete" && window.confirm(`确定删除「${config.name}」？\n订阅立即失效，配置和历史文件将被删除，此操作无法撤销。`)) {
    await mutate(() => api(`${base}?${revision}`, { method: "DELETE" }), "配置已删除，旧订阅链接已失效。");
  }
}
function askUploadChoice(dialog) {
  // Escape closes the dialog without setting a new returnValue.
  dialog.returnValue = "cancel";
  dialog.showModal();
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue), { once: true });
  });
}

async function chooseUploadTarget(filename, matches) {
  const single = matches.length === 1;
  $("#upload-match-message").textContent = single
    ? `已有配置「${matches[0].name}」使用了文件名「${filename}」，请选择如何处理。`
    : `有 ${matches.length} 份配置使用文件名「${filename}」，请选择要更新的配置，或另存为新配置。`;
  const select = $("#upload-target");
  select.replaceChildren(new Option("请选择配置", ""));
  for (const item of matches) {
    const updated = new Date(item.updated_at).toLocaleString("zh-CN");
    select.add(new Option(`${item.name} · v${item.version} · ${updated}`, item.id));
  }
  select.value = single ? matches[0].id : "";
  $("#upload-target-label").hidden = single;
  const update = $("#upload-update");
  function showSelection() {
    const selected = matches.find(item => item.id === select.value);
    update.disabled = !selected;
    update.textContent = selected ? `更新「${selected.name}」至 v${selected.version + 1}` : "更新原配置";
  }
  select.onchange = showSelection;
  showSelection();
  const choice = await askUploadChoice($("#upload-match-dialog"));
  if (choice === "update") return matches.find(item => item.id === select.value) ?? "cancel";
  return choice;
}

const unchangedMessage = "文件内容相同，未生成新版本";
async function sha256Hex(buffer) {
  // crypto.subtle exists only in secure contexts; the server still rejects content identical to the current version.
  if (!globalThis.crypto?.subtle) return null;
  try {
    const digest = await crypto.subtle.digest("SHA-256", buffer);
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
  } catch (_) { return null; }
}
async function confirmDuplicateUpload(target, versions) {
  const names = versions.map(version => `版本 ${version}`).join("、");
  $("#upload-duplicate-message").textContent = `这个文件与「${target.name}」的${names} 内容相同。继续上传会生成一个内容相同的新版本，并占用一个历史版本名额。只想恢复旧内容时，也可以在「历史版本」中回滚。`;
  return await askUploadChoice($("#upload-duplicate-dialog")) === "continue";
}

async function uploadFile(file, target) {
  if (file.size > maxUploadBytes) throw new Error("文件超过上传大小限制");
  let buffer;
  try { buffer = await file.arrayBuffer(); }
  catch (_) { throw new Error("文件无法读取，请重新选择"); }
  const form = new FormData();
  form.append("file", new Blob([buffer], {type: file.type}), file.name);
  let nameMessage = "";
  if (!target) {
    // Read before each file so earlier uploads in this batch and other admins' changes are visible.
    const items = await api("/api/configs").then(response => response.json());
    const matches = items.filter(item => item.filename === file.name);
    if (matches.length) {
      const choice = await chooseUploadTarget(file.name, matches);
      if (choice === "cancel") return "已取消";
      if (choice === "copy") nameMessage = `为「${file.name}」填写新名称，创建独立订阅链接。`;
      else target = choice;
    }
  }
  if (target) {
    const sha256 = await sha256Hex(buffer);
    if (sha256 && sha256 !== target.sha256) {
      const versions = await api(`/api/configs/${target.id}/versions`).then(response => response.json());
      const same = versions.filter(item => !item.is_current && item.sha256 === sha256).map(item => item.version);
      if (same.length && !await confirmDuplicateUpload(target, same)) return "已取消";
    }
    const path = `/api/configs/${target.id}/content?expected_current_version=${target.version}`;
    try {
      const updated = await api(path, { method: "PUT", body: form }).then(response => response.json());
      return `已更新「${updated.name}」至 v${updated.version}`;
    } catch (error) {
      if (error.code === "unchanged") return unchangedMessage;
      throw error;
    }
  }
  const nameInput = $("#upload-name");
  nameInput.value = "";
  while (true) {
    if (nameMessage) {
      $("#upload-name-message").textContent = nameMessage;
      if (await askUploadChoice($("#upload-name-dialog")) !== "create") return "已取消";
      form.set("name", nameInput.value.trim());
    }
    try {
      const created = await api("/api/configs", { method: "POST", body: form }).then(response => response.json());
      return `已创建「${created.name}」`;
    } catch (error) {
      if (error.code === "name_invalid") {
        nameInput.value = Array.from(nameInput.value || file.name.replace(/\.ya?ml$/i, "")).slice(0, 100).join("");
        nameMessage = error.message;
        continue;
      }
      if (error.status !== 409) throw error;
      nameMessage = `「${file.name}」的显示名称已被使用，请填写其他名称。`;
    }
  }
}

let uploadQueue = null;
function uploadUnavailable() {
  const reason = $("#login-notice").hidden ? $("#service-status").textContent : "需要重新登录";
  return `${reason}，暂时无法上传，请稍后再试。`;
}
async function uploadFiles(files, config = null) {
  if (!files.length) return;
  if (!ready) {
    notify(uploadUnavailable(), true);
    return;
  }
  const batch = Array.from(files, file => ({file, config}));
  if (uploadQueue) {
    for (const entry of batch) uploadQueue.push(entry);
    notify(`已将 ${batch.length} 个文件加入上传队列。`);
    return;
  }
  if (busy) { notify("正在处理其他操作，请稍后再上传。", true); return; }
  uploadQueue = batch;
  busy = true; render();
  const reports = []; let failed = false;
  try {
    while (uploadQueue.length) {
      const pending = uploadQueue;
      uploadQueue = [];
      for (const {file, config} of pending) {
        notify(`正在处理：${file.name}`);
        try {
          if (!ready) throw new Error(uploadUnavailable());
          const result = await uploadFile(file, config);
          reports.push(`${file.name}：${result}`);
        } catch (error) { failed = true; reports.push(`${file.name}：${error.message}`); }
      }
      // Keep accepting drops until this refresh and any newly queued batches finish.
      await load();
    }
  } finally {
    uploadQueue = null;
    busy = false;
    render();
  }
  notify(reports.join("\n"), failed);
}
for (const id of ["#upload-top", "#upload-select"]) $(id).addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", async e => { await uploadFiles(e.target.files); e.target.value = ""; });
$("#replace-input").addEventListener("change", async e => { await uploadFiles(e.target.files, replacement); e.target.value = ""; replacement = null; });
for (const event of ["dragenter", "dragover"]) $("#dropzone").addEventListener(event, e => { e.preventDefault(); $("#dropzone").classList.add("dragover"); });
$("#dropzone").addEventListener("dragleave", () => $("#dropzone").classList.remove("dragover"));
$("#dropzone").addEventListener("drop", e => { e.preventDefault(); $("#dropzone").classList.remove("dragover"); uploadFiles(e.dataTransfer.files); });
window.addEventListener("dragover", e => e.preventDefault());
window.addEventListener("drop", e => {
  if (!e.defaultPrevented && uploadQueue) uploadFiles(e.dataTransfer.files);
  e.preventDefault();
});
$("#search").addEventListener("input", render); $("#refresh").addEventListener("click", load);
for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => document.getElementById(button.dataset.close).close());
$("#rename-form").addEventListener("submit", async e => {
  e.preventDefault(); if (!editing) return;
  const config = editing;
  const dialog = $("#rename-dialog");
  const fields = {expected_updated_at: config.updated_at};
  if ($("#rename-name").value !== config.name) fields.name = $("#rename-name").value;
  if ($("#rename-description").value !== config.description) fields.description = $("#rename-description").value;
  if (Object.keys(fields).length === 1) { dialog.close(); return; }
  const saved = await mutate(() => api(`/api/configs/${config.id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(fields)
  }), "配置信息已更新。", null, error => {
    if (!dialog.open || editing !== config) return;
    if (error.code === "stale" && error.current) {
      if (!("name" in fields)) $("#rename-name").value = error.current.name;
      if (!("description" in fields)) $("#rename-description").value = error.current.description;
      editing = error.current;
    }
    $("#rename-error").textContent = error.code === "stale" ? "其他页面已修改此配置，请核对输入后再次保存。" : error.message;
  });
  if (saved && dialog.open && editing === config) dialog.close();
});
$("#link-dialog").addEventListener("close", () => { $("#link-value").value = ""; });
function refreshPage() { load(); loadBackups(); }
window.addEventListener("online", refreshPage);
changes?.addEventListener("message", refreshPage);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshPage(); });
let theme = "system";
try { theme = localStorage.getItem("mcm-theme") || "system"; } catch (_) { /* storage can be disabled */ }
function setTheme() { if (!["system", "light", "dark"].includes(theme)) theme = "system"; document.documentElement.dataset.theme = theme; $("#theme").textContent = { system: "跟随系统", light: "浅色模式", dark: "深色模式" }[theme]; }
setTheme(); $("#theme").addEventListener("click", () => { theme = { system: "light", light: "dark", dark: "system" }[theme]; setTheme(); try { localStorage.setItem("mcm-theme", theme); } catch (_) {} });
load();

let historySession;
async function showHistory(config) {
  const dialog = $("#history-dialog");
  const session = {config};
  historySession = session;
  $("#history-error").textContent = "";
  $("#history-title").textContent = `历史版本 · ${config.name}`;
  $("#history-list").textContent = "正在读取版本…";
  if (!dialog.open) dialog.showModal();
  await refreshHistory(session);
}
async function refreshHistory(session) {
  const {config} = session;
  const dialog = $("#history-dialog");
  if (!dialog.open || historySession !== session) return;
  let items;
  try {
    items = await api(`/api/configs/${config.id}/versions`).then(r => r.json());
  } catch (error) {
    if (dialog.open && historySession === session) {
      $("#history-list").replaceChildren();
      if (error.status === 404) dialog.close();
      notify(error.status === 404 ? "配置已被删除" : error.message, true);
    }
    return;
  }
  if (!dialog.open || historySession !== session) return;
  const current = items.find(v => v.is_current);
  const list = $("#history-list"); list.replaceChildren();
  for (const item of items) {
    const row = document.createElement("article"); row.className = "history-row";
    const info = document.createElement("p");
    info.textContent = `版本 ${item.version}${item.is_current ? " · 当前" : ""}${item.source_version ? ` · 回滚自 ${item.source_version}` : ""} · ${item.filename} · ${bytes(item.size)} · ${new Date(item.created_at).toLocaleString()}\nSHA-256：${item.sha256}`;
    row.append(info);
    const preview = document.createElement("button"); preview.textContent = "预览";
    preview.onclick = async () => { try { const r = await api(`/api/configs/${config.id}/versions/${item.version}/preview`); $("#preview-content").textContent = await r.text(); $("#preview-title").textContent = `${config.name} · 版本 ${item.version}`; $("#preview-dialog").showModal(); } catch(e) { notify(e.message, true); } };
    row.append(preview);
    if (!item.is_current && item.sha256 === current.sha256) {
      const note = document.createElement("span"); note.className = "history-note"; note.textContent = "内容与当前版本相同";
      row.append(note);
    }
    if (!item.is_current) {
      const sameContent = item.sha256 === current.sha256;
      const rollback = document.createElement("button");
      rollback.textContent = sameContent ? "校验并修复当前文件" : "回滚到此版本";
      rollback.disabled = !ready || busy;
      rollback.onclick = async () => {
        const reason = mutationBlocked();
        if (reason) { $("#history-error").textContent = reason; return; }
        $("#history-error").textContent = "";
        const prompt = sameContent
          ? "校验当前文件？如果文件完整则跳过回滚，损坏时从此版本恢复。"
          : `回滚到版本 ${item.version}？将生成新版本，订阅链接和启停状态保持不变。`;
        if (!confirm(prompt)) return;
        await mutate(
          () => api(`/api/configs/${config.id}/versions/${item.version}/rollback`, {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({expected_current_version: current.version})
          }),
          sameContent ? `已从版本 ${item.version} 修复当前文件` : "回滚完成，订阅链接保持不变。",
          sameContent ? "当前文件完整，无需修复" : null
        );
        await refreshHistory(session);
      };
      row.append(rollback);
    }
    list.append(row);
  }
}
let backupTimer;
async function loadBackups() {
  clearTimeout(backupTimer);
  try {
    const {items, used_bytes, quota_bytes, max_bytes} = await api("/api/backups").then(r => r.json());
    $("#backup-quota").textContent = `已用 ${bytes(used_bytes)} / 上限 ${bytes(quota_bytes)} · 单次上限 ${bytes(max_bytes)}`;
    const list = $("#backup-list"); list.replaceChildren();
    const labels = {queued:"等待处理",snapshotting:"创建快照",encrypting:"加密中",ready:"已完成",failed:"创建失败",unregistered:"未登记的备份文件"};
    const reasons = {quota_exceeded:"超出备份配额，请删除旧备份",too_large:"超过单次备份上限",disk_space:"磁盘空间不足",storage_error:"存储读写失败",interrupted:"任务已中断",archive_missing:"归档缺失",archive_corrupt:"归档校验失败"};
    for (const item of items) {
      const row = document.createElement("article"); row.className = "history-row";
      const text = document.createElement("p"); text.textContent = `${new Date(item.created_at).toLocaleString()} · ${labels[item.status]} · ${bytes(item.size)}${item.sha256 ? `\nSHA-256：${item.sha256}` : ""}`; row.append(text);
      if (item.error_code || item.filename) text.append(" · ", item.filename || reasons[item.error_code] || "请检查存储");
      if (item.status === "ready") { const a = document.createElement("a"); a.textContent = "下载加密备份"; a.href = `/api/backups/${item.id}/download`; a.download = ""; row.append(a); }
      if (["ready","failed","unregistered"].includes(item.status)) { const b = document.createElement("button"); b.textContent = "删除"; b.onclick = async () => { if (!confirm("删除服务器上的这份备份？已下载的副本不受影响。")) return; try { await api(`/api/backups/${item.id}`, {method:"DELETE"}); await loadBackups(); } catch(e) {notify(e.message,true);} }; row.append(b); }
      list.append(row);
    }
    const active = items.some(i => !["ready","failed","unregistered"].includes(i.status));
    $("#backup-new").disabled = active || !$("#login-notice").hidden;
    if (active) backupTimer = setTimeout(() => {loadBackups(); load();}, 2000);
  } catch(e) {notify(e.message,true);}
}
$("#backup-new").onclick = () => $("#backup-dialog").showModal();
$("#backup-refresh").onclick = loadBackups;
$("#backup-dialog").addEventListener("close", () => $("#backup-form").reset());
$("#backup-form").onsubmit = async e => {
  e.preventDefault();
  if ($("#backup-password").value !== $("#backup-confirm").value) {notify("两次密码不一致",true); return;}
  let body = JSON.stringify({password:$("#backup-password").value});
  $("#backup-dialog").close(); $("#backup-form").reset();
  try { await api("/api/backups", {method:"POST",headers:{"Content-Type":"application/json"},body}); await loadBackups(); } catch(e) {notify(e.message,true);} finally {body=null;}
};
loadBackups();
