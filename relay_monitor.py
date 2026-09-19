#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中转站模型探针 (本地网页版, 支持 OpenAI 协议)

双击「双击启动监测面板.command」或运行:  python3 relay_monitor.py
打开:  http://localhost:8765   (可用 PORT=9000 环境变量换端口)

功能:
  - 添加多个中转站(任意 OpenAI 兼容协议), 拉取模型列表后下拉选择某个模型
  - 对选中模型探测: chat/completions + max_tokens=50, 单次/持续监测
  - 检测到模型可用时: 页面弹窗 + 系统通知 + 响铃
  - 配置保存于本地 relay_monitor_config.json (服务端持久化, 换浏览器不丢)

性能设计:
  - 每个中转站每轮只探测 1 个选中模型
  - 服务端全局共享 8 线程池, 空闲零开销; 无任何后台定时器
"""

import json
import os
import time
import uuid
import urllib.request
import urllib.error
import urllib.parse
import ssl
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8765"))
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay_monitor_config.json")

# 中转站证书链各异, 不做严格校验(仅本机自用)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# 全局共享探测线程池: 有任务才起线程, 空闲时不占资源
POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="probe")

MAX_TOKENS = 50  # 每次探测请求的 max_tokens

CODEX_UA = "codex_cli_rs/0.42.0 (Mac OS 15.6.0; arm64)"

# codex CLI 随请求携带的系统指令文本(完整指纹的一部分)
CODEX_INSTRUCTIONS = """You are Codex, based on GPT-5. You are running as a coding agent in the Codex CLI on a user's computer.

# Personality

You are a collaborative expert software engineer. You are pair programming with the user, helping them understand and modify their codebase. You are pragmatic: you prefer the smallest change that solves the real problem, and you say plainly when something is a bad idea.

# General rules

- Work only inside the current working directory unless the user explicitly asks otherwise.
- Do not delete data or modify files unless explicitly asked; prefer reading and explaining over changing.
- Match the style of the surrounding code: formatting, naming, and idiom. Keep comments rare and meaningful.
- When you need information, gather it with quick, targeted searches instead of asking the user.
- Before running a command that changes system state, check that the evidence supports it.

# Coding style

- Write clear, readable code in complete sentences of logic; do not compress with abbreviations or arrow chains.
- Prefer simple, direct implementations. Add tests only when the user asks for them.
- Report outcomes faithfully: if tests fail, say so with the output; if a step was skipped, say that.

# Output

