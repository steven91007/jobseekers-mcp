"""The Jobseekers MCP server: LinkedIn job search, visa checks, gitkb and bot status.

Every tool is a thin adapter: argument docs for the agent, one Langfuse root
observation per call, and translation of known failures into MCP tool errors the
agent can read. The work happens in :mod:`.jobs`, :mod:`.kb` and
:mod:`.subscriptions`, which in turn call the same modules the CLI and the
Discord bot use.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Callable

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from gitkb import db as kb_db, gitio
from gitkb.config import ConfigError
from gitkb.summarize import SummaryError

from . import jobs, kb, subscriptions
from . import observability as obs
from .observability import NAMES

INSTRUCTIONS = """\
Tools for a job hunt and for this repository's history.

Jobs: search_jobs finds LinkedIn postings (newest first); get_job_detail reads one
posting; check_visa tells which postings mention visa / work-permit sponsorship.
LinkedIn rate-limits aggressively, so search once with the right filters rather
than many narrow searches, and check visas only for the postings worth applying to.
When check_visa marks a posting unknown, it returns the description: read it and
judge sponsorship yourself.

Git history (gitkb): gitkb_search / gitkb_show / gitkb_log / gitkb_history explain
why code changed. gitkb_pending + gitkb_import_summaries add summaries for new
commits (the gitkb_update prompt walks through it).

Discord bot: list_subscriptions and bot_status are read-only.
"""

READ_ONLY_WEB = ToolAnnotations(read_only_hint=True, open_world_hint=True)
READ_ONLY_LOCAL = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES_LOCAL = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

# Failures the agent can act on; anything else surfaces as an unexpected tool error.
EXPECTED_ERRORS = (
    jobs.ToolInputError,
    jobs.LinkedInUnavailable,
    kb.KbError,
    kb_db.DbError,
    gitio.GitError,
    ConfigError,
    SummaryError,
    subscriptions.BotDbMissing,
)

mcp = MCPServer(name="jobseekers", instructions=INSTRUCTIONS, version="1.0.0")


def _meta(ctx: Context | None) -> Any:
    try:
        return ctx.request_context.meta if ctx is not None else None
    except Exception:
        return None


def _traced(
    ctx: Context | None,
    *,
    name: str,
    as_type: str,
    tool: str,
    feature: str,
    input: dict,
    fn: Callable[[Any], dict],
) -> dict[str, Any]:
    try:
        with obs.tool_call(name, as_type=as_type, input=input, tool=tool, feature=feature,
                           meta=_meta(ctx)) as root:
            try:
                out = fn(root)
            except EXPECTED_ERRORS as e:
                root.update(output={"error": str(e)})
                raise ToolError(str(e)) from e
            root.update(output=out)
            return out
    finally:
        # MCP clients may kill the server process without warning when a session ends,
        # so nothing may wait in the export buffer after a call returns.
        obs.flush()


# --- jobs ------------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY_WEB)
def search_jobs(
    keyword: Annotated[str, Field(description='Job title or skills, e.g. "LLM Engineer".')],
    location: Annotated[str, Field(description=(
        'One place or a comma-separated list ("Berlin, Amsterdam, Dublin"). Region presets '
        "expand to their countries: Nordics/北歐, DACH/德語區, Benelux/荷比盧, Baltics/波羅的海. "
        "Empty searches everywhere."))] = "",
    max_results: Annotated[int, Field(ge=1, le=jobs.MAX_RESULTS_CAP,
                                      description="Total postings across all locations.")] = 10,
    work_type: Annotated[str, Field(description='"onsite", "remote", "hybrid", or empty for any.')] = "",
    job_type: Annotated[str, Field(description=(
        '"fulltime", "parttime", "contract", "temporary", "internship", "volunteer", '
        "or empty for any."))] = "",
    english_only: Annotated[bool, Field(
        description="Drop postings whose title or company is not in Latin script.")] = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Search LinkedIn job postings, newest first.

    Returns outcome (OK, EMPTY_OK = no postings match, EMPTY_SUSPICIOUS / PARSE_DRIFT =
    the scraper may be broken), the locations searched and the jobs with job_id,
    title, company, location, posted_date and url.
    """
    args = dict(keyword=keyword, location=location, max_results=max_results,
                work_type=work_type, job_type=job_type, english_only=english_only)
    return _traced(ctx, name=NAMES.SEARCH_JOBS, as_type="retriever", tool="search_jobs",
                   feature="jobs", input=args, fn=lambda root: jobs.search(**args, root=root))


