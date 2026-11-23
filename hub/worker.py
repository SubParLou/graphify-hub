"""Background work: clone/pull repos, build graphs, merge the global graph,
and keep the graphify MCP server running.

Everything that touches the global graph runs on one worker thread, so two
builds never write global-graph.json at the same time.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import config, store

_queue: "queue.Queue[tuple[str, str, bool]]" = queue.Queue()
_pending: set[str] = set()
_pending_lock = threading.Lock()
_stop = threading.Event()
_mcp_proc: subprocess.Popen | None = None


def log(msg: str) -> None:
    print(f"[hub] {msg}", flush=True)


# ── environment for child processes ──────────────────────────────────────────

def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = str(config.HOME_DIR)
    env["GRAPHIFY_NO_CTA"] = "1"
    env["GRAPHIFY_QUERY_LOG"] = str(config.DATA_DIR / "hub" / "queries.log")
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_CONFIG_DIR"] = str(config.HOME_DIR / ".config" / "gh")
    if config.GITHUB_TOKEN:
        env["GH_TOKEN"] = config.GITHUB_TOKEN
        # Token goes to git as an HTTP header through env-only config, so it never
        # lands in argv (visible in ps) or in the clone's .git/config.
        basic = base64.b64encode(f"x-access-token:{config.GITHUB_TOKEN}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
    # The hub's own secrets are not the build's business.
    for k in ("HUB_API_KEY", "WEBHOOK_SECRET", "GITHUB_TOKEN"):
        env.pop(k, None)
    return env


def _run(args: list[str], cwd: Path | None = None, timeout: int = 600) -> str:
    res = subprocess.run(
        args, cwd=cwd, env=_child_env(), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if res.returncode != 0:
        tail = (res.stderr or res.stdout).strip().splitlines()[-8:]
        raise RuntimeError(f"{' '.join(args[:3])} failed ({res.returncode}): " + " | ".join(tail))
    return res.stdout


def _graphify(*args: str, timeout: int | None = None) -> str:
    return _run([sys.executable, "-m", "graphify", *args], cwd=config.DATA_DIR,
                timeout=timeout or config.BUILD_TIMEOUT_SEC)


# ── paths ─────────────────────────────────────────────────────────────────────

def repo_root(full_name: str) -> Path:
    """Directory the MCP server's project_path points at (holds graphify-out/)."""
    return config.REPOS_DIR / full_name


def graph_html(full_name: str) -> Path:
    return repo_root(full_name) / "graphify-out" / "graph.html"


# ── queue ─────────────────────────────────────────────────────────────────────

def enqueue(full_name: str, action: str = "sync", force: bool = False) -> bool:
    key = f"{action}:{full_name}"
    with _pending_lock:
        if key in _pending:
            return False
        _pending.add(key)
    if action == "sync":
        store.update(full_name, status="queued")
    _queue.put((action, full_name, force))
    return True


def enqueue_all(force: bool = False) -> int:
    return sum(enqueue(name, "sync", force) for name in store.all_repos())


def queue_size() -> int:
    return _queue.qsize()


# ── one repo ──────────────────────────────────────────────────────────────────

def _sync(full_name: str, force: bool) -> None:
    repo = store.get(full_name)
    if not repo:
        return
    branch = repo["branch"]
    root = repo_root(full_name)
    src = root / "src"
    started = time.monotonic()
    store.update(full_name, status="syncing", error=None)

    if not (src / ".git").exists():
        shutil.rmtree(src, ignore_errors=True)
        src.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--depth", "1", "--single-branch", "--branch", branch,
              f"https://github.com/{full_name}.git", str(src)], timeout=1800)
    else:
        _run(["git", "fetch", "--depth", "1", "origin", branch], cwd=src, timeout=1800)
        _run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=src)
        _run(["git", "clean", "-fdq"], cwd=src)
    sha = _run(["git", "rev-parse", "HEAD"], cwd=src).strip()

    graph = root / "graphify-out" / "graph.json"
    if not force and sha == repo.get("last_sha") and graph.exists():
        store.update(full_name, status="ok", last_checked=store.now())
        return

    store.update(full_name, status="building")
    extract = ["extract", str(src), "--out", str(root)]
    if config.BUILD_MODE == "code-only":
        extract.append("--code-only")
    _graphify(*extract)

    cluster = ["cluster-only", str(root), "--graph", str(graph)]
    if not config.LLM_LABELS:
        cluster.append("--no-label")
    _graphify(*cluster)

    _graphify("global", "add", str(graph), "--as", full_name, timeout=900)

    stats = json.loads(graph.read_text(encoding="utf-8"))
    store.update(
        full_name, status="ok", last_sha=sha, last_build=store.now(), last_checked=store.now(),
        nodes=len(stats.get("nodes", [])), edges=len(stats.get("links", stats.get("edges", []))),
        build_seconds=round(time.monotonic() - started, 1),
    )
    log(f"built {full_name} @ {sha[:8]}")