- Lead with the outcome. Give a short summary of what changed and why, then supporting detail.
- Use GitHub-flavored markdown when responding. Reference code as file paths with line numbers when helpful."""


def norm_base(base):
    """根地址自动补 /v1, 去尾部斜杠 (借鉴 keepalive 脚本的 URL 补全)"""
    base = base.strip().rstrip("/")
    p = urllib.parse.urlparse(base)
    if not p.path:  # 只填了域名根地址
        base += "/v1"
    return base


def build_opener(proxy=""):
    """proxy 为空直连; 否则走指定 HTTP/SOCKS 代理 (如 http://127.0.0.1:7890)"""
    handlers = [urllib.request.HTTPSHandler(context=SSL_CTX)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def probe_one(base, key, model, fmt="chat", ua="", session_id="", proxy=""):
    """对单个模型发探测请求, 返回结果 dict
    fmt: chat = OpenAI Chat Completions / responses = OpenAI Responses(原生)
         codex = 完全模拟 codex CLI 终端请求 (Responses+流式+完整请求头指纹)
         anthropic = Anthropic Messages (base 需含 /v1)"""
    sid = session_id or str(uuid.uuid4())
    if fmt == "codex":
        url = base + "/responses"
        payload = {
            "model": model,
            "instructions": CODEX_INSTRUCTIONS,
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "hi"}]}],
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": "medium", "summary": "auto"},
            "max_output_tokens": MAX_TOKENS,
            "prompt_cache_key": sid,
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": ua or CODEX_UA,
            "originator": "codex_cli_rs",
            "OpenAI-Beta": "responses=experimental",
            "session-id": sid,
            "thread-id": sid,
            "X-Client-Request-Id": str(uuid.uuid4()),
        }
    elif fmt == "responses":
        url = base + "/responses"
        # 借鉴 keepalive 脚本: 严格上游要求这些字段, 否则 400 invalid_responses_request
        payload = {"model": model, "input": "hi", "max_output_tokens": MAX_TOKENS,
                   "store": False, "include": ["reasoning.encrypted_content"]}
        if session_id:
            payload["prompt_cache_key"] = session_id
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    elif fmt == "anthropic":
        url = base + "/messages"
        payload = {"model": model, "max_tokens": MAX_TOKENS,
                   "messages": [{"role": "user", "content": "hi"}]}
        headers = {"x-api-key": key, "Authorization": f"Bearer {key}",
                   "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    else:
        url = base + "/chat/completions"
        payload = {"model": model, "messages": [{"role": "user", "content": "hi"}],
                   "max_tokens": MAX_TOKENS}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if ua:
        headers["User-Agent"] = ua
        if "codex" in ua.lower():
            headers["originator"] = "codex_cli_rs"  # codex CLI 会携带此头, 部分中转站校验
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    t0 = time.time()
    try:
        with build_opener(proxy).open(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        code = e.code
    except Exception as e:
        return {"model": model, "ok": False, "code": 0,
                "err": f"{type(e).__name__}: {e}", "latency_ms": int((time.time() - t0) * 1000)}
    latency = int((time.time() - t0) * 1000)
    if code == 200:
        return {"model": model, "ok": True, "code": 200, "latency_ms": latency, "err": None}
    if code == 429:
        return {"model": model, "ok": False, "code": 429,
                "err": "429 限流(服务在线)", "latency_ms": latency}
    # 截取错误信息中的 message 字段, 便于直读
    err = body[:200]
    try:
        j = json.loads(body)
        msg = (j.get("error") or {}).get("message") or j.get("message")
        if msg:
            err = str(msg)[:200]
    except Exception:
        pass
    return {"model": model, "ok": False, "code": code, "err": err, "latency_ms": latency}


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>中转站模型探针</title>
<style>
  :root { --bg:#0f1115; --card:#1a1d24; --line:#2a2e38; --txt:#e6e8ee; --sub:#8b93a5;
          --ok:#34c759; --warn:#ff9f0a; --err:#ff453a; --blue:#0a84ff; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--txt); font:14px/1.6 -apple-system,"PingFang SC",sans-serif; padding:24px; }
  h1 { font-size:20px; margin-bottom:4px; }
  .hint { color:var(--sub); font-size:12px; margin-bottom:20px; }
  .toolbar { display:flex; gap:10px; margin-bottom:20px; flex-wrap:wrap; }
  button { background:var(--blue); color:#fff; border:0; border-radius:8px; padding:8px 14px; font-size:13px; cursor:pointer; }
  button.gray { background:var(--line); color:var(--txt); }
  button.danger { background:#5a2a2e; color:#ff8080; }
  button:disabled { opacity:.4; cursor:not-allowed; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(420px,1fr)); gap:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }
  .card.running { border-color:var(--blue); }
  .card.ok { border-color:var(--ok); }
  .status { display:inline-block; padding:2px 10px; border-radius:99px; font-size:12px; background:var(--line); color:var(--sub); }
  .status.running { background:#12305c; color:#7cb8ff; }
  .status.ok { background:#123a20; color:var(--ok); }
  .status.warn { background:#4a3608; color:var(--warn); }
  .status.err { background:#4a1414; color:var(--err); }
  .fields { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin:12px 0; }
  label { font-size:11px; color:var(--sub); display:block; margin-bottom:2px; }
  input, select { width:100%; background:#12141a; border:1px solid var(--line); border-radius:6px;
                  color:var(--txt); padding:7px 9px; font-size:13px; }
  input:focus, select:focus { outline:1px solid var(--blue); }
  .row { display:flex; gap:8px; align-items:center; margin-top:10px; flex-wrap:wrap; }
  .row .spacer { flex:1; }
  .result { margin-top:10px; font-size:12px; color:var(--sub); min-height:18px; word-break:break-all; }
  .log { margin-top:8px; background:#12141a; border-radius:6px; padding:8px; height:96px;
         overflow-y:auto; font:11px/1.7 Menlo,monospace; color:var(--sub); white-space:pre-wrap; word-break:break-all; }
  #overlay { position:fixed; inset:0; background:rgba(0,0,0,.6); display:none; align-items:center; justify-content:center; z-index:99; }
  #modal { background:var(--card); border:1px solid var(--ok); border-radius:14px; padding:28px 34px; max-width:460px; text-align:center; }
  #modal h2 { color:var(--ok); margin-bottom:10px; }
  #modal p { color:var(--sub); font-size:13px; margin-bottom:18px; white-space:pre-line; word-break:break-all; }
</style>
</head>
<body>
<h1>🛰️ 中转站模型探针</h1>
<div class="hint">拉取模型列表 → 下拉选择模型 → 探测 · 双间隔(失败探活/成功保活) · 根地址自动补 /v1 · 配置存于本机</div>
<div class="toolbar">
  <button onclick="addCard()">＋ 添加中转站</button>
  <button class="gray" onclick="targets.forEach(t=>{if(t.currentModel)scan(t,true)})">全部探测一次</button>
  <button class="danger" onclick="notifyTest()">测试提醒</button>
</div>
<div class="grid" id="grid"></div>

<div id="overlay"><div id="modal">
  <h2 id="m-title">✅ 模型可用</h2>
  <p id="m-body"></p>
  <button onclick="closeModal()">知道了</button>
</div></div>

<script>
const $ = s => document.querySelector(s);
let uid = 0;

/* User-Agent 预设 */
const UA_PRESETS = [
  "claude-cli/2.1.161 (external, cli)",
  "claude-cli/2.1.161",
  "claude-code/1.0.0",
  "claude-code/0.1.0",
  "Kilo-Code/1.0",
  "codex_cli_rs/0.42.0 (Mac OS 15.6.0; arm64)",
  "codex_cli_rs/0.21.0",
  "codex/1.0.0",
];

/* ---------- 提醒 ---------- */
function beep(times = 3) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    for (let i = 0; i < times; i++) {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = 880;
      g.gain.setValueAtTime(0.001, ctx.currentTime + i * 0.45);
      g.gain.exponentialRampToValueAtTime(0.3, ctx.currentTime + i * 0.45 + 0.03);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + i * 0.45 + 0.4);
      o.connect(g).connect(ctx.destination);
      o.start(ctx.currentTime + i * 0.45); o.stop(ctx.currentTime + i * 0.45 + 0.42);
    }
  } catch (e) {}
}
function sysNotify(title, body) {
  if (!("Notification" in window)) return;
  if (Notification.permission === "granted") new Notification(title, { body });
  else if (Notification.permission !== "denied")
    Notification.requestPermission().then(p => { if (p === "granted") new Notification(title, { body }); });
}
let modalTimer = null;
function popup(title, body) {
  beep(); sysNotify(title, body);
  $("#m-title").textContent = title; $("#m-body").textContent = body;
  $("#overlay").style.display = "flex";
  clearTimeout(modalTimer); modalTimer = setTimeout(closeModal, 30000);
}
function closeModal() { $("#overlay").style.display = "none"; }
function notifyTest() { popup("🔔 提醒测试", "看到弹窗并听到铃声说明提醒正常。"); }

/* ---------- 配置持久化 ---------- */
let targets = [];
async function load() {
  let list = [];
  try {
    const j = await (await fetch("/api/config")).json();
    if (Array.isArray(j.targets) && j.targets.length) list = j.targets;
  } catch (e) {}
  if (!list.length) { try { list = JSON.parse(localStorage.getItem("relay_targets") || "[]"); } catch (e) {} }
  targets = list.map(t => Object.assign(
    { probeInterval: 120, keepaliveInterval: 120, format: "chat", ua: "", codexMode: false, proxy: "" }, t, {
    running: false, timer: null,
    // 状态不持久化, 页面打开后以新一轮探测结果为准
    models: (t.models || []).map(m => ({ name: m.name, status: "idle", code: 0, err: null, latency: 0 })),
  }));
  targets.forEach(t => { if (!t.sessionId) t.sessionId = crypto.randomUUID(); });
  uid = targets.reduce((m, t) => Math.max(m, parseInt(t.id, 10) || 0), 0);
  render();
}
function save() {
  const data = JSON.stringify({ targets: targets.map(t => ({
    id: t.id, name: t.name, base: t.base, key: t.key,
    probeInterval: t.probeInterval, keepaliveInterval: t.keepaliveInterval,
    format: t.format || "chat", ua: t.ua || "", sessionId: t.sessionId || "",
    codexMode: !!t.codexMode, proxy: t.proxy || "",
    currentModel: t.currentModel || "", models: t.models.map(m => ({ name: m.name })) })) });
  localStorage.setItem("relay_targets", data);
  fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: data }).catch(() => {});
}

/* ---------- 目标管理 ---------- */
function addCard(t) {
  t = t || {};
  targets.push(Object.assign({
    id: (++uid + Date.now()), name: "", base: "https://", key: "",
    probeInterval: 120, keepaliveInterval: 120, format: "chat", ua: "", codexMode: false, proxy: "",
    sessionId: crypto.randomUUID(), currentModel: "", models: [], running: false, timer: null,
  }, t, { running: false, timer: null }));
  render(); save();
}
function delCard(id) {
  const t = targets.find(x => x.id === id); if (t) stopWatch(t);
  targets = targets.filter(x => x.id !== id);
  render(); save();
}
function field(id, k, v) {
  const t = targets.find(x => x.id === id); if (!t) return;
  if (k === "codexMode") t[k] = v.checked;
  else {
    const isNum = k === "probeInterval" || k === "keepaliveInterval";
    t[k] = isNum ? (parseInt(v.value) || 120) : v.value.trim();
  }
  save();
}

/* ---------- 渲染 ---------- */
function esc(s) { return (s || "").replace(/"/g, "&quot;").replace(/</g, "&lt;"); }
function modelOptions(t) {
  const names = t.models.map(m => m.name);
  if (t.currentModel && !names.includes(t.currentModel)) names.unshift(t.currentModel);
  let html = `<option value="">${t.models.length ? "— 请选择模型 —" : "— 请先拉取模型列表 —"}</option>`;
  html += names.map(n => `<option value="${esc(n)}" ${n === t.currentModel ? "selected" : ""}>${esc(n)}</option>`).join("");
  html += `<option value="__manual__">✏️ 手动输入…</option>`;
  return html;
}
function render() {
  $("#grid").innerHTML = targets.map(t => `
  <div class="card ${t.running ? "running" : ""}" id="card-${t.id}">
    <div class="row" style="margin:0 0 4px">
      <input style="flex:1" placeholder="备注名(可选)" value="${esc(t.name)}" oninput="field(${t.id},'name',this)">
      <span class="status" id="st-${t.id}">${t.running ? "监测中" : "待机"}</span>
    </div>
    <div class="fields">
      <div style="grid-column:1/3"><label>API 地址 (OpenAI 兼容 Base URL)</label>
        <input value="${esc(t.base)}" oninput="field(${t.id},'base',this)" placeholder="https://api.example.com/v1"></div>
      <div style="grid-column:1/3"><label>API Key</label>
        <input type="password" value="${esc(t.key)}" oninput="field(${t.id},'key',this)" placeholder="sk-..."></div>
      <div><label>模型 (下拉选择)</label>
        <select onchange="onModelChange(${t.id},this)">${modelOptions(t)}</select></div>
      <div><label>上游格式</label>
        <select onchange="field(${t.id},'format',this)">
          <option value="chat" ${t.format !== "responses" && t.format !== "anthropic" ? "selected" : ""}>Chat Completions</option>
          <option value="responses" ${t.format === "responses" ? "selected" : ""}>Responses (原生)</option>
          <option value="anthropic" ${t.format === "anthropic" ? "selected" : ""}>Anthropic Messages</option>
        </select></div>
      <div><label>探活间隔 (失败时, 秒)</label>
        <input type="number" min="15" value="${t.probeInterval}" oninput="field(${t.id},'probeInterval',this)"></div>
      <div><label>保活间隔 (成功后, 秒)</label>
        <input type="number" min="15" value="${t.keepaliveInterval}" oninput="field(${t.id},'keepaliveInterval',this)"></div>
      <div style="grid-column:1/3"><label>User-Agent (预设或自定义)</label>
        <select onchange="onUaChange(${t.id},this)">
          <option value="">不设置(程序默认)</option>
          ${UA_PRESETS.map(u => `<option value="${esc(u)}" ${t.ua === u ? "selected" : ""}>${esc(u)}</option>`).join("")}
          ${t.ua && !UA_PRESETS.includes(t.ua) ? `<option value="${esc(t.ua)}" selected>${esc(t.ua)}</option>` : ""}
          <option value="__custom__" ${t.ua && !UA_PRESETS.includes(t.ua) ? "" : ""}>✏️ 自定义…</option>
        </select></div>
      <div style="grid-column:1/3"><label class="chk">
        <input type="checkbox" ${t.codexMode ? "checked" : ""} onchange="field(${t.id},'codexMode',this)">
        完全模拟 Codex 终端请求 (强制 Responses + 流式 + codex 请求头与 instructions 指纹, UA 留空时自动用 codex_cli_rs)</label></div>
      <div style="grid-column:1/3"><label>代理 (可选, 如 Clash: http://127.0.0.1:7890)</label>
        <input value="${esc(t.proxy)}" oninput="field(${t.id},'proxy',this)" placeholder="http://127.0.0.1:7890"></div>
    </div>
    <div class="row" style="margin:4px 0 0">
      <button class="gray" onclick="fetchModels(targets.find(x=>x.id===${t.id}))">📋 拉取模型列表</button>
      <button onclick="scan(targets.find(x=>x.id===${t.id}),true)" ${t.currentModel ? "" : "disabled"}>▶ 探测一次</button>
      <button onclick="toggleWatch(targets.find(x=>x.id===${t.id}))">${t.running ? "⏹ 停止监测" : "⏱ 持续监测"}</button>
      <span class="spacer"></span>
      <button class="danger" onclick="if(confirm('确定删除?'))delCard(${t.id})">删除</button>
    </div>
    <div class="result" id="res-${t.id}">尚未探测</div>
    <div class="log" id="log-${t.id}">日志…</div>
  </div>`).join("") || `<div style="color:var(--sub)">还没有监测目标, 点击「添加中转站」开始。</div>`;
}
function onModelChange(id, sel) {
  const t = targets.find(x => x.id === id); if (!t) return;
  if (sel.value === "__manual__") {
    const name = (prompt("输入模型名称:") || "").trim();
    if (name) {
      if (!t.models.some(m => m.name === name)) t.models.push({ name, status: "idle", code: 0, err: null, latency: 0 });
      t.currentModel = name;
    }
  } else {
    t.currentModel = sel.value;
  }
  save(); render();
}
function onUaChange(id, sel) {
  const t = targets.find(x => x.id === id); if (!t) return;
  if (sel.value === "__custom__") {
    const v = (prompt("输入自定义 User-Agent:") || "").trim();
    t.ua = v || "";
  } else {
    t.ua = sel.value;
  }
  save(); render();
}
function setStatus(t, text, cls) {
  const el = document.querySelector(`#st-${t.id}`); if (!el) return;
  el.className = "status " + (cls || ""); el.textContent = text;
  document.querySelector(`#card-${t.id}`).classList.toggle("ok", cls === "ok");
}
function setResult(t, text) {
  const el = document.querySelector(`#res-${t.id}`); if (el) el.textContent = text;
}
function log(t, msg) {
  const el = document.querySelector(`#log-${t.id}`); if (!el) return;
  const ts = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  el.textContent = `[${ts}] ${msg}\n` + el.textContent;
  if (el.textContent.length > 5000) el.textContent = el.textContent.slice(0, 5000);
}

/* ---------- 拉取模型列表 ---------- */
async function fetchModels(t) {
  if (!t.base || !t.key) { alert("请先填写 API 地址和 Key"); return; }
  if (!Notification || Notification.permission === "default") Notification.requestPermission();
  log(t, "正在拉取模型列表…");
  try {
    const r = await (await fetch("/api/models", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base: t.base, key: t.key, ua: t.ua || "", proxy: t.proxy || "" }),
    })).json();
    if (r.http_code === 200 && r.models && r.models.length) {
      // 保留已有模型的状态缓存
      const known = new Map(t.models.map(m => [m.name, m]));
      t.models = r.models.map(n => known.get(n) || ({ name: n, status: "idle", code: 0, err: null, latency: 0 }));
      if (!t.currentModel || !t.models.some(m => m.name === t.currentModel)) t.currentModel = "";
      save(); render();
      log(t, `已拉取 ${r.models.length} 个模型, 请在下拉框选择要探测的模型`);
    } else if (r.network_error) {
      log(t, "拉取失败: " + r.error);
    } else {
      log(t, `拉取模型失败 HTTP ${r.http_code} ${r.snippet || ""}`);
    }
  } catch (e) { log(t, "拉取失败: " + e.message); }
}

/* ---------- 探测 ---------- */
async function scan(t, manual) {
  const model = t.currentModel;
  if (!model) { log(t, "请先在下拉框选择模型"); return; }
  setStatus(t, "探测中…", "running");
  setResult(t, `正在探测 ${model} …`);
  let r;
  try {
    r = await (await fetch("/api/probe", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base: t.base, key: t.key,
                             format: t.codexMode ? "codex" : (t.format || "chat"),
                             ua: t.ua || "", session_id: t.sessionId || "",
                             proxy: t.proxy || "", models: [model] }),
    })).json();
  } catch (e) { log(t, "探测请求失败: " + e.message); setStatus(t, t.running ? "监测中" : "待机", ""); return; }
  if (!r.results || !r.results.length) { log(t, "探测失败: " + (r.error || "未知错误")); return; }

  const res = r.results[0];
  // 借鉴 keepalive 脚本: 400 时更换会话 ID, 避免被上游按坏会话持续拒绝
  if (res.code === 400 && t.sessionId) {
    t.sessionId = crypto.randomUUID();
    save();
    log(t, "收到 400, 已自动更换会话 ID");
  }
  const entry = t.models.find(m => m.name === model);
  const wasOk = entry && entry.status === "ok";
  if (entry) Object.assign(entry, {
    status: res.ok ? "ok" : "err", code: res.code, err: res.err, latency: res.latency_ms });

  if (res.ok) {
    setStatus(t, "✅ 可用", "ok");
    setResult(t, `✅ ${model} 可用 · ${res.latency_ms} ms`);
    log(t, `探测 ${model}: ✅ 200 可用 (${res.latency_ms} ms)`);
    // 手动探测成功必提醒; 持续监测只在"不可用→可用"跳变时提醒, 避免每轮骚扰
    if (manual || !wasOk) popup(`✅ ${t.name || t.base} 可用`, `模型 ${model} 可以使用了\n延迟 ${res.latency_ms} ms`);
  } else if (res.code === 429) {
    setStatus(t, "⚠️ 限流", "warn");
    setResult(t, `⚠️ ${model}: 429 限流 (服务在线) · ${res.latency_ms} ms`);
    log(t, `探测 ${model}: 429 限流, 服务已可达`);
    if (manual) popup(`⚠️ ${t.name || t.base} 在线但限流`, `模型 ${model} 返回 429\n服务已可达, 稍后重试通常可用`);
  } else if (res.code === 401 || res.code === 403) {
    setStatus(t, "Key 无效", "err");
    setResult(t, `❌ ${res.code}: ${res.err || "认证失败"}`);
    log(t, `探测 ${model}: ${res.code} 认证失败, 请检查 Key`);
    if (t.running) stopWatch(t);
  } else {
    setStatus(t, "❌ 不可用", "err");
    setResult(t, `❌ ${model}: HTTP ${res.code} ${res.err || ""}`);
    log(t, `探测 ${model}: HTTP ${res.code} ${res.err || ""}`);
  }
  return { ok: res.ok, code: res.code };
}

