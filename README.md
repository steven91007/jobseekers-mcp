# jobseekers-mcp

**English** | [繁體中文](README.zh-TW.md)

The [MCP](https://modelcontextprotocol.io/) server for [Jobseekers](https://github.com/steven91007/Jobseekers-). It lets agents such as Claude Code call LinkedIn job search, visa-sponsorship checks, the git-history knowledge base (gitkb) and the Discord bot's status directly, and every call can be traced in [Langfuse](https://langfuse.com/).

This repository holds only the MCP layer. The scraper, visa rules, gitkb and bot database code live in the Jobseekers project, and the server imports them from there instead of keeping a copy, so a fix on either side is always shared.

## Tools

| Tool | What it does |
|---|---|
| `search_jobs` | Search LinkedIn jobs, newest first, with multiple locations, region presets and `posted_within` (24h / 7d / 30d). Returns an `outcome` so the agent can tell "no jobs" apart from "blocked / scraper broken" |
| `get_job_detail` | Read one job's full description, criteria and rule-based visa verdict |
| `check_visa` | Check up to 15 jobs for visa sponsorship. Jobs the rules cannot decide come back with their description so **the calling model judges them**, which is why the server needs no LLM key |
| `gitkb_search` / `gitkb_show` / `gitkb_log` / `gitkb_history` | Query the git knowledge base: check why the code is the way it is before changing it |
| `gitkb_pending` / `gitkb_import_summaries` | Let the agent write and import summaries for unsummarized commits |
| `list_subscriptions` / `bot_status` | The Discord bot's subscriptions and last push (read-only; the database is opened with `mode=ro`) |

There is also a `jobs://regions` resource (the region presets) and a `gitkb_update` prompt (the steps to update the knowledge base).

All LinkedIn tools share one throttle (`MCP_LINKEDIN_MIN_GAP`, 3 seconds by default). `search_jobs` returns at most 50 jobs and `check_visa` checks at most 15. When LinkedIn blocks a request, the tool returns an error that tells the agent not to retry.

## How it finds the Jobseekers project

It tries these in order and stops at the first directory that contains `linkedin_scraper.py`, `visa.py`, `gitkb/` and `bot/`:

1. The `JOBSEEKERS_ROOT` environment variable
2. The directory above this repository, which is Jobseekers itself when this repository is mounted there as a submodule
3. The current working directory and its parents (MCP clients usually start the server inside the project)

`--check` prints the path it uses.

## Usage

### Inside Jobseekers (submodule, recommended)

Jobseekers mounts this repository as a submodule at `jobseekers-mcp/` and registers the server in its `.mcp.json`:

```bash
git clone --recurse-submodules https://github.com/steven91007/Jobseekers-.git
# if you already cloned:
git submodule update --init
```

Open Claude Code in the Jobseekers directory and approve the `jobseekers` server when asked the first time. The command it runs is:

```bash
uv run --no-project --python 3.13 --with-editable ./jobseekers-mcp python -m mcp_server
```

### Standalone

```bash
git clone https://github.com/steven91007/jobseekers-mcp.git
cd jobseekers-mcp
uv venv --python 3.13 && uv pip install -e .
JOBSEEKERS_ROOT=/path/to/Jobseekers- .venv/bin/jobseekers-mcp --check
```

To register it in another MCP client, use `jobseekers-mcp` (or `python -m mcp_server`) as the command and pass `JOBSEEKERS_ROOT` in its environment.

## Settings

Settings are read from the Jobseekers project's `.env`; set `JOBSEEKERS_ENV_FILE` to use another file. All of them are optional:

| Variable | Meaning |
|---|---|
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` | Tracing turns on only when both keys are set; without them the server works as usual |
| `LANGFUSE_TRACING_ENVIRONMENT` | `production` (default) or `development`, to keep test runs out of your real dashboards |
| `JOBAGENT_USER_ID` | The user id on traces, `me` by default |
| `MCP_LANGFUSE_MASK` | Set to `0` to turn off masking of emails, phone numbers and keys (on by default) |
| `MCP_LINKEDIN_MIN_GAP` | Minimum seconds between two LinkedIn tool calls, 3 by default |
| `MCP_SESSION_ID` | Override the Langfuse session id (one per server process by default) |
| `GITKB_REPO` / `JOBBOT_DB` | The repository gitkb reads and the bot database path; both default to the Jobseekers project |

## Langfuse tracing

- **One tool call is one trace.** The root observation's input is the tool arguments and its output is the tool result. Names are stable (`search-linkedin-jobs`, `check-visa-sponsorship`, `search-git-history`, ...; the full list is `NAMES` in `mcp_server/observability.py`), so you can build dashboards and evaluators on them.
- Under `check_visa`, each job gets a `check-job-visa` observation, split into `fetch-job-description` (retriever) and `classify-visa-rules`.
- All traces from one server process share a session id and are tagged `mcp` plus `jobs`, `gitkb` or `bot`.
- If the MCP client sends a W3C `traceparent` in the request's `_meta`, the tool's trace attaches to the client's trace. The MCP SDK's own `tools/call` spans are not exported.
- Emails, phone numbers and API keys in job descriptions are masked before they are sent.
- The server flushes after every tool call and on SIGTERM. MCP clients often kill the server process when a session ends; without this, the last traces would be lost.

## Development

```bash
uv venv --python 3.13 && uv pip install -e ".[test]"
JOBSEEKERS_ROOT=../Jobseekers- .venv/bin/python -m pytest -q
```

The tests are fully offline: LinkedIn is faked, and Langfuse spans go to an in-memory exporter so the trace nesting, observation types, session and masking can be checked. gitkb runs its whole pending / import flow in a throwaway git repository. CI (`.github/workflows/ci.yml`) checks out Jobseekers' master and runs the same tests.

### Releasing and updating the version in Jobseekers

1. Make the change here, push to `main` and wait for CI to pass. To pin a version, tag it (for example `v1.1.0`) and update the version in `pyproject.toml` and `mcp_server/__init__.py`.
2. In Jobseekers, move the submodule to the new commit:

   ```bash
   git submodule update --remote jobseekers-mcp   # or: cd jobseekers-mcp && git checkout v1.1.0
   git add jobseekers-mcp && git commit -m "Bump jobseekers-mcp to v1.1.0"
   ```

If a change needs matching changes in Jobseekers' core modules, merge those in Jobseekers first, then update this repository.
