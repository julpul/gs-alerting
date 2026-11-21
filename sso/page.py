"""HTML for Vistula ID: sign-in, account picker, signed-out and error pages (no JS needed)."""

from __future__ import annotations

from html import escape
from typing import Any

BRAND = "Vistula ID"
ORG = "Vistula Capital"

LOGO = """<svg width="36" height="36" viewBox="0 0 36 36" aria-hidden="true"><rect width="36" height="36" rx="9" fill="#0b4f4a"/>
<path d="M7 14c4-4 7 4 11 0s7 4 11 0" fill="none" stroke="#7fe0c9" stroke-width="2.6" stroke-linecap="round"/>
<path d="M7 21c4-4 7 4 11 0s7 4 11 0" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/></svg>"""

CSS = """
:root{--brand:#0b4f4a;--brand2:#0f6b63;--ink:#14211f;--muted:#5b6b68;--line:#d9e2e0;--bg:#eef3f2;--err:#b42318}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{font:15px/1.5 "Inter",system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg);display:flex;flex-direction:column}
.wrap{flex:1;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
.side{background:linear-gradient(160deg,#0b4f4a 0%,#0f6b63 55%,#138a7d 100%);color:#e8f6f3;padding:56px;display:flex;flex-direction:column;justify-content:space-between}
.side h2{font:600 30px/1.25 "Inter",system-ui,sans-serif;margin:0 0 12px;color:#fff;max-width:420px}
.side p{margin:0;max-width:420px;color:#cfe9e4}
.side small{color:#a9d6cd}
.main{display:flex;align-items:center;justify-content:center;padding:32px 16px}
.card{width:100%;max-width:400px;background:#fff;border:1px solid var(--line);border-radius:14px;padding:32px;box-shadow:0 12px 32px rgba(11,79,74,.08)}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px;color:var(--brand);margin-bottom:22px}
.brand span{color:var(--muted);font-weight:500}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 22px}
.sub b{color:var(--ink)}
label{display:block;font-weight:600;font-size:13px;margin:14px 0 6px}
input[type=text],input[type=password]{width:100%;height:44px;padding:0 12px;border:1px solid #b9c7c4;border-radius:8px;font:inherit;background:#fff}
input:focus{outline:2px solid var(--brand2);outline-offset:1px;border-color:var(--brand2)}
.btn{display:block;width:100%;height:44px;margin-top:20px;border:0;border-radius:8px;background:var(--brand);color:#fff;font:600 15px/44px inherit;cursor:pointer;text-align:center;text-decoration:none}
.btn:hover{background:var(--brand2)}
.btn.ghost{background:#fff;color:var(--brand);border:1px solid var(--line);margin-top:10px}
.err{background:#fef3f2;border:1px solid #fecdca;color:var(--err);border-radius:8px;padding:10px 12px;margin-bottom:6px;font-size:14px}
.row{display:flex;justify-content:space-between;align-items:center;margin-top:14px;font-size:13px;color:var(--muted)}
.row a{color:var(--brand2);text-decoration:none}
.acct{display:flex;align-items:center;gap:12px;width:100%;padding:12px;border:1px solid var(--line);border-radius:10px;background:#fff;font:inherit;text-align:left;cursor:pointer}
.acct:hover{border-color:var(--brand2);background:#f5faf9}
.av{width:40px;height:40px;border-radius:50%;background:#d7efe9;color:var(--brand);display:grid;place-items:center;font-weight:700}
.acct small{display:block;color:var(--muted)}
.foot{padding:14px 24px;font-size:12px;color:var(--muted);display:flex;gap:18px;justify-content:center;flex-wrap:wrap}
.secure{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);margin-top:18px}
@media (max-width:860px){.wrap{grid-template-columns:1fr}.side{display:none}}
"""


def _shell(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)} · {BRAND}</title>
<style>{CSS}</style></head><body><div class="wrap">
<aside class="side"><div class="brand" style="color:#fff">{LOGO}<div>{ORG}</div></div>
<div><h2>One sign-in for every {ORG} tool.</h2><p>Use your work account. Your access to each application is
managed by IT through your department and role.</p></div><small>{BRAND} · single sign-on (OpenID Connect)</small></aside>
<main class="main"><div class="card">{body}</div></main></div>
<footer class="foot"><span>© 2026 {ORG}</span><span>IT Service Desk · ext. 4357</span><span>Privacy</span>
<span>Demo identity provider: replace with Entra ID / Okta by configuration</span></footer></body></html>"""


def _hidden(params: dict[str, str]) -> str:
    return "".join(f'<input type="hidden" name="{escape(k)}" value="{escape(v)}">' for k, v in params.items())


def _head() -> str:
    return f'<div class="brand">{LOGO}<div>{BRAND} <span>· {ORG}</span></div></div>'


def login(params: dict[str, str], client: str, username: str = "", error: str = "") -> str:
    err = f'<div class="err" role="alert">{escape(error)}</div>' if error else ""
    body = f"""{_head()}<h1>Sign in</h1><p class="sub">to continue to <b>{escape(client)}</b></p>{err}
<form method="post" action="/authorize">{_hidden(params)}
<label for="u">Work username or email</label>
<input id="u" name="username" type="text" autocomplete="username" value="{escape(username)}" placeholder="name or name@demo" required autofocus>
<label for="p">Password</label>
<input id="p" name="password" type="password" autocomplete="current-password" required>
<button class="btn" type="submit">Sign in</button></form>
<div class="row"><span><input type="checkbox" checked disabled> Keep me signed in (8 h)</span><a href="#">Forgot password?</a></div>
<div class="secure">🔒 You are signing in to {escape(client)} through {BRAND}. {escape(client)} never sees your password.</div>"""
    return _shell("Sign in", body)


def picker(params: dict[str, str], user: dict[str, Any], client: str) -> str:
    initials = "".join(p[0] for p in str(user["name"]).split()[:2]).upper()
    body = f"""{_head()}<h1>Pick an account</h1><p class="sub">to continue to <b>{escape(client)}</b></p>
<form method="post" action="/authorize">{_hidden(params)}<input type="hidden" name="action" value="continue">
<button class="acct" type="submit"><span class="av">{escape(initials)}</span><span><b>{escape(user["name"])}</b>
<small>{escape(user["email"])} · signed in</small></span></button></form>
<form method="get" action="/authorize">{_hidden(params)}<input type="hidden" name="prompt" value="login">
<button class="btn ghost" type="submit">Use another account</button></form>"""
    return _shell("Pick an account", body)


def signed_out() -> str:
    body = f"""{_head()}<h1>You signed out</h1><p class="sub">Your {BRAND} session has ended on this device.
Close the browser to finish signing out of every application.</p>"""
    return _shell("Signed out", body)


def error(message: str) -> str:
    body = f"""{_head()}<h1>Can't sign you in</h1><div class="err">{escape(message)}</div>
<p class="sub">Ask the IT Service Desk if this keeps happening.</p>"""
    return _shell("Error", body)


def home(user: dict[str, Any] | None, people: int) -> str:
    who = (
        f"<p class='sub'>Signed in as <b>{escape(user['name'])}</b> ({escape(user['email'])}).</p>"
        f"<a class='btn ghost' href='/logout'>Sign out of {BRAND}</a>"
        if user
        else "<p class='sub'>Not signed in. Open an application such as Herald and choose single sign-on.</p>"
    )
    body = (
        f"""{_head()}<h1>{BRAND}</h1><p class="sub">Single sign-on for {ORG} · {people} accounts.</p>{who}"""
    )
    return _shell(BRAND, body)
