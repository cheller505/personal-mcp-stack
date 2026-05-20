# Contributing

Thanks for considering it. This is a personal project that became
sharable. Contributions are welcome — please open an issue first if you
want to discuss anything non-trivial.

## What's most wanted

**New source MCPs.** The 5 existing ones cover my needs; yours are
probably different. Particularly interesting:
- Calendar (Google or Microsoft Graph)
- GitHub issues/PRs
- Notion
- Confluence / Jira
- Linear
- Apple Notes (via export)
- Filesystem (a markdown-vault watcher)

**Count/aggregate tools.** `slack-mcp` and `email-mcp` have
`count_messages` for fast aggregation. `clickup-mcp`, `granola-mcp`,
`onenote-mcp` would benefit from the same pattern — see
`slack_mcp/tools.py` for the template.

**Auth hardening.** The chat UI auth is a deliberate stub
(`chat-mcp/chat/auth.py`). A real password/passkey gate that doesn't
assume Tailscale would be useful for non-Tailscale deployments.

**Better orchestration.** The orchestrator is bounded at 10 tool-call
iterations with naive parallelism. Smarter scheduling (e.g. dependency
detection, partial-failure tolerance) would help on more complex queries.

**Semantic search layer.** FTS5 is fast and good for keywords; for
fuzzy questions (e.g. "emails about the GPU procurement decision") an
embedding layer + vector store would help. The MCP shape is agnostic —
add a `semantic_search` tool to each source, or a global one in chat-mcp.

## Adding a new source MCP

Use any existing source as a template. `clickup-mcp/` is the simplest
(REST API, paste-token auth, no MSAL device flow). High-level recipe:

1. `cp -r clickup-mcp <new>-mcp`
2. Rename package: `mv <new>-mcp/clickup_mcp <new>-mcp/<new>_mcp`
3. Find/replace `clickup`/`ClickUp` → `<new>`/`<New>` across files
4. Rewrite `<new>_mcp/api.py` for the new upstream HTTP API. Keep the rate
   limiter pattern (sliding window + respect-Retry-After).
5. Adjust the schema in `<new>_mcp/database.py`. WAL + FTS5 with a
   contentless table is the pattern; if your search content spans
   multiple tables, copy the `messages_fts_rowid` mapping pattern from
   slack-mcp.
6. Adjust full + delta semantics in `<new>_mcp/sync.py`. Most upstreams
   support some form of `updated_after` filter or delta token; if not,
   walk newest-first and stop at a cutoff.
7. Write the tool surface in `<new>_mcp/tools.py`. At minimum:
   `list_*`, `search_*` (FTS5-backed), `get_*`, `count_*`, plus
   `sync_status` and `force_sync` (hide these from the LLM via the chat
   `hidden_tools` list).
8. `<new>_mcp/server.py` — pick a free port (8771+) and copy
   `email_mcp/server.py` verbatim, just rename the inner class. Do NOT
   use Starlette `Route` objects; the raw ASGI app pattern is required
   because Starlette 1.0 removed `Request._send` which the MCP SDK
   examples assume.
9. Update `<new>-mcp.service`: working dir, ExecStart, log path.
10. `./install.sh` to create the venv and enable the unit (not start).
11. First run interactively to complete any auth dance; then
    `systemctl --user start <new>-mcp.service`.
12. Add to `~/.chat-mcp/config.json` under `mcp_servers`. Restart
    chat-mcp.

Add a section to `OPERATIONS.md` documenting any new gotchas. The
"known issues" catalog is the load-bearing piece of the docs.

## Code style

- Match the existing files. Terse, no docstring novels, comments only
  for non-obvious *why*.
- Type hints on public functions, optional elsewhere.
- Async by default in I/O paths; sync is fine for in-process work.
- No new dependencies without a real reason. The current set is small
  (`httpx` or `aiohttp`, `mcp`, `uvicorn`, `apscheduler`, BeautifulSoup
  where HTML stripping is needed).

## Testing

There are no tests. Honestly — it's a personal infra project, integration
tests against live APIs are flaky, and I didn't invest. PRs that add
tests are welcome, but please don't gate functional changes on test
coverage.

If you change something that touches sync logic, run a fresh full sync
against a real account and watch the log for a clean catchup pass.

## License

By contributing, you agree your contribution is licensed under MIT,
matching the rest of the repo.
