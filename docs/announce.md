# Announcement drafts

Pre-written copy you can paste verbatim or trim. All point at the public repo
once it's live.

---

## Short social post — Mastodon / Bluesky (≤500 chars)

> Just shipped a personal-knowledge MCP stack: five local Python services that
> mirror my email, Slack, ClickUp, Granola meetings, and OneNote into local
> SQLite + FTS5 caches, served via the Model Context Protocol. A small chat UI
> on top talks to a local Ollama (qwen3:32b). Search across all of it in
> milliseconds, nothing leaves the machine. MIT licensed.
>
> https://github.com/cheller505/memex

---

## Short social post — X / Twitter (≤280 chars)

> Personal knowledge MCP stack: 5 local Python services mirror my email,
> Slack, tasks, meeting notes, and OneNote into SQLite+FTS5. Chat UI on top
> with local Ollama. Sub-second search across years of data. Nothing leaves
> the machine. MIT.
>
> https://github.com/cheller505/memex

---

## Long-form social post — LinkedIn (300-500 words)

> **A local-first personal knowledge stack with MCP**
>
> I spent two days last week building a self-hosted system that mirrors my
> work data (email, Slack, ClickUp tasks, Granola meeting notes, OneNote pages)
> into local SQLite caches and serves them to a local LLM through the Model
> Context Protocol (MCP). It's now in production on a single GPU host, and
> it's open source: https://github.com/cheller505/memex
>
> Why bother? Three reasons.
>
> **Privacy.** Most "AI assistant" products send your query traffic to a
> third party. For sensitive research-computing planning data, that's a
> non-starter. With everything running on my own hardware (Ollama, qwen3:32b),
> nothing leaves the network.
>
> **Search.** Each system has its own search box. None of them search the
> others. With ~300,000 records cached locally across five sources, a SQLite
> FTS5 query returns sub-second results across my entire work history.
>
> **Composability.** Each data source is a vanilla MCP server, so any MCP-aware
> client (Claude Desktop, Claude Code, custom bots) can use the same tools.
> The chat web UI is just one consumer.
>
> Architecture: five Python services (one per source) doing background
> delta-sync into per-source SQLite + FTS5 caches. A chat web UI on top
> auto-discovers their tools and orchestrates them with a local model.
> Everything runs as systemd user services with auto-restart.
>
> Build cost: ~36 hours of focused work. Most of that was discovering and
> documenting the gotchas: MSAL hard-rejects `offline_access` as a "reserved"
> scope, Starlette 1.0 broke the MCP SDK's SSE examples, OneNote returns 403
> when your OneDrive crosses a 5,000-item SharePoint ceiling, etc. The
> `OPERATIONS.md` file in the repo documents 14 of them.
>
> MIT licensed, PRs and forks welcome. If you've been thinking about doing
> something similar — for your team's Confluence, your Notion vault, your
> calendar, your code-review queue — the per-source MCP is a ~200-line
> skeleton you can fork.
>
> https://github.com/cheller505/memex

---

## CaRCC lightning talk abstract (≤200 words)

> **Local-first AI assistance with the Model Context Protocol: mirroring your
> work data for fast, private, composable search**
>
> Most cloud LLM "connectors" pull query traffic off-host and don't keep a
> local archive. For research-computing professionals juggling sensitive
> infrastructure planning data across email, Slack, tasks, meetings, and
> notebooks, that tradeoff is often unacceptable.
>
> This talk describes a small open-source system that mirrors all those data
> sources into per-source local SQLite caches with FTS5 search and serves
> them to any MCP-aware client (Claude Desktop, Claude Code, custom UIs)
> over the Model Context Protocol. A self-hosted chat UI on top uses a local
> Ollama model to answer cross-source questions with sub-second tool calls.
>
> I'll walk through the architecture, share the 14 gotchas I documented
> during the build (Microsoft Graph delta-token case sensitivity, OneNote's
> 5,000-item SharePoint ceiling, MSAL scope foot-guns, APScheduler async
> footwork, ARM/MoE LLM-runner incompatibilities), and demo the working
> system. The code, docs, and per-source skeleton are MIT-licensed for
> anyone wanting to do the same — or extend it to their team's tools.

---

## Internal NCSA mailing-list email

> **Subject:** Open-source local-first MCP stack — search across all your
> work data
>
> Folks,
>
> Sharing something I built over the past couple of days that some of you
> might find useful: an open-source personal-knowledge stack that mirrors
> your email, Slack, ClickUp tasks, Granola meeting notes, and OneNote pages
> into local SQLite + FTS5 caches and serves them to a local LLM via MCP.
>
> Repo: https://github.com/cheller505/memex
>
> What you get:
>
> - Sub-second full-text search across all five sources at once
> - A chat UI on the host that uses a local Ollama model (no data leaves your
>   network)
> - All tools also work in Claude Desktop / Claude Code / any MCP client
> - Easy to extend — adding a new source (Confluence, Jira, Calendar, …) is a
>   few hundred lines from the per-source skeleton
> - MIT licensed, ~36 hours' build time, runs as systemd user services
>
> See `README.md` for the architecture, `SETUP.md` for replication steps,
> and `OPERATIONS.md` for known issues (including the OneNote 5,000-item
> SharePoint ceiling and other surprises). PRs welcome.
>
> Happy to chat through the design if anyone's thinking about something
> similar for their team.
>
> — Chris

---

## Tagline options (in case you want a one-liner)

- "Mirror your work data into a local SQLite cache, query it with a local LLM."
- "5 local MCP servers + 1 chat UI = your entire work history, searchable, private."
- "Local-first AI assistance for research-computing professionals who care
  about where their data lives."
- "Bring-your-own-LLM personal-knowledge stack with MCP."

---

## Hashtags / topics

For social: `#MCP`, `#ModelContextProtocol`, `#LocalLLM`, `#Ollama`,
`#ResearchComputing`, `#OpenSource`

For GitHub topics (Settings → About → gear): `mcp`,
`model-context-protocol`, `ollama`, `local-llm`, `personal-knowledge-management`,
`microsoft-graph`, `slack`, `clickup`, `sqlite`, `fts5`
