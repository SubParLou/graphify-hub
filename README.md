# Graphify Hub

A self-hosted, always-on [graphify](https://github.com/Graphify-Labs/graphify) server: one place that keeps
code knowledge graphs of your GitHub repos current and serves them to every machine's coding agent over MCP.

It is glue around graphify, not a fork. graphify is a pinned dependency (`requirements.txt`), so upgrading the
graph engine is one version bump.

## What it does

- **Keeps graphs current.** Clones your repos (private ones too), pulls on a schedule or on a GitHub push
  webhook, and rebuilds a repo's graph only when its commit changed. Code is parsed locally with tree-sitter:
  no LLM, no tokens.
- **Cross-repo graph.** Every repo is merged into one global graph, with calls that cross repos linked.
- **One MCP endpoint for all machines.** Runs graphify's HTTP MCP server on the global graph. Graphs reload on
  change; nothing restarts.
- **Dashboard.** Add repos from a picker of what your token can see, watch build status, sync/rebuild/remove,
  open each repo's interactive `graph.html`.
- **PR impact.** graphify's `list_prs` / `get_pr_impact` / `triage_prs` MCP tools work against your repos.

```
GitHub ──push/webhook──▶ graphify-hub (TrueNAS)
                           ├─ :8780 dashboard + /webhook/github
                           └─ :8781 /mcp ◀── Claude Code / Cursor / Codex on each machine (over Tailscale)
/data
  repos/<owner>/<repo>/src            clone
  repos/<owner>/<repo>/graphify-out   graph.json, graph.html, GRAPH_REPORT.md
  home/.graphify/global-graph.json    all repos merged
  hub/state.json                      tracked repos + build status
```

## Deploy on TrueNAS SCALE (24.10+)

1. **Create a dataset** for the hub's data, e.g. `tank/apps/graphify-hub`, and give the `apps` user (uid 568)
   write access (Datasets → Edit Permissions → owner `apps`).
2. **Create a GitHub token**: fine-grained PAT → repository access to the repos you want indexed →
   permissions *Contents: Read*, *Metadata: Read*, *Pull requests: Read*.
3. **Generate a hub key**: `openssl rand -hex 32`. It protects both the dashboard and the MCP endpoint.
4. **Install**: Apps → Discover Apps → ⋮ → *Install via YAML*, paste `docker-compose.yml`, fill in the
   `CHANGE ME` values and the dataset path.
5. Open `http://<truenas-tailscale-name>:8780`, sign in with the hub key, add repos.

The first build of each repo starts as soon as it's added. The MCP server starts once the first graph exists.

### Tailscale

Install the Tailscale app on TrueNAS (or have the host on your tailnet) and reach the hub by its tailnet name.
Don't forward 8780/8781 to the internet: the hub key is the only auth, and traffic is plain HTTP.

### Image visibility

The workflow publishes `ghcr.io/subparlou/graphify-hub`. GHCR packages start private. Either make the
package public (GitHub → Packages → graphify-hub → Package settings → Change visibility) or log TrueNAS into
`ghcr.io` with a token that has `read:packages`.

## Connect your agents

On each machine:

```bash
export GRAPHIFY_HUB_KEY=<hub key>
claude mcp add --transport http graphify http://<truenas-tailscale-name>:8781/mcp \
  --header "Authorization: Bearer $GRAPHIFY_HUB_KEY"
```

- A query without `project_path` searches the **global graph** across all repos.
- `project_path: "/data/repos/<owner>/<repo>"` scopes it to one repo (the dashboard shows each path).
- PR tools also take `repo: "<owner>/<repo>"`.

Remote agents don't get graphify's local "use the graph first" hook. Add a line like this to a project's
`CLAUDE.md` so the agent reaches for it:

> For questions about how this codebase fits together, use the `graphify` MCP tools
> (`query_graph`, `shortest_path`, `get_neighbors`) with `project_path: "/data/repos/<owner>/<repo>"`
> before grepping or reading files.

## GitHub webhook (optional, instant rebuilds)

Set `WEBHOOK_SECRET`, then in each repo: Settings → Webhooks → Add webhook →
payload URL `http://<host>:8780/webhook/github`, content type `application/json`, the same secret,
*Just the push event*. GitHub must be able to reach that URL. Over a Tailscale-only setup, use
[Tailscale Funnel](https://tailscale.com/kb/1223/funnel) for just that path, or skip webhooks and rely on the
scheduled sync.

## Settings

| Variable | Default | |
|---|---|---|
| `HUB_API_KEY` | required | Dashboard login and MCP bearer token (16+ chars) |
| `GITHUB_TOKEN` | – | Needed for private repos, the repo picker and PR tools |
| `WEBHOOK_SECRET` | – | Enables `/webhook/github` |
| `SYNC_INTERVAL_MINUTES` | `15` | Scheduled pull + rebuild-if-changed. `0` disables |
| `BUILD_MODE` | `code-only` | `full` also maps docs/PDFs/images with an LLM (set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `OPENAI_BASE_URL` + `OPENAI_MODEL` for Ollama) |
| `LLM_LABELS` | `0` | `1` names graph communities with the LLM after each build |
| `BUILD_TIMEOUT_SECONDS` | `3600` | Per graphify step |
| `HUB_PORT` / `MCP_PORT` | `8780` / `8781` | |

## Upgrading graphify

Bump `graphifyy[mcp]==…` in `requirements.txt` and push. The workflow publishes a new image; update the app
on TrueNAS. Then hit *Rebuild* on each repo to pick up extractor fixes (a plain sync skips unchanged commits).

## Run locally

```bash
pip install -r requirements.txt
HUB_DATA_DIR=./data HUB_API_KEY=$(openssl rand -hex 32) GITHUB_TOKEN=... python -m hub
```

## Not included

Graphify Cloud's formal verification, SDLC-wide code review, meeting ingestion and Sentry/Jira connectors.
