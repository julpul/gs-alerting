"""The mockgw log page: a phone-like list of SMS and calls, refreshed every 2 s.

Message text comes from alerts (untrusted), so the page only ever writes it with `textContent`.
"""

from __future__ import annotations

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>mockgw - phone</title>
<style>
 body{font-family:system-ui,sans-serif;background:#eceff1;margin:0;padding:16px}
 h1{font-size:18px;margin:0 0 12px}
 #list{max-width:520px;margin:auto}
 .card{background:#fff;border-radius:14px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px #0003}
 .card.call{border-left:6px solid #c62828}.card.sms{border-left:6px solid #1565c0}
 .meta{font-size:12px;color:#607d8b;display:flex;justify-content:space-between}
 .text{margin:6px 0 8px;white-space:pre-wrap;word-break:break-word}
 .row{display:flex;gap:6px}input{flex:1;padding:6px;border:1px solid #b0bec5;border-radius:6px}
 button{padding:6px 12px;border:0;border-radius:6px;background:#1565c0;color:#fff;cursor:pointer}
 button.ack{background:#2e7d32}.empty{text-align:center;color:#78909c;margin-top:40px}
 .note{font-size:12px;color:#2e7d32;margin-top:4px}
</style></head><body>
<div id="list"><h1>mockgw - SMS and calls</h1><div id="items"><div class="empty">Nothing yet.</div></div></div>
<script>
const drafts = new Map(), notes = new Map();
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text; return e; };
async function post(url, body) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  return r.ok;
}
function card(kind, m) {
  const c = el('div', 'card ' + kind);
  const meta = el('div', 'meta');
  meta.append(el('span', '', (kind === 'call' ? 'Call to ' : 'SMS to ') + m.to), el('span', '', m.at));
  c.append(meta, el('div', 'text', m.text));
  if (kind === 'call') {
    c.append(el('div', 'meta', 'Ringing... Press 1 to ACK'));
    const b = el('button', 'ack', 'Press 1 to ACK');
    b.onclick = async () => { notes.set(m.id, (await post('/calls/' + m.id + '/press', {digit: '1'})) ? 'ACK sent' : 'ACK failed'); load(); };
    c.append(b);
  }
  const row = el('div', 'row');
  const input = el('input'); input.placeholder = 'Reply, e.g. ACK 1234'; input.value = drafts.get(m.id) || '';
  input.oninput = () => drafts.set(m.id, input.value);
  const send = el('button', '', 'Reply');
  send.onclick = async () => { const text = input.value.trim(); if (!text) return;
    notes.set(m.id, (await post('/reply', {from: m.to, text})) ? 'Reply sent' : 'Reply failed');
    drafts.delete(m.id); load(); };
  row.append(input, send); c.append(row);
  if (notes.has(m.id)) c.append(el('div', 'note', notes.get(m.id)));
  return c;
}
async function load() {
  if (document.activeElement && document.activeElement.tagName === 'INPUT' && document.activeElement.value) return;
  const s = await (await fetch('/admin/state')).json();
  const all = [...s.sms.map(m => ['sms', m]), ...s.calls.map(m => ['call', m])]
    .sort((a, b) => b[1].at.localeCompare(a[1].at));
  const items = document.getElementById('items'); items.replaceChildren();
  if (!all.length) items.append(el('div', 'empty', 'Nothing yet.'));
  for (const [kind, m] of all) items.append(card(kind, m));
}
load(); setInterval(load, 2000);
</script></body></html>
"""


def render_page() -> str:
    return PAGE