/* ---------- 持续监测 (双间隔: 失败用探活间隔, 成功后切到保活间隔) ---------- */
function watchTick(t) {
  scan(t, false).then(r => {
    if (!t.running) return;
    const sec = Math.max(15, r && r.ok ? t.keepaliveInterval : t.probeInterval);
    t.timer = setTimeout(() => watchTick(t), sec * 1000);
  });
}
function toggleWatch(t) {
  if (t.running) { stopWatch(t); return; }
  if (!t.base || !t.key || !t.currentModel) { alert("请先填写地址/Key 并在下拉框选择模型"); return; }
  if (!Notification || Notification.permission === "default") Notification.requestPermission();
  t.running = true; setStatus(t, "监测中", "running");
  log(t, `开始持续监测 ${t.currentModel} (探活 ${t.probeInterval}s / 保活 ${t.keepaliveInterval}s)`);
  watchTick(t);
  render();
}
function stopWatch(t) {
  t.running = false;
  if (t.timer) { clearTimeout(t.timer); t.timer = null; }
  setStatus(t, "已停止", "");
  render();
}

load();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 安静模式

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path == "/api/config":
            try:
                with open(CONFIG_FILE, "rb") as f:
                    self._send(200, "application/json", f.read())
            except Exception:
                self._json({"targets": []})
        elif self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        try:
            if self.path == "/api/config":
                data = self._read_body()
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                self._json({"ok": True})
            elif self.path == "/api/models":
                self._handle_models()
            elif self.path == "/api/probe":
                self._handle_probe()
            else:
                self._send(404, "text/plain", b"not found")
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"})

    def _handle_models(self):
        req = self._read_body()
        base = norm_base(str(req.get("base", "")))
        key = str(req.get("key", "")).strip()
        ua = str(req.get("ua") or "").strip()
        proxy = str(req.get("proxy") or "").strip()
        if not (base and key):
            self._json({"error": "参数不完整"})
            return
        headers = {"Authorization": f"Bearer {key}"}
        if ua:
            headers["User-Agent"] = ua
            if "codex" in ua.lower():
                headers["originator"] = "codex_cli_rs"
        r = urllib.request.Request(base + "/models", headers=headers)
        try:
            with build_opener(proxy).open(r, timeout=20) as resp:
                body = resp.read().decode("utf-8", "replace")
                code = resp.status
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            code = e.code
        except Exception as e:
            self._json({"network_error": True, "error": f"{type(e).__name__}: {e}"})
            return
        models = None
        if code == 200:
            try:
                data = json.loads(body)
                models = sorted(m.get("id", "") for m in data.get("data", [])
                                if isinstance(m, dict) and m.get("id"))
            except Exception:
                pass
        self._json({"http_code": code, "models": models, "snippet": body[:300]})

    def _handle_probe(self):
        req = self._read_body()
        base = norm_base(str(req.get("base", "")))
        key = str(req.get("key", "")).strip()
        models = [str(m) for m in (req.get("models") or [])][:100]
        fmt = str(req.get("format") or "chat")
        ua = str(req.get("ua") or "").strip()
        session_id = str(req.get("session_id") or "").strip()
        proxy = str(req.get("proxy") or "").strip()
        if not (base and key and models):
            self._json({"error": "参数不完整"})
            return
        futures = {m: POOL.submit(probe_one, base, key, m, fmt, ua, session_id, proxy) for m in models}
        results = [futures[m].result() for m in models]
        self._json({"results": results})


Server = ThreadingHTTPServer
Server.daemon_threads = True

if __name__ == "__main__":
    print(f"中转站模型探针已启动: http://localhost:{PORT}  (Ctrl+C 退出)")
    Server(("127.0.0.1", PORT), Handler).serve_forever()
