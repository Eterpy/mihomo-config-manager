"use strict";
const $ = (selector) => document.querySelector(selector);
const csrf = $('meta[name="csrf-token"]').content;
let configs = [], ready = false, busy = false, replacement = null, editing = null;
function notify(message, error = false) {
  const el = $("#notice"); el.textContent = message; el.classList.toggle("error", error); el.hidden = false;
}
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.method && !["GET", "HEAD"].includes(options.method)) headers.set("X-CSRF-Token", csrf);
  const response = await fetch(path, { ...options, headers, credentials: "same-origin", redirect: "error" });
  if (!response.ok) {
    let message = `请求失败（${response.status}），请刷新页面或重新登录`;
    try { message = (await response.json()).detail || message; } catch (_) { /* no response content logged */ }
    const error = new Error(message); error.status = response.status; throw error;
  }
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
async function load() {
  try {
    const [items, state] = await Promise.all([api("/api/configs").then(r => r.json()), api("/api/status").then(r => r.json())]);
    configs = items; ready = state.ready && !state.snapshotting;
    $("#service-status").textContent = state.snapshotting ? "备份快照中" : ready ? "运行正常" : "需要恢复";
    if (state.snapshotting) notify("正在创建一致快照，配置写入暂缓。");
    else if (!ready) notify("存储存在未完成操作。请检查服务器磁盘和权限，然后重启管理端恢复。", true);
  } catch (error) { ready = false; $("#service-status").textContent = "连接异常"; notify(error.message, true); }
  render();
}
async function mutate(task, message) {
  if (busy || !ready) return;
  busy = true; render();
  try { await task(); notify(message); }
  catch (error) { notify(error.message, true); }
  finally { busy = false; await load(); }
}
async function action(type, config) {
  const base = `/api/configs/${config.id}`;
  if (type === "copy") {
    try { await navigator.clipboard.writeText(config.subscription_url); notify("订阅链接已复制，请妥善保管。"); }
    catch (_) { $("#link-value").value = config.subscription_url; $("#link-dialog").showModal(); $("#link-value").select(); }
  } else if (type === "history") {
    await showHistory(config);
  } else if (type === "preview") {
    const response = await api(`${base}/preview`);
    $("#preview-content").textContent = await response.text(); $("#preview-title").textContent = config.name; $("#preview-dialog").showModal();
  } else if (type === "replace") {
    replacement = config; $("#replace-input").click();
  } else if (type === "rename") {
    editing = config; $("#rename-name").value = config.name; $("#rename-description").value = config.description; $("#rename-dialog").showModal();
  } else if (type === "toggle") {
    await mutate(() => api(`${base}/${config.enabled ? "disable" : "enable"}`, { method: "POST" }), config.enabled ? "已停用，订阅链接不可访问。" : "已启用，订阅链接恢复可用。");
  } else if (type === "rotate" && window.confirm(`重置「${config.name}」的 Token？\n旧订阅链接将失效，你需要在客户端更新链接。`)) {
    await mutate(() => api(`${base}/rotate-token`, { method: "POST" }), "Token 已重置。请复制新链接并更新客户端订阅。");
  } else if (type === "delete" && window.confirm(`确定删除「${config.name}」？\n订阅立即失效，配置和历史文件将被删除，此操作无法撤销。`)) {
    await mutate(() => api(base, { method: "DELETE" }), "配置已删除，旧订阅链接已失效。");
  }
}
async function uploadFiles(files, config = null) {
  if (busy || !ready || !files.length) return;
  busy = true; render();
  const reports = []; let failed = false;
  for (const file of Array.from(files)) {
    notify(`正在上传：${file.name}`);
    const form = new FormData(); form.append("file", file);
    const path = config ? `/api/configs/${config.id}/content` : "/api/configs";
    const send = () => api(path, { method: config ? "PUT" : "POST", body: form });
    try {
      try { await send(); }
      catch (error) {
        if (error.status !== 409 || config) throw error;
        const name = window.prompt("显示名称已存在，请输入新的显示名称：", file.name.replace(/\.ya?ml$/i, ""));
        if (!name) throw new Error("已取消上传");
        form.set("name", name); await send();
      }
      reports.push(`${file.name}：${config ? "新版本已发布，订阅链接保持不变" : "上传成功"}`);
    } catch (error) { failed = true; reports.push(`${file.name}：${error.message}`); }
  }
  busy = false; await load(); notify(reports.join("\n"), failed);
}
for (const id of ["#upload-top", "#upload-select"]) $(id).addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", async e => { await uploadFiles(e.target.files); e.target.value = ""; });
$("#replace-input").addEventListener("change", async e => { await uploadFiles(e.target.files, replacement); e.target.value = ""; replacement = null; });
for (const event of ["dragenter", "dragover"]) $("#dropzone").addEventListener(event, e => { e.preventDefault(); $("#dropzone").classList.add("dragover"); });
$("#dropzone").addEventListener("dragleave", () => $("#dropzone").classList.remove("dragover"));
$("#dropzone").addEventListener("drop", e => { e.preventDefault(); $("#dropzone").classList.remove("dragover"); uploadFiles(e.dataTransfer.files); });
window.addEventListener("dragover", e => e.preventDefault()); window.addEventListener("drop", e => e.preventDefault());
$("#search").addEventListener("input", render); $("#refresh").addEventListener("click", load);
for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => document.getElementById(button.dataset.close).close());
$("#rename-form").addEventListener("submit", async e => {
  e.preventDefault(); if (!editing || busy) return;
  const config = editing; $("#rename-dialog").close();
  await mutate(() => api(`/api/configs/${config.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: $("#rename-name").value, description: $("#rename-description").value }) }), "配置信息已更新。");
});
let theme = "system";
try { theme = localStorage.getItem("mcm-theme") || "system"; } catch (_) { /* storage can be disabled */ }
function setTheme() { if (!["system", "light", "dark"].includes(theme)) theme = "system"; document.documentElement.dataset.theme = theme; $("#theme").textContent = { system: "跟随系统", light: "浅色模式", dark: "深色模式" }[theme]; }
setTheme(); $("#theme").addEventListener("click", () => { theme = { system: "light", light: "dark", dark: "system" }[theme]; setTheme(); try { localStorage.setItem("mcm-theme", theme); } catch (_) {} });
load();

async function showHistory(config) {
  const items = await api(`/api/configs/${config.id}/versions`).then(r => r.json());
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
    if (!item.is_current) {
      const rollback = document.createElement("button"); rollback.textContent = "回滚到此版本"; rollback.disabled = !ready || busy;
      rollback.onclick = async () => {
        if (!confirm(`回滚到版本 ${item.version}？将生成新版本，订阅链接和启停状态保持不变。`)) return;
        await mutate(() => api(`/api/configs/${config.id}/versions/${item.version}/rollback`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({expected_current_version:current.version})}), "回滚完成，订阅链接保持不变。");
        await showHistory(config);
      };
      row.append(rollback);
    }
    list.append(row);
  }
  if (!$("#history-dialog").open) $("#history-dialog").showModal();
}
let backupTimer;
async function loadBackups() {
  clearTimeout(backupTimer);
  try {
    const items = await api("/api/backups").then(r => r.json());
    const list = $("#backup-list"); list.replaceChildren();
    const labels = {queued:"等待处理",snapshotting:"创建快照",encrypting:"加密中",ready:"已完成",failed:"失败，请检查存储后重试"};
    for (const item of items) {
      const row = document.createElement("article"); row.className = "history-row";
      const text = document.createElement("p"); text.textContent = `${new Date(item.created_at).toLocaleString()} · ${labels[item.status]} · ${bytes(item.size)}${item.sha256 ? `\nSHA-256：${item.sha256}` : ""}`; row.append(text);
      if (item.status === "ready") { const a = document.createElement("a"); a.textContent = "下载加密备份"; a.href = `/api/backups/${item.id}/download`; a.download = ""; row.append(a); }
      if (["ready","failed"].includes(item.status)) { const b = document.createElement("button"); b.textContent = "删除"; b.onclick = async () => { if (!confirm("删除服务器上的这份备份？已下载的副本不受影响。")) return; try { await api(`/api/backups/${item.id}`, {method:"DELETE"}); await loadBackups(); } catch(e) {notify(e.message,true);} }; row.append(b); }
      list.append(row);
    }
    const active = items.some(i => !["ready","failed"].includes(i.status));
    $("#backup-new").disabled = active;
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
