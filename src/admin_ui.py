"""极简记忆浏览 + 编辑 UI + 审计日志查看。

独立跑，不走 memU SDK，直接读写 postgres。
- `/` 一页 HTML。
- 读：`/api/stats` `/api/categories` `/api/items` `/api/resources` `/api/audit`。
- 写：
  - `PATCH /api/items/{id}` body `{summary: str}` —— 改记忆文本，自动重新 embedding。
  - `DELETE /api/items/{id}` —— 删条目（级联删 category_items 关联）。
  - `PATCH /api/categories/{id}` body `{summary?, description?}` —— 改分类摘要，重新 embedding。
  - `DELETE /api/categories/{id}` —— 删分类及其 items 关联。

embedding 改写通过 HTTP 调 `http://127.0.0.1:18080/v1/embeddings`（即 bot 启动时的 embed_server）；
若 embed server 不在，PATCH 会跳过 embedding 更新并在响应里警告，
检索质量会轻微下滑直到下次该条被 memU 重算。

审计日志读 `data/audit.jsonl`（bot 进程 audit_log 模块产出），只读不改。

启动：`.venv/bin/python -m scripts.admin`
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

from .config import settings

log = logging.getLogger(__name__)


def _engine():
    s = settings()
    if s.memu_metadata_provider != "postgres" or not s.memu_db_url:
        raise RuntimeError("admin UI 需要 MEMU_METADATA_PROVIDER=postgres 和 MEMU_DB_URL")
    return create_engine(s.memu_db_url, future=True)


def _embed_url() -> str:
    s = settings()
    return f"http://{s.embed_server_host}:{s.embed_server_port}/v1/embeddings"


async def _embed_one(text_: str) -> list[float] | None:
    """调本地 embed server 给一段文本生成向量；失败返 None。"""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as c:
            r = await c.post(
                _embed_url(),
                json={"model": "any", "input": [text_]},
            )
            r.raise_for_status()
            data = r.json()
            return data["data"][0]["embedding"]
    except Exception as e:
        log.warning("embed server 不可达，跳过 embedding：%s", e)
        return None


def _vec_literal(vec: list[float]) -> str:
    """pgvector 接受 '[0.1, 0.2, ...]' 字面量。"""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


class ItemPatch(BaseModel):
    summary: str = Field(min_length=1, max_length=4000)


class CategoryPatch(BaseModel):
    summary: str | None = None
    description: str | None = None


_INDEX_HTML = """<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8">
<title>AIDemo · 记忆浏览</title>
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
  .ev.memory_recall, .ev.memory_flush { background: #ecf8ee; color: #1a8a3a; }
  .ev.persona_update, .ev.persona_consolidate { background: #f5edff; color: #7a3fcc; }
  .ev.proactive_decision, .ev.proactive_fire, .ev.proactive_opener_generated { background: #fff3e0; color: #b56500; }
  .ev.tool_call { background: #f0f0f0; color: #555; }
  .ev.interest_bump { background: #fffbe5; color: #997300; }
  .ev.startup, .ev.shutdown { background: #fdecec; color: #c53b3b; }
  .audit-summary { max-width: 700px; word-break: break-word; }
  .audit-summary .k { color: var(--muted); font-size: 12px; margin-right: 4px; }
  pre.json { background: #f7f7f7; border: 1px solid var(--bd); border-radius: 6px; padding: 10px; font-size: 12px; overflow-x: auto; max-height: 60vh; margin: 0; white-space: pre-wrap; word-break: break-word; }
</style>
</head><body>

<header>
  <h1>AIDemo · 记忆浏览</h1>
  <div class="stats">
    <span>分类 <b id="s-cats">—</b></span>
    <span>记忆项 <b id="s-items">—</b></span>
    <span>资源 <b id="s-res">—</b></span>
    <span>审计 <b id="s-audit">—</b></span>
  </div>
</header>

<nav>
  <button data-tab="cats" class="active">分类</button>
  <button data-tab="items">记忆项</button>
  <button data-tab="res">资源</button>
  <button data-tab="audit">审计</button>
</nav>

<main>
  <div id="tab-cats" class="tab">
    <table>
      <thead><tr><th style="width:140px">名字</th><th style="width:80px">条目</th><th>摘要</th><th style="width:140px" class="mono">更新</th><th style="width:140px"></th></tr></thead>
      <tbody id="cats-tbody"><tr><td colspan="5" class="empty">加载中…</td></tr></tbody>
    </table>
  </div>

  <div id="tab-items" class="tab" style="display:none">
    <div class="toolbar">
      <input type="search" id="q" placeholder="搜索记忆内容（对 summary ILIKE）" />
      <select id="type-filter">
        <option value="">全部类型</option>
        <option value="profile">profile</option>
        <option value="event">event</option>
      </select>
      <span class="muted" id="items-hint"></span>
    </div>
    <table>
      <thead><tr><th style="width:80px">类型</th><th>内容</th><th style="width:130px" class="mono">时间</th><th style="width:140px"></th></tr></thead>
      <tbody id="items-tbody"><tr><td colspan="4" class="empty">加载中…</td></tr></tbody>
    </table>
  </div>

  <div id="tab-res" class="tab" style="display:none">
    <table>
      <thead><tr><th style="width:100px">模态</th><th>URL</th><th class="mono" style="width:140px">创建</th></tr></thead>
      <tbody id="res-tbody"><tr><td colspan="3" class="empty">加载中…</td></tr></tbody>
    </table>
  </div>

  <div id="tab-audit" class="tab" style="display:none">
    <div class="toolbar">
      <select id="audit-filter">
        <option value="">全部事件</option>
        <option value="user_msg">user_msg</option>
        <option value="assistant_reply">assistant_reply</option>
        <option value="memory_recall">memory_recall</option>
        <option value="memory_flush">memory_flush</option>
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
    <div class="row" id="modal-desc-row" style="display:none">
      <label class="muted">描述</label>
      <textarea id="modal-desc" style="min-height:60px"></textarea>
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
const fmt = ts => ts ? new Date(ts).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';
const toast = (msg) => { const t = $('#toast'); t.textContent = msg; t.classList.add('on'); setTimeout(() => t.classList.remove('on'), 1600); };

let _modalCtx = null; // { kind: 'item'|'cat', id, onSave(summary, description?) }
function openModal(title, summary, opts = {}) {
  $('#modal-title').textContent = title;
  $('#modal-text').value = summary || '';
  $('#modal-desc-row').style.display = opts.withDesc ? '' : 'none';
  $('#modal-desc').value = opts.description || '';
  $('#modal').classList.add('on');
}
function closeModal() { $('#modal').classList.remove('on'); _modalCtx = null; }
$('#modal-save').addEventListener('click', async () => {
  if (!_modalCtx) return;
  const s = $('#modal-text').value.trim();
  const d = $('#modal-desc').value.trim();
  await _modalCtx.onSave(s, d);
  closeModal();
});

async function loadStats() {
  const r = await fetch('/api/stats'); const j = await r.json();
  $('#s-cats').textContent = j.categories;
  $('#s-items').textContent = j.items;
  $('#s-res').textContent = j.resources;
  $('#s-audit').textContent = j.audit ?? '—';
}

async function loadCats() {
  const r = await fetch('/api/categories'); const j = await r.json();
  const tb = $('#cats-tbody'); tb.innerHTML = '';
  if (!j.length) { tb.innerHTML = '<tr><td colspan="5" class="empty">空</td></tr>'; return; }
  for (const c of j) {
    const tr = el('tr');
    tr.appendChild(el('td', {}, c.name || '（未命名）'));
    tr.appendChild(el('td', {}, String(c.item_count || 0)));
    const td = el('td', { class: 'summary' });
    td.textContent = (c.summary || c.description || '').slice(0, 400);
    tr.appendChild(td);
    tr.appendChild(el('td', { class: 'mono' }, fmt(c.updated_at)));
    const ops = el('td', { class: 'ops' });
    const bEdit = el('button', { class: 'op' }, '编辑');
    bEdit.onclick = () => {
      _modalCtx = {
        kind: 'cat', id: c.id,
        onSave: async (summary, description) => {
          const r = await fetch(`/api/categories/${encodeURIComponent(c.id)}`, {
            method: 'PATCH', headers: {'content-type': 'application/json'},
            body: JSON.stringify({ summary, description }),
          });
          if (r.ok) { toast('已保存'); loadCats(); }
          else { toast('失败：' + r.status); }
        },
      };
      openModal(`编辑分类：${c.name}`, c.summary || '', { withDesc: true, description: c.description || '' });
    };
    const bDel = el('button', { class: 'op danger' }, '删除');
    bDel.onclick = async () => {
      if (!confirm(`删除分类「${c.name}」？关联的 items 记录不会删，但会失去与该分类的关联。`)) return;
      const r = await fetch(`/api/categories/${encodeURIComponent(c.id)}`, { method: 'DELETE' });
      if (r.ok) { toast('已删除'); loadCats(); loadStats(); }
      else toast('失败：' + r.status);
    };
    ops.appendChild(bEdit); ops.appendChild(bDel);
    tr.appendChild(ops);
    tb.appendChild(tr);
  }
}

async function loadItems() {
  const q = encodeURIComponent($('#q').value || '');
  const t = encodeURIComponent($('#type-filter').value || '');
  const r = await fetch(`/api/items?q=${q}&type=${t}&limit=200`); const j = await r.json();
  $('#items-hint').textContent = `${j.length} 条`;
  const tb = $('#items-tbody'); tb.innerHTML = '';
  if (!j.length) { tb.innerHTML = '<tr><td colspan="4" class="empty">没命中</td></tr>'; return; }
  for (const it of j) {
    const tr = el('tr');
    const pill = el('span', { class: 'pill' }, it.memory_type || '');
    const tdT = el('td'); tdT.appendChild(pill); tr.appendChild(tdT);
    tr.appendChild(el('td', { class: 'summary' }, it.summary || ''));
    tr.appendChild(el('td', { class: 'mono' }, fmt(it.created_at)));
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

async function loadResources() {
  const r = await fetch('/api/resources?limit=200'); const j = await r.json();
  const tb = $('#res-tbody'); tb.innerHTML = '';
  if (!j.length) { tb.innerHTML = '<tr><td colspan="3" class="empty">空</td></tr>'; return; }
  for (const x of j) {
    const tr = el('tr');
    tr.appendChild(el('td', {}, x.modality || ''));
    tr.appendChild(el('td', { class: 'summary' }, x.url || ''));
    tr.appendChild(el('td', { class: 'mono' }, fmt(x.created_at)));
    tb.appendChild(tr);
  }
}

document.querySelectorAll('nav button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  const tab = b.dataset.tab;
  document.querySelectorAll('.tab').forEach(t => t.style.display = 'none');
  $('#tab-' + tab).style.display = '';
  if (tab === 'cats') loadCats();
  if (tab === 'items') loadItems();
  if (tab === 'res') loadResources();
  if (tab === 'audit') loadAudit();
  toggleAuditAutoRefresh(tab === 'audit' && $('#audit-auto').checked);
}));

let _deb;
$('#q').addEventListener('input', () => { clearTimeout(_deb); _deb = setTimeout(loadItems, 250); });
$('#type-filter').addEventListener('change', loadItems);

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
      <td class="mono">${auditTimeFmt(d.ts)}</td>
      <td><span class="ev ${d.event}">${d.event}</span></td>
      <td class="audit-summary">${summarizeAudit(d)}</td>
      <td class="ops"><button class="op">详情</button></td>
    `;
    tr.querySelector('button').addEventListener('click', () => {
      $('#modal-title').textContent = `${d.event} · ${auditTimeFmt(d.ts)}`;
      $('#modal-text').style.display = 'none';
      $('#modal-desc-row').style.display = 'none';
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

// 关 modal 时还原 modal-text 的显示，避免下次编辑变 audit-detail 模式
const _origCloseModal = closeModal;
closeModal = () => {
  _origCloseModal();
  $('#modal-text').style.display = '';
  $('#modal-save').style.display = '';
  const detail = document.getElementById('modal-detail');
  if (detail) detail.style.display = 'none';
};

document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

loadStats();
loadCats();
</script>

</body></html>
"""


def build_app() -> FastAPI:
    app = FastAPI(title="AIDemo Memory Admin")
    eng = _engine()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_INDEX_HTML)

    @app.get("/api/stats")
    async def stats() -> dict[str, int]:
        with eng.connect() as c:
            mem_stats = {
                "categories": int(c.execute(text("SELECT count(*) FROM memory_categories")).scalar() or 0),
                "items": int(c.execute(text("SELECT count(*) FROM memory_items")).scalar() or 0),
                "resources": int(c.execute(text("SELECT count(*) FROM resources")).scalar() or 0),
            }
        audit_count = 0
        path = settings().root / "data" / "audit.jsonl"
        if path.exists():
            try:
                with path.open("rb") as f:
                    audit_count = sum(1 for _ in f)
            except Exception:
                pass
        return {**mem_stats, "audit": audit_count}

    @app.get("/api/audit")
    async def audit_list(
        limit: int = Query(300, ge=1, le=5000),
        event: str = Query("", max_length=64),
    ) -> list[dict[str, Any]]:
        path = settings().root / "data" / "audit.jsonl"
        if not path.exists():
            return []
        # 简单实现：全文 readlines（一天约 50KB-500KB，无需 mmap 倒读）
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            log.warning("audit read err: %s", e)
            return []
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
            out.append(d)
        # 倒序，取最近 limit 条
        out.reverse()
        return out[:limit]

    @app.get("/api/categories")
    async def categories() -> list[dict[str, Any]]:
        sql = text("""
            SELECT c.id, c.name, c.description, c.summary, c.updated_at,
              (SELECT count(*) FROM category_items ci WHERE ci.category_id = c.id) AS item_count
            FROM memory_categories c
            ORDER BY c.updated_at DESC NULLS LAST
        """)
        with eng.connect() as c:
            rows = c.execute(sql).mappings().all()
        return [dict(r) for r in rows]

    @app.get("/api/items")
    async def items_list(
        q: str = Query("", max_length=200),
        type: str = Query("", max_length=32),
        limit: int = Query(200, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        where, params = [], {"limit": limit}
        if q.strip():
            where.append("summary ILIKE :q"); params["q"] = f"%{q.strip()}%"
        if type.strip():
            where.append("memory_type = :t"); params["t"] = type.strip()
        wh = f"WHERE {' AND '.join(where)}" if where else ""
        sql = text(
            f"SELECT id, memory_type, summary, created_at FROM memory_items {wh} "
            f"ORDER BY created_at DESC LIMIT :limit"
        )
        with eng.connect() as c:
            rows = c.execute(sql, params).mappings().all()
        return [dict(r) for r in rows]

    @app.get("/api/resources")
    async def resources(limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
        sql = text(
            "SELECT id, modality, url, created_at FROM resources ORDER BY created_at DESC LIMIT :limit"
        )
        with eng.connect() as c:
            rows = c.execute(sql, {"limit": limit}).mappings().all()
        return [dict(r) for r in rows]

    # ============ 编辑接口 ============

    @app.patch("/api/items/{item_id}")
    async def patch_item(item_id: str, body: ItemPatch) -> dict[str, Any]:
        new_summary = body.summary.strip()
        vec = await _embed_one(new_summary)
        params: dict[str, Any] = {"id": item_id, "summary": new_summary}
        if vec is not None:
            sql = text(
                "UPDATE memory_items SET summary = :summary, embedding = (:vec)::vector, "
                "updated_at = NOW() WHERE id = :id"
            )
            params["vec"] = _vec_literal(vec)
        else:
            sql = text(
                "UPDATE memory_items SET summary = :summary, updated_at = NOW() WHERE id = :id"
            )
        with eng.begin() as c:
            r = c.execute(sql, params)
            if r.rowcount == 0:
                raise HTTPException(404, "not found")
        return {"ok": True, "embedded": vec is not None}

    @app.delete("/api/items/{item_id}")
    async def delete_item(item_id: str) -> dict[str, Any]:
        with eng.begin() as c:
            r = c.execute(text("DELETE FROM memory_items WHERE id = :id"), {"id": item_id})
            if r.rowcount == 0:
                raise HTTPException(404, "not found")
        return {"ok": True}

    @app.patch("/api/categories/{cat_id}")
    async def patch_category(cat_id: str, body: CategoryPatch) -> dict[str, Any]:
        sets: list[str] = []
        params: dict[str, Any] = {"id": cat_id}
        embed_source: str | None = None
        if body.summary is not None:
            sets.append("summary = :summary")
            params["summary"] = body.summary.strip()
            embed_source = params["summary"]
        if body.description is not None:
            sets.append("description = :description")
            params["description"] = body.description.strip()
            # 优先用 summary 做 embedding；没有的话用 description
            if embed_source is None:
                embed_source = params["description"]
        if not sets:
            raise HTTPException(400, "nothing to update")

        embedded = False
        if embed_source:
            vec = await _embed_one(embed_source)
            if vec is not None:
                sets.append("embedding = (:vec)::vector")
                params["vec"] = _vec_literal(vec)
                embedded = True
        sets.append("updated_at = NOW()")
        sql = text(f"UPDATE memory_categories SET {', '.join(sets)} WHERE id = :id")
        with eng.begin() as c:
            r = c.execute(sql, params)
            if r.rowcount == 0:
                raise HTTPException(404, "not found")
        return {"ok": True, "embedded": embedded}

    @app.delete("/api/categories/{cat_id}")
    async def delete_category(cat_id: str) -> dict[str, Any]:
        with eng.begin() as c:
            # 先手动删 category_items（避免外键约束）
            c.execute(text("DELETE FROM category_items WHERE category_id = :id"), {"id": cat_id})
            r = c.execute(text("DELETE FROM memory_categories WHERE id = :id"), {"id": cat_id})
            if r.rowcount == 0:
                raise HTTPException(404, "not found")
        return {"ok": True}

    return app
