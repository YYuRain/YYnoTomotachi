"""极简记忆浏览 + 编辑 UI + 审计日志查看（自搭记忆栈版本，2026-05-18 起）。

只面对一张 `memories` 表（schema 见 src/memory_store.py）。
- `/` 一页 HTML——只有「记忆项」和「审计」两个 tab（之前的「分类」「资源」随 memU 一起退役）。
- 读：`/api/stats` `/api/items` `/api/audit`
- 写：
  - `PATCH /api/items/{id}` body `{summary: str}` —— 改记忆文本，自动重新 embedding。
  - `DELETE /api/items/{id}` —— 删条目。

Embedding 走 `src.embed_client`（共享 :18080 调用），失败时降级为只更新文本。

启动：`.venv/bin/python -m scripts.admin`
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

from . import embed_client
from .config import settings


# Cookie session：登录走 Telegram /memory 返回的 token URL。
# 没有密码逻辑——bot 进程铸 token 是完全的"凭票入场"。
# token 用 HMAC 签名，密钥在 src/users.py 中由 bot/admin 共享（disk file `data/.webui_secret`）。
_SESSION_COOKIE = "aidemo_session"
_SESSION_TTL = 7 * 86400


def _session_from_request(request: Request) -> dict | None:
    from . import users
    return users.verify_session_token(request.cookies.get(_SESSION_COOKIE, ""))


def _get_viewer(request: Request) -> int | None:
    """返回 viewer 的 user_id；None 表示 admin（看全部，可加 ?user_id 过滤）。

    cookie 不在或失效 → 401。前端 fetch 收到 401 跳 /login（提示去 Telegram 拿链接）。
    """
    # dev 模式：admin env 都没设 + admin_chat_id 是默认值时，不鉴权
    admin_user = os.environ.get("ADMIN_UI_USER", "")
    admin_pwd = os.environ.get("ADMIN_UI_PASSWORD", "")
    if not admin_user and not admin_pwd:
        return None
    p = _session_from_request(request)
    if p is None:
        raise HTTPException(status_code=401, detail="login required")
    if p.get("a"):
        return None  # admin
    v = p.get("v")
    return int(v) if v is not None else None

log = logging.getLogger(__name__)


def _engine():
    s = settings()
    if not s.memu_db_url:
        raise RuntimeError("admin UI 需要 MEMU_DB_URL（postgres）")
    return create_engine(s.memu_db_url, future=True)


class ItemPatch(BaseModel):
    summary: str = Field(min_length=1, max_length=4000)


_LOGIN_HTML = """<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIDemo · 登录</title>
<style>
  * { box-sizing: border-box; }
  body { font: 14px/1.5 -apple-system, "PingFang SC", system-ui, sans-serif; color:#222; margin:0;
         background:#fafafa; min-height:100vh; display:flex; align-items:center; justify-content:center; padding:20px; }
  .card { background:white; padding:28px 26px; border-radius:10px; border:1px solid #e4e4e4;
          box-shadow:0 4px 16px rgba(0,0,0,.04); width:100%; max-width:380px; text-align:center; }
  h1 { margin:0 0 14px; font-size:18px; font-weight:600; }
  p { margin:0 0 12px; color:#555; line-height:1.6; }
  .cmd { display:inline-block; padding:6px 12px; background:#f0f4ff; color:#1a6cff;
         border-radius:6px; font-family:ui-monospace,Menlo,monospace; font-size:14px; margin:2px; }
  .err { color:#d9554f; font-size:13px; margin-top:14px; min-height:18px; }
  .muted { color:#999; font-size:12px; }
</style>
</head><body>
<div class="card">
  <h1>AIDemo · 登录</h1>
  <p>这个 webUI 走 Telegram 链接登录，没有密码。</p>
  <p>在 Telegram 里给你的 bot 发<br><span class="cmd">/memory</span><br>会回一条带登录链接的消息，点开就直接进。</p>
  <p class="muted">链接 10 分钟内有效；登录后浏览器保 7 天</p>
  <div class="err" id="err"></div>
</div>
<script>
const params = new URLSearchParams(location.search);
if (params.get('err') === 'expired') {
  document.getElementById('err').textContent = '上次的链接过期了——重新发 /memory 拿一条';
}
</script>
</body></html>
"""


_INDEX_HTML = """<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>AIDemo · 记忆浏览</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
  :root { --fg:#222; --muted:#777; --bd:#e4e4e4; --bg:#fafafa; --accent:#1a6cff; --danger:#d9554f; }
  * { box-sizing: border-box; }
  body { font: 14px/1.5 -apple-system, "PingFang SC", system-ui, sans-serif; color: var(--fg); margin: 0; background: var(--bg); }
  header { padding: 14px 22px; border-bottom: 1px solid var(--bd); background: white; display: flex; gap: 18px; align-items: center; }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; }
  header .stats span { color: var(--muted); font-size: 13px; }
  header .stats b { color: var(--fg); font-variant-numeric: tabular-nums; }
  nav { background: white; padding: 0 22px; border-bottom: 1px solid var(--bd); display: flex; gap: 0; }
  nav button { border: none; background: none; padding: 10px 14px; font: inherit; cursor: pointer; border-bottom: 2px solid transparent; color: var(--muted); }
  nav button.active { color: var(--accent); border-bottom-color: var(--accent); }
  main { padding: 18px 22px; }
  .toolbar { margin-bottom: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .toolbar input, .toolbar select { font: inherit; padding: 6px 10px; border: 1px solid var(--bd); border-radius: 6px; background: white; }
  .toolbar input[type=search] { min-width: 260px; }
  table { width: 100%; border-collapse: collapse; background: white; border: 1px solid var(--bd); border-radius: 6px; overflow: hidden; }
  th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--bd); vertical-align: top; font-size: 13px; }
  th { background: #f5f5f5; font-weight: 600; color: #444; }
  tr:last-child td { border-bottom: none; }
  td.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted); white-space: nowrap; }
  td.summary { max-width: 700px; }
  td.ops { text-align: right; white-space: nowrap; }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 10px; background: #eef4ff; color: var(--accent); font-size: 12px; }
  /* status 三态色（PRD v2 / 5.1） */
  .st { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; }
  .st.confirmed { background: #ecf8ee; color: #1a8a3a; }
  .st.to_verify { background: #fff3e0; color: #b56500; }
  .st.stale { background: #f0f0f0; color: #888; text-decoration: line-through; }
  .empty { color: var(--muted); text-align: center; padding: 40px; }
  .muted { color: var(--muted); }
  button.op { border: 1px solid var(--bd); background: white; padding: 3px 9px; border-radius: 4px; cursor: pointer; font: inherit; font-size: 12px; color: #444; margin-left: 4px; }
  button.op:hover { background: #f4f4f4; }
  button.op.danger:hover { background: #fdf0ef; color: var(--danger); border-color: var(--danger); }
  .modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,.3); display: none; align-items: center; justify-content: center; z-index: 10; }
  .modal-bg.on { display: flex; }
  .modal { background: white; border-radius: 10px; padding: 18px; width: min(640px, 92vw); box-shadow: 0 10px 40px rgba(0,0,0,.2); }
  .modal h2 { font-size: 14px; margin: 0 0 10px; color: #555; }
  .modal textarea { width: 100%; min-height: 120px; padding: 10px; border: 1px solid var(--bd); border-radius: 6px; font: inherit; resize: vertical; }
  .modal .row { margin-top: 10px; }
  .modal .actions { margin-top: 14px; text-align: right; }
  .modal .actions button { padding: 6px 14px; margin-left: 8px; border-radius: 6px; border: 1px solid var(--bd); background: white; cursor: pointer; font: inherit; }
  .modal .actions button.primary { background: var(--accent); color: white; border-color: var(--accent); }
  .toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #333; color: white; padding: 8px 16px; border-radius: 6px; opacity: 0; transition: opacity .2s; z-index: 20; }
  .toast.on { opacity: 1; }
  /* audit tab：事件 chip 着色，按事件类别 */
  .ev { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .ev.user_msg, .ev.assistant_reply { background: #eef4ff; color: #1a6cff; }
  .ev.memory_recall, .ev.memory_flush, .ev.memory_conflict_check, .ev.memory_reverify,
  .ev.memory_dream, .ev.memory_dream_one { background: #ecf8ee; color: #1a8a3a; }
  .ev.persona_update, .ev.persona_consolidate { background: #f5edff; color: #7a3fcc; }
  .ev.proactive_decision, .ev.proactive_fire, .ev.proactive_opener_generated { background: #fff3e0; color: #b56500; }
  .ev.tool_call { background: #f0f0f0; color: #555; }
  .ev.interest_bump { background: #fffbe5; color: #997300; }
  .ev.startup, .ev.shutdown { background: #fdecec; color: #c53b3b; }
  .audit-summary { max-width: 700px; word-break: break-word; }
  .audit-summary .k { color: var(--muted); font-size: 12px; margin-right: 4px; }
  pre.json { background: #f7f7f7; border: 1px solid var(--bd); border-radius: 6px; padding: 10px; font-size: 12px; overflow-x: auto; max-height: 60vh; margin: 0; white-space: pre-wrap; word-break: break-word; }

  /* 表格容器横向滚动——窄屏时表格不破布局，可以左右滑 */
  .tab { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .tab table { min-width: 100%; }

  /* 移动端断点：表格转卡片，thead 隐藏，每行变成一张卡 */
  @media (max-width: 720px) {
    body { font-size: 13px; }
    header { padding: 10px 12px; flex-wrap: wrap; gap: 6px 12px; }
    header h1 { font-size: 14px; flex: 0 0 100%; }
    header .stats { font-size: 12px; display: flex; gap: 10px; flex-wrap: wrap; }
    header .stats span { white-space: nowrap; }
    nav { padding: 0; overflow-x: auto; -webkit-overflow-scrolling: touch; }
    nav button { padding: 12px 14px; font-size: 13px; flex-shrink: 0; min-height: 44px; }
    main { padding: 10px 12px; }
    .toolbar { gap: 6px; }
    .toolbar input[type=search] { min-width: 0; flex: 1 1 180px; }
    .toolbar select, .toolbar input { font-size: 13px; padding: 8px 10px; }
    /* 触控更友好的按钮 */
    button.op { padding: 7px 11px; font-size: 12px; min-height: 32px; }
    .modal { padding: 14px; }
    .modal textarea { min-height: 100px; }
    .modal .actions button { padding: 8px 14px; }
    pre.json { font-size: 11px; max-height: 50vh; }

    /* 表格 → 卡片栈 */
    .tab { overflow-x: visible; }
    .tab table { display: block; background: transparent; border: none; min-width: 0; border-radius: 0; }
    .tab thead { display: none; }
    .tab tbody { display: block; }
    .tab tr {
      display: block;
      background: white;
      border: 1px solid var(--bd);
      border-radius: 8px;
      padding: 10px 12px;
      margin-bottom: 8px;
      box-shadow: 0 1px 2px rgba(0,0,0,.02);
    }
    .tab td {
      display: block;
      padding: 5px 0;
      border: none;
      white-space: normal;
      word-break: break-word;
      font-size: 13px;
      max-width: none;
    }
    .tab td.mono { color: var(--muted); font-size: 11px; white-space: normal; }
    .tab td.ops {
      text-align: left;
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px dashed var(--bd);
      white-space: normal;
    }
    .tab td.ops button.op { margin-left: 0; margin-right: 6px; }
    .tab td[data-label]::before {
      content: attr(data-label) "  ";
      display: inline-block;
      color: var(--muted);
      font-size: 11px;
      min-width: 44px;
      margin-right: 8px;
      vertical-align: top;
    }
    .tab td.summary[data-label]::before,
    .tab td.audit-summary[data-label]::before {
      display: block;
      margin-bottom: 2px;
      min-width: 0;
    }
    .tab tr td.empty { text-align: center; padding: 30px 8px; color: var(--muted); }
    .tab tr td.empty::before { display: none; }
  }

  /* 防止 iOS Safari 自动放大 input（字号 < 16px 会触发） */
  @media (max-width: 720px) {
    .toolbar input[type=search],
    .toolbar select,
    .modal textarea { font-size: 16px; }
  }
</style>
</head><body>

<header>
  <h1>AIDemo · 记忆浏览</h1>
  <div class="stats">
    <span>记忆项 <b id="s-items">—</b></span>
    <span>审计 <b id="s-audit">—</b></span>
  </div>
  <div id="user-switch" style="margin-left:auto;display:none">
    <label class="muted" style="font-size:12px">查看用户：</label>
    <select id="user-select" style="font:inherit;padding:4px 8px;border:1px solid var(--bd);border-radius:6px"></select>
  </div>
  <div id="who" style="font-size:12px;color:var(--muted);display:flex;gap:10px;align-items:center;margin-left:14px">
    <span id="who-label"></span>
    <a href="/logout" id="logout" style="color:var(--accent);text-decoration:none">退出</a>
  </div>
</header>

<nav>
  <button data-tab="items" class="active">记忆项</button>
  <button data-tab="graph">图谱</button>
  <button data-tab="audit">审计</button>
</nav>

<main>
  <div id="tab-items" class="tab">
    <div class="toolbar">
      <input type="search" id="q" placeholder="搜索记忆内容（对 summary ILIKE）" />
      <select id="type-filter">
        <option value="">全部类型</option>
        <option value="profile">profile</option>
        <option value="event">event</option>
      </select>
      <select id="status-filter">
        <option value="">全部状态</option>
        <option value="confirmed">confirmed</option>
        <option value="to_verify">to_verify</option>
        <option value="stale">stale</option>
      </select>
      <span class="muted" id="items-hint"></span>
    </div>
    <table>
      <thead><tr><th style="width:80px">类型</th><th style="width:90px">状态</th><th>内容</th><th style="width:130px" class="mono">时间</th><th style="width:140px"></th></tr></thead>
      <tbody id="items-tbody"><tr><td colspan="5" class="empty">加载中…</td></tr></tbody>
    </table>
  </div>

  <div id="tab-graph" class="tab" style="display:none">
    <div class="toolbar">
      <select id="graph-type-filter">
        <option value="">全部类型</option>
        <option value="profile">profile</option>
        <option value="event">event</option>
      </select>
      <label class="muted"><input type="checkbox" id="graph-only-deps" /> 只看有依赖关系的节点</label>
      <span class="muted" id="graph-hint">拖动节点可调整 · 滚轮缩放 · 鼠标悬停看内容</span>
    </div>
    <div id="graph-container" style="position:relative;width:100%;height:calc(100vh - 240px);background:#fff;border:1px solid var(--bd);border-radius:6px;overflow:hidden">
      <svg id="graph-svg" style="width:100%;height:100%;display:block"></svg>
      <div id="graph-tip" style="position:absolute;pointer-events:none;background:rgba(20,20,20,.92);color:#fff;padding:6px 10px;border-radius:6px;font-size:12px;line-height:1.5;max-width:320px;display:none;z-index:5"></div>
      <div id="graph-legend" style="position:absolute;left:12px;bottom:12px;background:rgba(255,255,255,.92);border:1px solid var(--bd);border-radius:6px;padding:6px 10px;font-size:11px;line-height:1.6;color:#555">
        <div><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#1a8a3a;margin-right:6px;vertical-align:middle"></span>confirmed</div>
        <div><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#b56500;margin-right:6px;vertical-align:middle"></span>to_verify</div>
        <div><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#888;margin-right:6px;vertical-align:middle"></span>stale</div>
        <div style="margin-top:4px;color:#999">边方向：B → A 表示 B 依赖于 A</div>
      </div>
    </div>
  </div>

  <div id="tab-audit" class="tab" style="display:none">
    <div class="toolbar">
      <select id="audit-filter">
        <option value="">全部事件</option>
        <option value="user_msg">user_msg</option>
        <option value="assistant_reply">assistant_reply</option>
        <option value="memory_recall">memory_recall</option>
        <option value="memory_flush">memory_flush</option>
        <option value="memory_conflict_check">memory_conflict_check</option>
        <option value="memory_reverify">memory_reverify</option>
        <option value="memory_dream">memory_dream</option>
        <option value="memory_dream_one">memory_dream_one</option>
        <option value="persona_update">persona_update</option>
        <option value="persona_consolidate">persona_consolidate</option>
        <option value="proactive_decision">proactive_decision</option>
        <option value="proactive_fire">proactive_fire</option>
        <option value="proactive_opener_generated">proactive_opener_generated</option>
        <option value="tool_call">tool_call</option>
        <option value="interest_bump">interest_bump</option>
        <option value="startup">startup</option>
        <option value="shutdown">shutdown</option>
      </select>
      <select id="audit-limit">
        <option value="100">最近 100 条</option>
        <option value="300" selected>最近 300 条</option>
        <option value="1000">最近 1000 条</option>
      </select>
      <label class="muted"><input type="checkbox" id="audit-auto" checked /> 自动刷新（5s）</label>
      <span class="muted" id="audit-hint"></span>
    </div>
    <table>
      <thead><tr><th class="mono" style="width:130px">时间</th><th style="width:160px">事件</th><th>摘要</th><th style="width:80px"></th></tr></thead>
      <tbody id="audit-tbody"><tr><td colspan="4" class="empty">加载中…</td></tr></tbody>
    </table>
  </div>
</main>

<!-- Edit modal -->
<div class="modal-bg" id="modal">
  <div class="modal">
    <h2 id="modal-title">编辑</h2>
    <div class="row">
      <label class="muted" id="modal-label">摘要</label>
      <textarea id="modal-text"></textarea>
    </div>
    <div class="actions">
      <button onclick="closeModal()">取消</button>
      <button class="primary" id="modal-save">保存</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const el = (tag, attrs = {}, text = '') => {
  const e = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
  if (text) e.textContent = text;
  return e;
};

let _isAdmin = false;
let _currentUid = '';
const withUid = (qs = '') => {
  const sep = qs ? '&' : '';
  return _currentUid ? qs + sep + 'user_id=' + encodeURIComponent(_currentUid) : qs;
};
const fmt = ts => ts ? new Date(ts).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';
const toast = (msg) => { const t = $('#toast'); t.textContent = msg; t.classList.add('on'); setTimeout(() => t.classList.remove('on'), 1600); };

let _modalCtx = null;
function openModal(title, summary) {
  $('#modal-title').textContent = title;
  $('#modal-text').value = summary || '';
  $('#modal').classList.add('on');
}
function closeModal() { $('#modal').classList.remove('on'); _modalCtx = null; }
$('#modal-save').addEventListener('click', async () => {
  if (!_modalCtx) return;
  const s = $('#modal-text').value.trim();
  await _modalCtx.onSave(s);
  closeModal();
});

async function loadStats() {
  const r = await fetch('/api/stats?' + withUid()); const j = await r.json();
  $('#s-items').textContent = j.items;
  $('#s-audit').textContent = j.audit ?? '—';
}

async function loadItems() {
  const q = encodeURIComponent($('#q').value || '');
  const t = encodeURIComponent($('#type-filter').value || '');
  const st = encodeURIComponent($('#status-filter').value || '');
  const r = await fetch(`/api/items?q=${q}&type=${t}&status=${st}&limit=200&` + withUid()); const j = await r.json();
  $('#items-hint').textContent = `${j.length} 条`;
  const tb = $('#items-tbody'); tb.innerHTML = '';
  if (!j.length) { tb.innerHTML = '<tr><td colspan="5" class="empty">没命中</td></tr>'; return; }
  for (const it of j) {
    const tr = el('tr');
    const pill = el('span', { class: 'pill' }, it.memory_type || '');
    const tdT = el('td', { 'data-label': '类型' }); tdT.appendChild(pill); tr.appendChild(tdT);
    const stPill = el('span', { class: 'st ' + (it.status || 'confirmed') }, it.status || 'confirmed');
    if (it.confidence !== undefined && it.confidence < 1) stPill.title = `confidence=${Number(it.confidence).toFixed(2)}`;
    const tdSt = el('td', { 'data-label': '状态' }); tdSt.appendChild(stPill); tr.appendChild(tdSt);
    const tdSummary = el('td', { class: 'summary', 'data-label': '内容' });
    tdSummary.appendChild(document.createTextNode(it.summary || ''));
    if (it.depends_on && it.depends_on.length) {
      const depRow = el('div', { class: 'muted', style: 'margin-top:4px;font-size:11px' });
      depRow.appendChild(document.createTextNode('依赖：'));
      it.depends_on.forEach((depId, idx) => {
        if (idx > 0) depRow.appendChild(document.createTextNode(' · '));
        const a = el('a', { href: '#', style: 'color:var(--accent);font-family:ui-monospace,Menlo,monospace;text-decoration:none' }, depId.slice(0, 8));
        a.title = '跳到这条事实';
        a.onclick = (e) => { e.preventDefault(); _focusItem(depId); };
        depRow.appendChild(a);
      });
      tdSummary.appendChild(depRow);
    }
    tr.appendChild(tdSummary);
    tr.appendChild(el('td', { class: 'mono', 'data-label': '时间' }, fmt(it.created_at)));
    const ops = el('td', { class: 'ops' });
    const bEdit = el('button', { class: 'op' }, '编辑');
    bEdit.onclick = () => {
      _modalCtx = {
        kind: 'item', id: it.id,
        onSave: async (summary) => {
          const r = await fetch(`/api/items/${encodeURIComponent(it.id)}`, {
            method: 'PATCH', headers: {'content-type': 'application/json'},
            body: JSON.stringify({ summary }),
          });
          if (r.ok) { const rj = await r.json(); toast(rj.embedded ? '已保存 + 已重算向量' : '已保存（向量未更新）'); loadItems(); }
          else toast('失败：' + r.status);
        },
      };
      openModal(`编辑记忆项（${it.memory_type}）`, it.summary || '');
    };
    const bDel = el('button', { class: 'op danger' }, '删除');
    bDel.onclick = async () => {
      if (!confirm('删除这条记忆？')) return;
      const r = await fetch(`/api/items/${encodeURIComponent(it.id)}`, { method: 'DELETE' });
      if (r.ok) { toast('已删除'); loadItems(); loadStats(); }
      else toast('失败：' + r.status);
    };
    ops.appendChild(bEdit); ops.appendChild(bDel);
    tr.appendChild(ops);
    tb.appendChild(tr);
  }
}

document.querySelectorAll('nav button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  const tab = b.dataset.tab;
  document.querySelectorAll('.tab').forEach(t => t.style.display = 'none');
  $('#tab-' + tab).style.display = '';
  if (tab === 'items') loadItems();
  if (tab === 'graph') loadGraph();
  if (tab === 'audit') loadAudit();
  toggleAuditAutoRefresh(tab === 'audit' && $('#audit-auto').checked);
}));

// items 内点 deps id：切回 items tab，搜框塞 short id 过滤定位
function _focusItem(fullId) {
  const itemsBtn = document.querySelector('nav button[data-tab="items"]');
  if (itemsBtn && !itemsBtn.classList.contains('active')) itemsBtn.click();
  const short = (fullId || '').slice(0, 8);
  $('#q').value = short;
  loadItems();
}

// ============ graph tab（D3 force-directed） ============
let _graphSim = null;

const GRAPH_COLOR = {
  confirmed: '#1a8a3a',
  to_verify: '#b56500',
  stale: '#888',
};

async function loadGraph() {
  if (typeof d3 === 'undefined') {
    $('#graph-hint').textContent = 'D3 库加载失败（需要联网拉 CDN）';
    return;
  }
  const t = encodeURIComponent($('#graph-type-filter').value || '');
  const r = await fetch(`/api/items?q=&type=${t}&limit=1000&` + withUid());
  const items = await r.json();

  const idSet = new Set(items.map(it => it.id));
  let nodes = items.map(it => ({
    id: it.id,
    summary: it.summary || '',
    type: it.memory_type || 'profile',
    status: it.status || 'confirmed',
    confidence: it.confidence,
    deps: it.depends_on || [],
  }));
  const links = [];
  for (const n of nodes) {
    for (const dep of (n.deps || [])) {
      // 边方向 source=B(依赖者) → target=A(被依赖)
      if (idSet.has(dep)) links.push({ source: n.id, target: dep });
    }
  }

  if ($('#graph-only-deps').checked) {
    const inEdges = new Set();
    links.forEach(l => { inEdges.add(l.source); inEdges.add(l.target); });
    nodes = nodes.filter(n => inEdges.has(n.id));
  }

  $('#graph-hint').textContent = `${nodes.length} 节点 · ${links.length} 条依赖 · 拖动 / 滚轮 / 悬停`;
  _renderGraph(nodes, links);
}

function _renderGraph(nodes, links) {
  const svgEl = document.getElementById('graph-svg');
  const tip = document.getElementById('graph-tip');
  const w = svgEl.clientWidth, h = svgEl.clientHeight;
  const svg = d3.select(svgEl);
  svg.selectAll('*').remove();
  if (_graphSim) { _graphSim.stop(); _graphSim = null; }
  if (!nodes.length) {
    svg.append('text').attr('x', w/2).attr('y', h/2)
       .attr('text-anchor', 'middle').attr('fill', '#aaa').attr('font-size', 14)
       .text('没有节点（切换用户或调整过滤）');
    return;
  }

  // arrow marker（指向被依赖方）
  const defs = svg.append('defs');
  defs.append('marker')
      .attr('id', 'arrow').attr('viewBox', '0 -5 10 10').attr('refX', 14).attr('refY', 0)
      .attr('markerWidth', 6).attr('markerHeight', 6).attr('orient', 'auto')
    .append('path').attr('d', 'M0,-5L10,0L0,5').attr('fill', '#bbb');

  const root = svg.append('g');
  svg.call(d3.zoom().scaleExtent([0.2, 4]).on('zoom', (e) => root.attr('transform', e.transform)));

  const link = root.append('g').attr('stroke', '#bbb').attr('stroke-opacity', 0.6).attr('stroke-width', 1.2)
    .selectAll('line').data(links).join('line').attr('marker-end', 'url(#arrow)');

  const nodeG = root.append('g').selectAll('g').data(nodes).join('g')
    .style('cursor', 'grab')
    .call(d3.drag()
      .on('start', (ev, d) => { if (!ev.active) _graphSim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
      .on('end', (ev, d) => { if (!ev.active) _graphSim.alphaTarget(0); d.fx = null; d.fy = null; })
    );

  nodeG.append('circle')
    .attr('r', d => d.type === 'profile' ? 8 : 6)
    .attr('fill', d => GRAPH_COLOR[d.status] || '#aaa')
    .attr('stroke', '#fff').attr('stroke-width', 1.5)
    .on('mouseover', (ev, d) => {
      tip.style.display = 'block';
      tip.innerHTML =
        `<div style="font-weight:600;margin-bottom:2px">${d.type} · <span style="color:${GRAPH_COLOR[d.status]}">${d.status}</span>${d.confidence < 1 ? ` · c=${Number(d.confidence).toFixed(2)}` : ''}</div>` +
        `<div>${escapeHtml(d.summary)}</div>` +
        (d.deps && d.deps.length ? `<div style="margin-top:4px;color:#bbb;font-size:11px">依赖 ${d.deps.length} 条</div>` : '');
    })
    .on('mousemove', (ev) => {
      const rect = document.getElementById('graph-container').getBoundingClientRect();
      tip.style.left = (ev.clientX - rect.left + 14) + 'px';
      tip.style.top = (ev.clientY - rect.top + 14) + 'px';
    })
    .on('mouseout', () => { tip.style.display = 'none'; })
    .on('dblclick', (ev, d) => _focusItem(d.id));

  nodeG.append('text')
    .attr('dx', 11).attr('dy', 4).attr('font-size', 10).attr('fill', '#444')
    .text(d => d.summary.length > 18 ? d.summary.slice(0, 17) + '…' : d.summary);

  _graphSim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(80).strength(0.7))
    .force('charge', d3.forceManyBody().strength(-220))
    .force('center', d3.forceCenter(w/2, h/2))
    .force('collide', d3.forceCollide().radius(28))
    .on('tick', () => {
      link
        .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
        .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
      nodeG.attr('transform', d => `translate(${d.x},${d.y})`);
    });
}

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s || '';
  return div.innerHTML;
}

$('#graph-type-filter').addEventListener('change', loadGraph);
$('#graph-only-deps').addEventListener('change', loadGraph);

let _deb;
$('#q').addEventListener('input', () => { clearTimeout(_deb); _deb = setTimeout(loadItems, 250); });
$('#type-filter').addEventListener('change', loadItems);
$('#status-filter').addEventListener('change', loadItems);

// ============ audit tab ============
const truncate = (s, n) => { s = String(s || ''); return s.length > n ? s.slice(0, n) + '…' : s; };
const auditTimeFmt = ts => {
  if (!ts) return '';
  const d = new Date(ts);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const pad = n => String(n).padStart(2, '0');
  if (sameDay) return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

function summarizeAudit(d) {
  const t = (s, n) => truncate(s, n);
  switch (d.event) {
    case 'startup': return `provider=${d.provider} · model=${d.model}`;
    case 'shutdown': return '';
    case 'user_msg': return (d.has_image ? '🖼️ ' : '') + t(d.text, 100);
    case 'assistant_reply': {
      const lat = d.latency_ms ? ` ${d.latency_ms}ms` : '';
      const err = d.error ? ` ⚠️ ${t(d.error, 50)}` : '';
      const meta = `<span class="k">${d.mode || '?'}${lat}${err}</span>`;
      return meta + t(d.text || '(空)', 100);
    }
    case 'memory_recall':
      if (!d.hits) return `<span class="k">0 hits</span>${t(d.query, 60)}`;
      return `<span class="k">${d.hits} hits</span>${(d.snippets||[]).slice(0,2).map(s=>'「'+t(s,40)+'」').join(' · ')}`;
    case 'memory_flush':
      return `<span class="k">${d.msgs} 消息 → +${d.new_items||0} 项</span>` +
             ((d.new_item_summaries||[]).slice(0,2).map(s=>'「'+t(s,40)+'」').join(' · '));
    case 'memory_conflict_check': {
      const flips = d.flips || [];
      if (!flips.length) return `<span class="k">${d.candidates} 候选 · 无变更</span>「${t(d.new_summary, 50)}」`;
      const flipStr = flips.slice(0,3).map(f => `${(f.id||'').slice(0,6)}→${f.verdict}`).join(' ');
      return `<span class="k">+1「${t(d.new_summary, 40)}」</span>触发 ${flips.length} 改: ${flipStr}`;
    }
    case 'memory_reverify': {
      const lat = d.latency_ms ? ` ${d.latency_ms}ms` : '';
      return `<span class="k">${d.verdict || '?'}${lat}</span>「${t(d.fact, 60)}」`;
    }
    case 'memory_dream': {
      const lat = d.latency_ms ? ` ${d.latency_ms}ms` : '';
      return `<span class="k">扫 ${d.reviewed}${lat}</span>` +
             `+${d.to_confirmed||0} confirmed · ` +
             `+${d.to_stale||0} stale · ` +
             `${d.uncertain||0} 维持 · ` +
             `${d.errors||0} 错`;
    }
    case 'memory_dream_one': {
      const lat = d.latency_ms ? ` ${d.latency_ms}ms` : '';
      return `<span class="k">${d.verdict || '?'}${lat}</span>「${t(d.fact, 60)}」`;
    }
    case 'persona_update': {
      const deltas = Object.entries(d.trait_deltas || {}).map(([k,v])=>`${k}${v>0?'+':''}${v}`).join(' ');
      const obs = (d.new_observations || []).length;
      const ms = (d.new_milestones || []).length;
      const parts = [];
      if (deltas) parts.push(`<span class="k">Δ</span>${deltas}`);
      if (d.mood) parts.push(`<span class="k">mood</span>${d.mood}`);
      if (obs) parts.push(`+${obs} 观察`);
      if (ms) parts.push(`+${ms} 锚点`);
      return parts.join(' · ') || '无变化';
    }
    case 'persona_consolidate':
      return `保留 ${d.observations_kept} 观察 · 丢 ${d.observations_dropped}`;
    case 'proactive_decision':
      return `${d.should ? '✓ GO' : '× skip'} · ${t(d.why || '', 80)}`;
    case 'proactive_fire':
      return `「${t(d.opener_text, 80)}」`;
    case 'proactive_opener_generated':
      return `<span class="k">idle ${d.idle_sec}s</span>「${t(d.text, 60)}」`;
    case 'tool_call':
      return `<span class="k">${d.tool}</span>"${t(d.query, 30)}" → ${d.result_chars} chars`;
    case 'interest_bump':
      return (d.topics || []).map(tp => {
        const h = (d.heat_after || {})[tp];
        return tp + (h !== undefined ? `(${Number(h).toFixed(1)})` : '');
      }).join(' · ');
    default:
      return t(JSON.stringify(d), 100);
  }
}

async function loadAudit() {
  const ev = $('#audit-filter').value;
  const lim = $('#audit-limit').value;
  const params = new URLSearchParams();
  if (ev) params.set('event', ev);
  if (lim) params.set('limit', lim);
  if (_currentUid) params.set('user_id', _currentUid);
  let j;
  try {
    j = await fetch('/api/audit?' + params).then(r => r.json());
  } catch (e) {
    return;
  }
  const tb = $('#audit-tbody'); tb.innerHTML = '';
  if (!j.length) { tb.innerHTML = '<tr><td colspan="4" class="empty">空</td></tr>'; return; }
  $('#audit-hint').textContent = `${j.length} 条`;
  for (const d of j) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="mono" data-label="时间">${auditTimeFmt(d.ts)}</td>
      <td data-label="事件"><span class="ev ${d.event}">${d.event}</span></td>
      <td class="audit-summary" data-label="摘要">${summarizeAudit(d)}</td>
      <td class="ops"><button class="op">详情</button></td>
    `;
    tr.querySelector('button').addEventListener('click', () => {
      $('#modal-title').textContent = `${d.event} · ${auditTimeFmt(d.ts)}`;
      $('#modal-text').style.display = 'none';
      $('#modal-save').style.display = 'none';
      let detail = $('#modal-detail');
      if (!detail) {
        detail = document.createElement('pre');
        detail.id = 'modal-detail';
        detail.className = 'json';
        $('#modal-text').parentElement.appendChild(detail);
      }
      detail.style.display = '';
      detail.textContent = JSON.stringify(d, null, 2);
      $('#modal').classList.add('on');
    });
    tb.appendChild(tr);
  }
}

let _auditTimer = null;
function toggleAuditAutoRefresh(on) {
  if (_auditTimer) { clearInterval(_auditTimer); _auditTimer = null; }
  if (on) _auditTimer = setInterval(loadAudit, 5000);
}
$('#audit-filter').addEventListener('change', loadAudit);
$('#audit-limit').addEventListener('change', loadAudit);
$('#audit-auto').addEventListener('change', e => toggleAuditAutoRefresh(e.target.checked));

const _origCloseModal = closeModal;
closeModal = () => {
  _origCloseModal();
  $('#modal-text').style.display = '';
  $('#modal-save').style.display = '';
  const detail = document.getElementById('modal-detail');
  if (detail) detail.style.display = 'none';
};

document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

async function initViewer() {
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 8000);
    const r = await fetch('/api/me', { signal: ctl.signal, credentials: 'same-origin' });
    clearTimeout(t);
    if (r.status === 401) { location.href = '/login'; return; }
    const me = await r.json();
    _isAdmin = !!me.is_admin;
    $('#who-label').textContent = _isAdmin ? '身份：管理员' : `身份：user_id=${me.user_id}`;
    if (!_isAdmin) {
      _currentUid = String(me.user_id || '');
      return;
    }
    const us = await fetch('/api/users').then(r => r.json());
    const sel = $('#user-select');
    const optAll = el('option', { value: '' }, '全部用户');
    sel.appendChild(optAll);
    for (const u of us) {
      const label = String(u.chat_id) + (u.note ? ` (${u.note})` : '') + (u.status === 'test' ? ' [test]' : '');
      const o = el('option', { value: String(u.chat_id) }, label);
      sel.appendChild(o);
    }
    $('#user-switch').style.display = '';
    sel.addEventListener('change', () => {
      _currentUid = sel.value;
      const cur = document.querySelector('nav button.active')?.dataset.tab;
      loadStats();
      if (cur === 'items') loadItems();
      else if (cur === 'graph') loadGraph();
      else if (cur === 'audit') loadAudit();
    });
  } catch (e) {
    console.warn('initViewer err', e);
    document.body.innerHTML = '<div style="padding:40px;text-align:center;color:#888;font-size:14px">'
      + '加载超时。可能是 cookie 没存住——回 Telegram 重发 /memory 拿新链接，'
      + '或长按链接选 "在浏览器中打开"。'
      + '<br><br><a href="/login" style="color:#1a6cff">回登录页</a>'
      + '</div>';
  }
}

initViewer().then(() => { loadStats(); loadItems(); });
</script>

</body></html>
"""


def _resolve_uid(viewer: int | None, q_uid: int | None) -> int | None:
    """决定查询的 user_id 过滤值（新 schema 是 BIGINT）。
    - viewer is None（admin）：用 q_uid；q_uid 也 None → None（不过滤，看全部）
    - 普通用户：忽略 q_uid，强制用自己的 viewer_id（防越权）
    """
    if viewer is None:
        return q_uid
    return viewer


def build_app() -> FastAPI:
    app = FastAPI(title="AIDemo Memory Admin")
    eng = _engine()

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        admin_user = os.environ.get("ADMIN_UI_USER", "")
        admin_pwd = os.environ.get("ADMIN_UI_PASSWORD", "")
        if admin_user or admin_pwd:
            if _session_from_request(request) is None:
                return RedirectResponse("/login", status_code=302)
        return HTMLResponse(_INDEX_HTML)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page() -> HTMLResponse:
        return HTMLResponse(_LOGIN_HTML)

    @app.get("/login-by-token")
    async def login_by_token(t: str = Query(..., max_length=2048)) -> HTMLResponse:
        from . import users as _users
        payload = _users.verify_session_token(t)
        if payload is None:
            return HTMLResponse(
                '<meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/login?err=expired">',
                status_code=200,
            )
        cookie_token = _users.make_session_token(
            payload.get("v"),
            bool(payload.get("a")),
            ttl=_SESSION_TTL,
        )
        body = (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="refresh" content="0;url=/">'
            '<title>登录中…</title>'
            '<style>body{font:14px -apple-system,sans-serif;color:#888;margin:40px;text-align:center}</style>'
            '</head><body>登录中…<script>location.replace("/");</script></body></html>'
        )
        resp = HTMLResponse(body, status_code=200)
        resp.set_cookie(
            _SESSION_COOKIE, cookie_token,
            max_age=_SESSION_TTL, httponly=True, samesite="lax",
            secure=False, path="/",
        )
        return resp

    @app.get("/logout")
    async def logout() -> RedirectResponse:
        resp = RedirectResponse("/login", status_code=302)
        resp.delete_cookie(_SESSION_COOKIE)
        return resp

    @app.get("/api/me")
    async def whoami(viewer: int | None = Depends(_get_viewer)) -> dict[str, Any]:
        return {"is_admin": viewer is None, "user_id": viewer}

    @app.get("/api/users")
    async def list_users(viewer: int | None = Depends(_get_viewer)) -> list[dict[str, Any]]:
        from . import users as _users
        if viewer is not None:
            return [{"chat_id": viewer, "status": "active", "note": "you"}]
        return _users.list_users_with_meta()

    @app.get("/api/stats")
    async def stats(
        viewer: int | None = Depends(_get_viewer),
        user_id: int | None = Query(None),
    ) -> dict[str, int]:
        uid = _resolve_uid(viewer, user_id)
        with eng.connect() as c:
            if uid is None:
                items = int(c.execute(text("SELECT count(*) FROM memories")).scalar() or 0)
            else:
                items = int(c.execute(
                    text("SELECT count(*) FROM memories WHERE user_id=:u"),
                    {"u": uid},
                ).scalar() or 0)
        # audit 计数：按 user_id 过滤的行
        audit_count = 0
        path = settings().root / "data" / "audit.jsonl"
        if path.exists():
            try:
                if uid is None:
                    with path.open("rb") as f:
                        audit_count = sum(1 for _ in f)
                else:
                    uid_str = str(uid)
                    with path.open("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                d = json.loads(line)
                            except Exception:
                                continue
                            if str(d.get("user_id", "")) == uid_str:
                                audit_count += 1
            except Exception:
                pass
        return {"items": items, "audit": audit_count}

    @app.get("/api/audit")
    async def audit_list(
        viewer: int | None = Depends(_get_viewer),
        user_id: int | None = Query(None),
        limit: int = Query(300, ge=1, le=5000),
        event: str = Query("", max_length=64),
    ) -> list[dict[str, Any]]:
        uid = _resolve_uid(viewer, user_id)
        path = settings().root / "data" / "audit.jsonl"
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            log.warning("audit read err: %s", e)
            return []
        uid_str = str(uid) if uid is not None else None
        out: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if event and d.get("event") != event:
                continue
            if uid_str is not None and str(d.get("user_id", "")) != uid_str:
                continue
            out.append(d)
        out.reverse()
        return out[:limit]

    @app.get("/api/items")
    async def items_list(
        viewer: int | None = Depends(_get_viewer),
        user_id: int | None = Query(None),
        q: str = Query("", max_length=200),
        type: str = Query("", max_length=32),
        status: str = Query("", max_length=16),
        limit: int = Query(200, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        uid = _resolve_uid(viewer, user_id)
        where: list[str] = []
        params: dict[str, Any] = {"limit": limit}
        if uid is not None:
            where.append("user_id = :u"); params["u"] = uid
        if q.strip():
            # 既匹配 summary 文本，也匹配 id 前缀（让 deps 链接能跳定位）
            where.append("(summary ILIKE :q OR id::text ILIKE :qp)")
            params["q"] = f"%{q.strip()}%"
            params["qp"] = f"{q.strip()}%"
        if type.strip():
            where.append("memory_type = :t"); params["t"] = type.strip()
        if status.strip():
            where.append("status = :st"); params["st"] = status.strip()
        wh = f"WHERE {' AND '.join(where)}" if where else ""
        sql = text(
            f"SELECT id, memory_type, summary, status, confidence, "
            f"last_verified_at, depends_on, created_at, updated_at FROM memories {wh} "
            f"ORDER BY created_at DESC LIMIT :limit"
        )
        with eng.connect() as c:
            rows = c.execute(sql, params).mappings().all()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            # depends_on 是 UUID[]，FastAPI 不会自动 stringify UUID 元素
            deps = d.get("depends_on")
            if deps:
                d["depends_on"] = [str(x) for x in deps]
            out.append(d)
        return out

    # ============ 编辑接口（普通用户只能改自己的；admin 不限）============

    def _check_owns(viewer: int | None, item_id: str) -> None:
        """非 admin 必须确认要改的行属于自己。"""
        if viewer is None:
            return
        with eng.connect() as c:
            owner = c.execute(
                text("SELECT user_id FROM memories WHERE id = CAST(:id AS uuid)"),
                {"id": item_id},
            ).scalar()
        if owner is None:
            raise HTTPException(404, "not found")
        if int(owner) != viewer:
            raise HTTPException(403, "not yours")

    @app.patch("/api/items/{item_id}")
    async def patch_item(
        item_id: str, body: ItemPatch,
        viewer: int | None = Depends(_get_viewer),
    ) -> dict[str, Any]:
        _check_owns(viewer, item_id)
        new_summary = body.summary.strip()
        vec = await embed_client.embed_one(new_summary)
        params: dict[str, Any] = {"id": item_id, "summary": new_summary}
        if vec is not None:
            sql = text(
                "UPDATE memories SET summary = :summary, embedding = CAST(:vec AS vector), "
                "updated_at = NOW() WHERE id = CAST(:id AS uuid)"
            )
            params["vec"] = embed_client.vec_literal(vec)
        else:
            sql = text(
                "UPDATE memories SET summary = :summary, updated_at = NOW() "
                "WHERE id = CAST(:id AS uuid)"
            )
        with eng.begin() as c:
            r = c.execute(sql, params)
            if r.rowcount == 0:
                raise HTTPException(404, "not found")
        return {"ok": True, "embedded": vec is not None}

    @app.delete("/api/items/{item_id}")
    async def delete_item(
        item_id: str,
        viewer: int | None = Depends(_get_viewer),
    ) -> dict[str, Any]:
        _check_owns(viewer, item_id)
        with eng.begin() as c:
            r = c.execute(
                text("DELETE FROM memories WHERE id = CAST(:id AS uuid)"),
                {"id": item_id},
            )
            if r.rowcount == 0:
                raise HTTPException(404, "not found")
        return {"ok": True}

    return app
