"""Dashboard, GitHub webhook and status API."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import time
from html import escape
from urllib.parse import parse_qs, quote

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import config, store, worker

COOKIE = "hub_session"
PUBLIC = {"/login", "/healthz", "/webhook/github"}


def _session_token() -> str:
    return hmac.new(config.API_KEY.encode(), b"graphify-hub-session", hashlib.sha256).hexdigest()


def _authed(request: Request) -> bool:
    bearer = request.headers.get("authorization", "")
    if bearer.lower().startswith("bearer ") and hmac.compare_digest(bearer[7:].strip(), config.API_KEY):
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), _session_token())


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path in PUBLIC or _authed(request):
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=303)


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", "replace")
    return {k: v[0].strip() for k, v in parse_qs(body).items()}


def _back(msg: str = "") -> RedirectResponse:
    return RedirectResponse("/" + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


# ── GitHub repo list (cached; the picker should not hit the API on every load) ─

_gh_cache: dict = {"at": 0.0, "repos": [], "error": None}


def _github_repos() -> tuple[list[dict], str | None]:
    if not config.GITHUB_TOKEN:
        return [], None
    if time.time() - _gh_cache["at"] > 300:
        try:
            _gh_cache.update(repos=worker.github_repos(), error=None)
        except Exception as e:
            _gh_cache["error"] = f"Could not list GitHub repos: {e}"
        _gh_cache["at"] = time.time()
    return _gh_cache["repos"], _gh_cache["error"]


# ── pages ─────────────────────────────────────────────────────────────────────

CSS = """
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--text:#e6e8ee;--dim:#8b93a7;--acc:#5aa9ff;--ok:#3ecf8e;--warn:#f5b84b;--err:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 20px}h1{font-size:20px;margin:0}h2{font-size:15px;margin:28px 0 10px;color:var(--dim);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:8px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--dim);font-weight:500;font-size:12px}
tr:last-child td{border-bottom:0}.dim{color:var(--dim)}.mono,code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.badge{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.ok{color:var(--ok)}.error{color:var(--err)}.queued,.syncing,.building,.pending,.removing{color:var(--warn)}
input,button{font:inherit;color:var(--text);background:#0c0e12;border:1px solid var(--line);border-radius:7px;padding:7px 10px}
button{cursor:pointer;background:#222836}button:hover{border-color:var(--acc)}button.primary{background:var(--acc);color:#06121f;border-color:var(--acc);font-weight:600}
button.danger:hover{border-color:var(--err);color:var(--err)}form.inline{display:inline}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}.msg{margin:12px 0;padding:10px 12px;border-radius:8px;background:#1c2433;border:1px solid var(--line)}
pre{background:#0c0e12;border:1px solid var(--line);border-radius:8px;padding:12px;overflow:auto;white-space:pre-wrap;word-break:break-all}
.err-text{color:var(--err);font-size:12px;max-width:420px;white-space:pre-wrap}
"""


def _page(title: str, body: str, refresh: bool = False) -> HTMLResponse:
    meta = '<meta http-equiv="refresh" content="10">' if refresh else ""
    return HTMLResponse(
        f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{meta}<title>{escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"
    )


async def login(request: Request):
    if request.method == "POST":
        key = (await _form(request)).get("key", "")
        if hmac.compare_digest(key, config.API_KEY):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, _session_token(), httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30)
            return resp
        await asyncio.sleep(1)
        return _page("Graphify Hub", '<h1>Graphify Hub</h1><p class="error">Wrong key.</p>' + _login_form())
    return _page("Graphify Hub", "<h1>Graphify Hub</h1>" + _login_form())


def _login_form() -> str:
    return ('<form method="post" class="row" style="margin-top:16px"><input type="password" name="key" '
            'placeholder="HUB_API_KEY" autofocus size="40"><button class="primary">Sign in</button></form>')


async def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


async def dashboard(request: Request):
    repos = store.all_repos()
    gh_repos, gh_error = _github_repos()
    host = request.url.hostname or "<truenas-host>"
    msg = request.query_params.get("msg")
    busy = any(r.get("status") in ("queued", "syncing", "building", "removing") for r in repos.values())

    rows = []
    for name, r in repos.items():
        status = r.get("status", "pending")
        q = quote(name, safe="/")
        graph_link = (f'<a href="/graph/{q}" target="_blank">graph</a>'
                      if worker.graph_html(name).exists() else '<span class="dim">no graph yet</span>')
        size = f'{r["nodes"]:,} nodes · {r["edges"]:,} edges' if r.get("nodes") is not None else ""
        err = f'<div class="err-text">{escape(r["error"])}</div>' if r.get("error") else ""
        rows.append(f"""<tr>
          <td><a href="https://github.com/{escape(q)}" target="_blank">{escape(name)}</a>
              <div class="dim mono">{escape(r.get("branch", ""))} · {escape((r.get("last_sha") or "")[:8])}</div></td>
          <td><span class="badge {escape(status)}">{escape(status)}</span>{err}</td>
          <td class="dim">{escape(size)}<div>{escape(r.get("last_build") or "never built")}</div></td>
          <td class="mono dim">{escape(str(worker.repo_root(name)))}</td>
          <td><div class="row">{graph_link}
            <form class="inline" method="post" action="/repos/{q}/sync"><button>Sync</button></form>
            <form class="inline" method="post" action="/repos/{q}/sync?force=1"><button title="Rebuild even if no new commits">Rebuild</button></form>
            <form class="inline" method="post" action="/repos/{q}/remove" onsubmit="return confirm('Remove {escape(name)} and its graph?')">
              <button class="danger">Remove</button></form></div></td></tr>""")
    table = ("<table><tr><th>Repo</th><th>Status</th><th>Graph</th><th>project_path</th><th></th></tr>"
             + "".join(rows) + "</table>") if rows else '<p class="dim">No repos yet. Add one below.</p>'

    options = "".join(f'<option value="{escape(g["full_name"])}">{escape(g["default_branch"])}'
                      f'{" · private" if g["private"] else ""}</option>'
                      for g in gh_repos if g["full_name"] not in repos)
    branches = json.dumps({g["full_name"]: g["default_branch"] for g in gh_repos}).replace("<", "\\u003c")

    mcp_cmd = (f'claude mcp add --transport http graphify http://{host}:{config.MCP_PORT}/mcp \\\n'
               f'  --header "Authorization: Bearer $GRAPHIFY_HUB_KEY"')
    body = f"""
<header><h1>Graphify Hub</h1><div class="row">
  <span class="dim">MCP server: <b class="{'ok' if worker.mcp_running() else 'pending'}">{'running' if worker.mcp_running() else 'waiting for first graph'}</b>
  · queue {worker.queue_size()} · sync every {config.SYNC_INTERVAL_MIN or '—'} min · {escape(config.BUILD_MODE)}</span>
  <form class="inline" method="post" action="/sync-all"><button>Sync all</button></form>
  <a href="/logout">Sign out</a></div></header>
{f'<div class="msg">{escape(msg)}</div>' if msg else ''}
{f'<div class="msg error">{escape(gh_error)}</div>' if gh_error else ''}
<h2>Repositories</h2><div class="panel">{table}</div>
<h2>Add a repository</h2><div class="panel">
  <form method="post" action="/repos/add" class="row">
    <input name="full_name" list="ghrepos" placeholder="owner/repo" size="40" required
           oninput="var b={branches}[this.value];if(b)this.form.branch.value=b">
    <datalist id="ghrepos">{options}</datalist>
    <input name="branch" placeholder="branch" value="main" size="16">
    <button class="primary">Add &amp; build</button></form>
  <p class="dim">Start typing to pick from the repos your GitHub token can see.</p></div>
<h2>Connect an agent</h2><div class="panel">
  <p>On each machine, with the hub key in <code>GRAPHIFY_HUB_KEY</code>:</p><pre>{escape(mcp_cmd)}</pre>
  <p class="dim">Queries with no <code>project_path</code> search the global graph across every repo.
  Pass a repo's <code>project_path</code> (column above) to scope a query to that repo.
  For PR tools, also pass <code>repo: "owner/repo"</code>.</p></div>"""
    return _page("Graphify Hub", body, refresh=busy)


async def add_repo(request: Request):
    form = await _form(request)
    name = form.get("full_name", "").removeprefix("https://github.com/").removesuffix(".git").strip("/")
    branch = form.get("branch") or "main"
    if not store.valid_name(name):
        return _back("Use the form owner/repo.")
    if not store.valid_branch(branch):
        return _back("That branch name isn't valid.")
    store.add(name, branch)
    worker.enqueue(name, "sync", force=True)
    return _back(f"Added {name}; first build queued.")


async def sync_repo(request: Request):
    name = f'{request.path_params["owner"]}/{request.path_params["repo"]}'
    if not store.get(name):
        return _back("Unknown repo.")
    worker.enqueue(name, "sync", force=request.query_params.get("force") == "1")
    return _back(f"Queued {name}.")


async def remove_repo(request: Request):
    name = f'{request.path_params["owner"]}/{request.path_params["repo"]}'
    if store.get(name):
        store.update(name, status="removing")
        worker.enqueue(name, "remove")
    return _back(f"Removing {name}.")


async def sync_all(request: Request):
    return _back(f"Queued {worker.enqueue_all()} repo(s).")


async def graph_view(request: Request):
    name = f'{request.path_params["owner"]}/{request.path_params["repo"]}'
    path = worker.graph_html(name)
    if not store.get(name) or not path.exists():
        return Response("No graph for that repo yet.", status_code=404)
    # Sandboxed: the page's scripts run in an opaque origin, so they can't use
    # the dashboard session cookie or post to the dashboard.
    return FileResponse(path, media_type="text/html",
                        headers={"Content-Security-Policy": "sandbox allow-scripts allow-popups"})


async def api_status(request: Request):
    return JSONResponse({"mcp_running": worker.mcp_running(), "queue": worker.queue_size(),
                         "repos": store.all_repos()})


async def webhook(request: Request):
    if not config.WEBHOOK_SECRET:
        return JSONResponse({"error": "webhooks disabled (WEBHOOK_SECRET not set)"}, status_code=404)
    body = await request.body()
    expected = "sha256=" + hmac.new(config.WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("x-hub-signature-256", ""), expected):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    event = request.headers.get("x-github-event", "")
    if event == "ping":
        return JSONResponse({"ok": True, "pong": True})
    if event != "push":
        return JSONResponse({"ok": True, "ignored": event})
    payload = json.loads(body)
    full = (payload.get("repository") or {}).get("full_name", "")
    match = next((n for n in store.all_repos() if n.lower() == full.lower()), None)
    if not match:
        return JSONResponse({"ok": True, "ignored": "repo not tracked"})
    branch = store.get(match)["branch"]
    if payload.get("ref") != f"refs/heads/{branch}":
        return JSONResponse({"ok": True, "ignored": "other branch"})
    worker.enqueue(match, "sync")
    return JSONResponse({"ok": True, "queued": match})


async def healthz(request: Request):
    return JSONResponse({"ok": True})


@contextlib.asynccontextmanager
async def lifespan(app):
    worker.start()
    yield
    worker.stop()


def create_app() -> Starlette:
    repo = "/{owner:str}/{repo:str}"
    return Starlette(
        routes=[
            Route("/", dashboard),
            Route("/login", login, methods=["GET", "POST"]),
            Route("/logout", logout),
            Route("/repos/add", add_repo, methods=["POST"]),
            Route(f"/repos{repo}/sync", sync_repo, methods=["POST"]),
            Route(f"/repos{repo}/remove", remove_repo, methods=["POST"]),
            Route("/sync-all", sync_all, methods=["POST"]),
            Route(f"/graph{repo}", graph_view),
            Route("/api/status", api_status),
            Route("/webhook/github", webhook, methods=["POST"]),
            Route("/healthz", healthz),
        ],
        middleware=[Middleware(AuthMiddleware)],
        lifespan=lifespan,
    )