def _remove(full_name: str) -> None:
    try:
        _graphify("global", "remove", full_name, timeout=900)
    except RuntimeError as e:
        log(f"global remove {full_name}: {e}")   # not in the global graph yet: fine
    root = repo_root(full_name).resolve()
    if config.REPOS_DIR.resolve() in root.parents:
        shutil.rmtree(root, ignore_errors=True)
        owner_dir = root.parent
        if owner_dir != config.REPOS_DIR.resolve() and not any(owner_dir.iterdir()):
            owner_dir.rmdir()
    store.remove(full_name)
    log(f"removed {full_name}")


def _worker_loop() -> None:
    while not _stop.is_set():
        try:
            action, full_name, force = _queue.get(timeout=1)
        except queue.Empty:
            continue
        with _pending_lock:
            _pending.discard(f"{action}:{full_name}")
        try:
            if action == "sync":
                _sync(full_name, force)
            elif action == "remove":
                _remove(full_name)
        except Exception as e:  # keep the worker alive whatever one repo does
            log(f"{action} {full_name} failed: {e}")
            store.update(full_name, status="error", error=str(e)[:2000], last_checked=store.now())
        finally:
            _queue.task_done()


def _scheduler_loop() -> None:
    if config.SYNC_INTERVAL_MIN <= 0:
        return
    while not _stop.wait(config.SYNC_INTERVAL_MIN * 60):
        n = enqueue_all()
        if n:
            log(f"scheduled sync: {n} repo(s) queued")


# ── MCP server supervisor ─────────────────────────────────────────────────────

def mcp_running() -> bool:
    return _mcp_proc is not None and _mcp_proc.poll() is None


def _mcp_loop() -> None:
    """Run graphify's HTTP MCP server on the global graph; restart it if it dies.

    It reloads graph.json files by mtime on its own, so rebuilds need no restart.
    """
    global _mcp_proc
    backoff = 2
    while not _stop.is_set():
        if not config.GLOBAL_GRAPH.exists():
            _stop.wait(5)       # nothing built yet
            continue
        env = _child_env()
        env["GRAPHIFY_API_KEY"] = config.API_KEY
        log(f"starting MCP server on :{config.MCP_PORT}")
        _mcp_proc = subprocess.Popen(
            [sys.executable, "-m", "graphify.serve", str(config.GLOBAL_GRAPH),
             "--transport", "http", "--host", "0.0.0.0", "--port", str(config.MCP_PORT)],
            cwd=config.DATA_DIR, env=env, stdin=subprocess.DEVNULL,
        )
        started = time.monotonic()
        while _mcp_proc.poll() is None and not _stop.is_set():
            _stop.wait(2)
        if _stop.is_set():
            break
        log(f"MCP server exited ({_mcp_proc.returncode}); restarting in {backoff}s")
        backoff = 2 if time.monotonic() - started > 60 else min(backoff * 2, 120)
        _stop.wait(backoff)


def start() -> None:
    for target in (_worker_loop, _scheduler_loop, _mcp_loop):
        threading.Thread(target=target, daemon=True, name=target.__name__).start()
    # Catch up on anything never built or changed while the hub was down.
    enqueue_all()


def stop() -> None:
    _stop.set()
    if mcp_running():
        _mcp_proc.terminate()
        try:
            _mcp_proc.wait(10)
        except subprocess.TimeoutExpired:
            _mcp_proc.kill()


# ── GitHub API (repo picker) ──────────────────────────────────────────────────

def github_repos(max_pages: int = 5) -> list[dict]:
    """Repos the token can see, most recently pushed first."""
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        req = urllib.request.Request(
            f"https://api.github.com/user/repos?per_page=100&page={page}&sort=pushed"
            "&affiliation=owner,collaborator,organization_member",
            headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "graphify-hub"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            batch = json.loads(resp.read())
        out += [{"full_name": r["full_name"], "default_branch": r.get("default_branch") or "main",
                 "private": r.get("private", False)} for r in batch]
        if len(batch) < 100:
            break
    return out