@mcp.tool(annotations=READ_ONLY_WEB)
def get_job_detail(
    job_id: Annotated[str, Field(description="Numeric LinkedIn job id from search_jobs, or a LinkedIn job URL.")],
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Read one LinkedIn posting: full description, criteria (seniority, employment
    type, ...) and the rule-based visa verdict."""
    return _traced(ctx, name=NAMES.JOB_DETAIL, as_type="retriever", tool="get_job_detail",
                   feature="jobs", input={"job_id": job_id},
                   fn=lambda root: jobs.job_detail(job_id, root=root))


@mcp.tool(annotations=READ_ONLY_WEB)
def check_visa(
    job_ids: Annotated[list[str], Field(min_length=1, max_length=jobs.MAX_VISA_CHECKS,
                                        description="LinkedIn job ids (or job URLs) from search_jobs.")],
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Check which postings mention visa / work-permit sponsorship or relocation help.

    Reads each posting (one LinkedIn request per job, so at most 15 per call) and
    applies multilingual rules (English, German, Nordic languages). Each result has
    status supported / not_supported / unknown / unchecked and the sentence it was
    based on. For unknown ones the description is included so you can judge them."""
    return _traced(ctx, name=NAMES.CHECK_VISA, as_type="chain", tool="check_visa",
                   feature="jobs", input={"job_ids": job_ids},
                   fn=lambda root: jobs.check_visa(job_ids, root=root))


@mcp.resource("jobs://regions", name="region-presets", mime_type="application/json",
              description="Region presets accepted in search_jobs locations and the countries they expand to.")
def region_presets() -> str:
    return json.dumps(jobs.regions(), ensure_ascii=False, indent=2)


# --- gitkb -----------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY_LOCAL)
def gitkb_search(
    query: Annotated[str, Field(description='SQLite FTS5 query, e.g. visa, "rate limit", scraper AND retry.')],
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum hits.")] = 20,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Full-text search over the commit and per-file change summaries of this repo."""
    return _traced(ctx, name=NAMES.KB_SEARCH, as_type="retriever", tool="gitkb_search",
                   feature="gitkb", input={"query": query, "limit": limit},
                   fn=lambda root: kb.search(query, limit))


@mcp.tool(annotations=READ_ONLY_LOCAL)
def gitkb_show(
    id: Annotated[str, Field(description=(
        "A git sha or prefix (commit note), or a 64-char note sha256 from gitkb_search."))],
    ctx: Context | None = None,
) -> dict[str, Any]:
    """The full knowledge note (Markdown) for a commit or a single file change."""
    return _traced(ctx, name=NAMES.KB_SHOW, as_type="retriever", tool="gitkb_show",
                   feature="gitkb", input={"id": id}, fn=lambda root: kb.show(id))


@mcp.tool(annotations=READ_ONLY_LOCAL)
def gitkb_log(
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum commits.")] = 30,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Summarized commits, newest first, with a one-line summary and tags each."""
    return _traced(ctx, name=NAMES.KB_LOG, as_type="retriever", tool="gitkb_log",
                   feature="gitkb", input={"limit": limit}, fn=lambda root: kb.log(limit))


@mcp.tool(annotations=READ_ONLY_LOCAL)
def gitkb_history(
    path: Annotated[str, Field(description='Repository-relative file path, e.g. "linkedin_scraper.py".')],
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Every summarized change to one file, oldest first: what changed and why."""
    return _traced(ctx, name=NAMES.KB_HISTORY, as_type="retriever", tool="gitkb_history",
                   feature="gitkb", input={"path": path}, fn=lambda root: kb.history(path))


@mcp.tool(annotations=READ_ONLY_LOCAL)
def gitkb_pending(
    limit: Annotated[int, Field(ge=1, le=20, description=(
        "Commits to return in this call. Diffs are large; keep it small and call again "
        "after importing. total_pending says how many remain."))] = 3,
    include_placeholders: Annotated[bool, Field(
        description="Also return commits that only have dry-run placeholder notes.")] = True,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Commits that still need a summary, with their canonical diff (`input`), the
    files each summary must cover, a field guide and the answer format."""
    return _traced(ctx, name=NAMES.KB_PENDING, as_type="retriever", tool="gitkb_pending",
                   feature="gitkb", input={"limit": limit, "include_placeholders": include_placeholders},
                   fn=lambda root: kb.pending(limit, include_placeholders, root=root))


@mcp.tool(annotations=WRITES_LOCAL)
def gitkb_import_summaries(
    summaries: Annotated[dict, Field(description=(
        '{"model": "claude-code/<model id>", "commits": {<full git sha>: {"summary", "why", '
        '"risks", "tags", "files": [{"path", "summary", "kind", "notable_symbols"}]}}} as in '
        "gitkb_pending's answer_format. Every file listed for a commit needs exactly one "
        "files[] entry. Write in English."))],
    allow_recipe_change: Annotated[bool, Field(description=(
        "Only after the user agreed: import even though the hash recipe changed since "
        "the index was built."))] = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Write knowledge notes for commits from your summaries and index them."""
    commits = summaries.get("commits") if isinstance(summaries, dict) else None
    shas = sorted(commits) if isinstance(commits, dict) else []
    return _traced(ctx, name=NAMES.KB_IMPORT, as_type="tool", tool="gitkb_import_summaries",
                   feature="gitkb", input={"commits": shas, "model": (summaries or {}).get("model")},
                   fn=lambda root: kb.import_summaries(summaries, allow_recipe_change, root=root))


@mcp.prompt(name="gitkb_update", description="Summarize every unsummarized commit into the gitkb knowledge base.")
def gitkb_update() -> str:
    return """\
Update the git knowledge base for every commit that does not yet have a real summary.
You write the summaries; no API call is involved.

1. Call gitkb_pending (limit 3). If it returns no commits, say so and stop.
2. For each commit, read the `input` field (canonical header + unified diff) carefully
   rather than paraphrasing the commit message. Follow `field_guide`. Cover every path
   in `files` with exactly one files[] entry. Summaries are in English.
3. Call gitkb_import_summaries with {"model": "claude-code/<your model id>", "commits": {...}}
   keyed by full git sha. Fix and retry if it reports failures.
4. Repeat from step 1 while total_pending is larger than what you just imported.
5. Finish with gitkb_log (limit 10).

If gitkb_import_summaries says the hash recipe changed, stop and ask the user.
Never edit files under knowledge/commits/ or knowledge/changes/ by hand."""


# --- Discord bot -----------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY_LOCAL)
def list_subscriptions(
    active_only: Annotated[bool, Field(description="Leave out paused subscriptions.")] = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """The Discord bot's saved job searches (keyword, location, filters) and how their
    last runs went. Read-only; manage subscriptions with the bot's /jobs commands."""
    return _traced(ctx, name=NAMES.SUBS_LIST, as_type="retriever", tool="list_subscriptions",
                   feature="bot", input={"active_only": active_only},
                   fn=lambda root: subscriptions.list_subscriptions(active_only))


@mcp.tool(annotations=READ_ONLY_LOCAL)
def bot_status(ctx: Context | None = None) -> dict[str, Any]:
    """When the Discord bot last pushed jobs, how many subscriptions are active or
    failing, and how many postings it has already pushed."""
    return _traced(ctx, name=NAMES.BOT_STATUS, as_type="retriever", tool="bot_status",
                   feature="bot", input={}, fn=lambda root: subscriptions.bot_status())
