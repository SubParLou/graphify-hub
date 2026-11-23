"""Settings, read once from the environment."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


DATA_DIR = Path(os.environ.get("HUB_DATA_DIR", "/data"))
REPOS_DIR = DATA_DIR / "repos"          # <owner>/<repo>/src = clone, <owner>/<repo>/graphify-out = graph
HOME_DIR = DATA_DIR / "home"            # HOME for graphify, git and gh
STATE_FILE = DATA_DIR / "hub" / "state.json"
GLOBAL_GRAPH = HOME_DIR / ".graphify" / "global-graph.json"

API_KEY = os.environ.get("HUB_API_KEY", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

HUB_PORT = _int("HUB_PORT", 8780)
MCP_PORT = _int("MCP_PORT", 8781)
SYNC_INTERVAL_MIN = _int("SYNC_INTERVAL_MINUTES", 15)   # 0 disables scheduled syncs
BUILD_TIMEOUT_SEC = _int("BUILD_TIMEOUT_SECONDS", 3600)

# code-only: local tree-sitter parse, no LLM, no tokens.
# full: also runs the semantic pass over docs/PDFs; needs an LLM key in the env
#       (ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENAI_BASE_URL for Ollama, ...).
BUILD_MODE = os.environ.get("BUILD_MODE", "code-only").strip()
# Name communities with the LLM backend after each build (costs tokens).
LLM_LABELS = os.environ.get("LLM_LABELS", "0").strip() == "1"


def check() -> None:
    problems = []
    if len(API_KEY) < 16:
        problems.append("HUB_API_KEY must be set (16+ chars, e.g. `openssl rand -hex 32`)")
    if not GITHUB_TOKEN:
        print("[hub] warning: GITHUB_TOKEN not set; only public repos will clone, and the repo picker "
              "and PR tools are off", file=sys.stderr)
    if BUILD_MODE not in ("code-only", "full"):
        problems.append("BUILD_MODE must be 'code-only' or 'full'")
    if problems:
        for p in problems:
            print(f"[hub] config error: {p}", file=sys.stderr)
        sys.exit(2)
    for d in (REPOS_DIR, HOME_DIR, STATE_FILE.parent):
        d.mkdir(parents=True, exist_ok=True)
