"""Minimal HTML control panel (no build step, no external assets)."""

PANEL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vistula Capital simulator</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --fg:#1b1f24; --muted:#667085; --line:#e4e7ec; --accent:#1d4ed8;
          --bad:#b42318; --good:#067647; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0f1115; --card:#171a21; --fg:#e8eaed; --muted:#98a2b3; --line:#2a2f3a; --accent:#6ea0ff;
            --bad:#f97066; --good:#47cd89; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  main { max-width: 1040px; margin: 0 auto; padding: 24px 16px 48px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  p.sub { margin: 0 0 20px; color: var(--muted); }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 12px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius: 10px; padding: 14px; }
  .card h2 { font-size: 13px; margin: 0 0 6px; text-transform: uppercase; letter-spacing: .04em;
             color: var(--muted); }
  .card p { margin: 0 0 10px; color: var(--muted); min-height: 38px; }
  button { font: inherit; border:1px solid var(--accent); background: var(--accent); color:#fff;
           padding: 6px 12px; border-radius: 8px; cursor: pointer; }
  button.ghost { background: transparent; color: var(--accent); }
  button:disabled { opacity: .5; cursor: default; }
  section { margin-top: 24px; }
  section > h2 { font-size: 15px; margin: 0 0 8px; }
  table { width:100%; border-collapse: collapse; background:var(--card); border:1px solid var(--line);
          border-radius: 10px; overflow:hidden; }
  th, td { text-align:left; padding: 7px 10px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 600; font-size: 12px; }
  td.num { font-variant-numeric: tabular-nums; }
  .bad { color: var(--bad); } .good { color: var(--good); }
  .row { display:flex; gap: 16px; align-items:center; flex-wrap: wrap; }
  input[type=range] { width: 220px; }
  input[type=text] { font: inherit; padding: 5px 8px; border:1px solid var(--line); border-radius: 8px;
                     background: var(--card); color: var(--fg); width: 100%; margin-bottom: 8px; }
  code { font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
  #toast { position: fixed; right: 16px; bottom: 16px; background: var(--fg); color: var(--bg);
           padding: 8px 12px; border-radius: 8px; opacity: 0; transition: opacity .2s; }
  #toast.on { opacity: 1; }
</style>
</head>
<body>
<main>
  <h1>Vistula Capital simulator</h1>
  <p class="sub">Client systems emitting traffic into <code id="target">...</code></p>

  <div class="card">
    <div class="row">
      <label>Baseline <strong id="rateLabel">2.0</strong> ev/s
        <input id="rate" type="range" min="0" max="50" step="0.5" value="2"></label>
      <span class="sub">effective now: <strong id="effective">0</strong> ev/s</span>
      <button id="pause" class="ghost">Pause</button>
      <label><input id="incidents" type="checkbox"> random incidents</label>
      <span>running scenarios: <strong id="running">0</strong></span>
    </div>
  </div>

  <section>
    <h2>Scenarios</h2>
    <div class="grid" id="scenarios"></div>
  </section>

  <section>
    <h2>Sent per source</h2>
    <table>
      <thead><tr><th>Source</th><th>Sent</th><th>Errors</th></tr></thead>
      <tbody id="sources"></tbody>
      <tfoot><tr><th>Total</th><th id="totSent" class="num"></th>
        <th id="totErr" class="num"></th></tr></tfoot>
    </table>
    <p class="sub" id="extra"></p>
  </section>

  <section>
    <h2>Last responses</h2>
    <table>
      <thead><tr><th>Time</th><th>Source</th><th>Via</th><th>Status</th><th>Origin</th><th>Detail</th></tr></thead>
      <tbody id="last"></tbody>
    </table>
  </section>
</main>
<div id="toast"></div>
<script>
const $ = (id) => document.getElementById(id);
let busy = false;
function toast(text) { const t = $("toast"); t.textContent = text; t.classList.add("on");
  setTimeout(() => t.classList.remove("on"), 1800); }
async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: {"Content-Type": "application/json"},
                               body: body ? JSON.stringify(body) : undefined });
  return r.json().catch(() => ({}));
}
async function loadScenarios() {
  const list = await (await fetch("scenarios")).json();
  $("scenarios").innerHTML = "";
  for (const s of list) {
    const card = document.createElement("div"); card.className = "card";
    const h = document.createElement("h2"); h.textContent = s.name;
    const p = document.createElement("p"); p.textContent = s.description;
    card.append(h, p);
    if (s.name === "new_schema") {
      const key = document.createElement("input"); key.type = "text"; key.id = "customKey";
      key.placeholder = "source key of your new source (optional)"; card.append(key);
    }
    const b = document.createElement("button"); b.textContent = "Run";
    b.onclick = async () => {
      const q = new URLSearchParams();
      const k = s.name === "new_schema" && $("customKey").value.trim();
      if (k) q.set("sourceKey", k);
      const res = await post("scenario/" + s.name + (q.toString() ? "?" + q : ""));
      toast(s.name + ": " + (res.steps ?? "?") + " events queued");
    };
    card.append(b); $("scenarios").append(card);
  }
}
function cell(text, cls) { const td = document.createElement("td"); td.textContent = text;
  if (cls) td.className = cls; return td; }
async function refresh() {
  let s; try { s = await (await fetch("stats")).json(); } catch { return; }
  $("target").textContent = s.targetApiUrl + (s.amqpEnabled ? " (trade-gateway via RabbitMQ)" : "");
  if (!busy) { $("rate").value = s.baseline.baseRate; }
  $("rateLabel").textContent = Number(s.baseline.baseRate).toFixed(1);
  $("effective").textContent = s.baseline.effectiveRate;
  $("pause").textContent = s.baseline.paused ? "Resume" : "Pause";
  $("incidents").checked = s.baseline.autoIncidents;
  $("running").textContent = s.scenarios.running;
  const body = $("sources"); body.innerHTML = "";
  for (const [name, c] of Object.entries(s.sources)) {
    const tr = document.createElement("tr");
    tr.append(cell(name), cell(c.sent, "num"), cell(c.errors, "num" + (c.errors ? " bad" : "")));
    body.append(tr);
  }
  $("totSent").textContent = s.totals.sent; $("totErr").textContent = s.totals.errors;
  $("extra").textContent = "retries " + s.totals.retries + " | amqp published " + s.totals.amqpPublished +
    " | amqp fallbacks " + s.totals.amqpFallbacks;
  const last = $("last"); last.innerHTML = "";
  for (const e of s.last) {
    const tr = document.createElement("tr");
    tr.append(cell(e.at.slice(11, 23)), cell(e.source), cell(e.via),
      cell(e.status ?? "-", e.ok ? "good" : "bad"), cell(e.origin), cell(e.detail.slice(0, 80)));
    last.append(tr);
  }
}
$("rate").oninput = () => { busy = true; $("rateLabel").textContent = Number($("rate").value).toFixed(1); };
$("rate").onchange = async () => { await post("baseline", { rate: Number($("rate").value) }); busy = false; };
$("pause").onclick = async () => { await post("baseline", { paused: $("pause").textContent === "Pause" }); };
$("incidents").onchange = async () => { await post("baseline", { autoIncidents: $("incidents").checked }); };
loadScenarios(); refresh(); setInterval(refresh, 1000);
</script>
</body>
</html>
"""
