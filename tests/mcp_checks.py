"""Offline checks for the MCP server: tool logic, the MCP protocol surface, gitkb
round-trip, the bot's read-only view, and the Langfuse trace shape.

LinkedIn is faked; Langfuse spans go to an in-memory exporter. Needs Python 3.10+
with requirements.txt installed (the mcp SDK): .venv/bin/python tests/mcp_checks.py
(pytest runs it in a subprocess through tests/test_mcp.py)
"""
import asyncio, json, os, pathlib, shutil, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["MCP_LINKEDIN_MIN_GAP"] = "0"
for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
    os.environ.pop(key, None)

from mcp import Client

import linkedin_scraper as ls
import visa
from bot import db as bot_db
from mcp_server import jobs, kb, subscriptions
from mcp_server import observability as obs
from mcp_server.server import mcp
from test_gitkb import make_repo

ok = True


def check(label, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {label} {extra}")


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


# --- fake LinkedIn ------------------------------------------------------------------

JOBS = [
    {"job_id": "4100000001", "title": "LLM Engineer", "company": "Acme", "location": "Berlin",
     "work_type": "Hybrid", "posted_date": "2026-09-27", "url": ls._build_job_url("4100000001")},
    {"job_id": "4100000002", "title": "ML Engineer", "company": "Beta", "location": "Dublin",
     "work_type": "N/A", "posted_date": "2026-09-26", "url": ls._build_job_url("4100000002")},
]
DESCRIPTIONS = {
    "4100000001": "Great team. We offer visa sponsorship and a relocation package.",
    "4100000002": "Build models. Questions? Mail jane.doe@example.com or call +49 30 1234 5678.",
    "4100000003": "Applicants must already have the right to work in Ireland.",
}
calls = {"search": [], "detail": []}
search_mode = {"raise": None}


def fake_search(**kw):
    calls["search"].append(kw)
    if search_mode["raise"]:
        raise search_mode["raise"]
    return ls.ScrapeResult(jobs=[dict(j) for j in JOBS], outcome=ls.Outcome.OK, raw_card_count=2,
                           parsed_count=2, pages_fetched=1)


def fake_detail(job_id, session=None, deadline=None):
    calls["detail"].append(job_id)
    if job_id not in DESCRIPTIONS:
        return {"error": f"HTTP 404 for {job_id}"}
    return {"description": DESCRIPTIONS[job_id], "criteria": {"Seniority level": "Mid-Senior level"}}


ls.search_jobs_strict = fake_search
ls.get_job_detail = fake_detail
visa.DETAIL_PAUSE = 0


print("\n[1] observability helpers")
tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
check("traceparent from a dict", obs.parent_from_meta({"traceparent": tp}) ==
      {"trace_id": "0af7651916cd43dd8448eb211c80319c", "parent_span_id": "b7ad6b7169203331"})
check("no meta -> None", obs.parent_from_meta(None) is None)
check("malformed -> None", obs.parent_from_meta({"traceparent": "00-xyz-01"}) is None)
check("all-zero trace id -> None",
      obs.parent_from_meta({"traceparent": "00-" + "0" * 32 + "-b7ad6b7169203331-01"}) is None)
masked = obs.mask_text("mail jane.doe@example.com, call +49 30 1234 5678, key sk-lf-abcdefghijklmnop1234, job 4100000001")
check("mask email/phone/key, keep job ids",
      "@" not in masked and "+49" not in masked and "sk-lf" not in masked and "4100000001" in masked, masked)
check("tracing is off without keys", not obs.enabled() and obs.init() is False)


print("\n[2] job tools (fake LinkedIn)")
out = jobs.search("LLM Engineer", location="Berlin, 北歐", max_results=500)
check("search returns jobs and outcome", out["outcome"] == "OK" and out["count"] == 2)
check("max_results capped at 50", calls["search"][-1]["max_results"] == jobs.MAX_RESULTS_CAP)
check("region preset expanded in searched_locations",
      out["searched_locations"] == ["Berlin", "Denmark", "Sweden", "Norway", "Finland", "Iceland"])
check("empty keyword rejected", raises(lambda: jobs.search("  "), jobs.ToolInputError))
check("bad work_type rejected", raises(lambda: jobs.search("x", work_type="moon"), jobs.ToolInputError))
search_mode["raise"] = ls.Blocked("HTTP 999")
try:
    jobs.search("x")
    check("blocked -> LinkedInUnavailable", False)
except jobs.LinkedInUnavailable as e:
    check("blocked -> LinkedInUnavailable with advice", "BLOCKED" in str(e) and "Do not retry" in str(e))
search_mode["raise"] = None

d = jobs.job_detail("https://www.linkedin.com/jobs/view/llm-engineer-at-acme-4100000001/")
check("job URL accepted, detail has visa verdict", d["job_id"] == "4100000001" and d["visa"]["status"] == "supported")
check("non-numeric id rejected", raises(lambda: jobs.job_detail("abc"), jobs.ToolInputError))

v = jobs.check_visa(["4100000001", "4100000002", "4100000003", "4100000001", "4199999999"])
by_id = {r["job_id"]: r for r in v["results"]}
check("duplicates dropped", len(v["results"]) == 4)
check("supported / not_supported / unknown / unchecked",
      [by_id[j]["status"] for j in ("4100000001", "4100000003", "4100000002", "4199999999")]
      == ["supported", "not_supported", "unknown", "unchecked"])
check("unknown carries the description for the agent",
      "description" in by_id["4100000002"] and v["needs_review"] == ["4100000002"])
check("decided ones do not carry the description", "description" not in by_id["4100000001"])
check("too many ids rejected",
      raises(lambda: jobs.check_visa([str(4100000000 + i) for i in range(16)]), jobs.ToolInputError))
check("regions list presets with aliases", "北歐" in jobs.regions()["nordics"]["aliases"])


async def protocol_checks():
    async with Client(mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        check("all tools registered", names == {
            "search_jobs", "get_job_detail", "check_visa", "gitkb_search", "gitkb_show",
            "gitkb_log", "gitkb_history", "gitkb_pending", "gitkb_import_summaries",
            "list_subscriptions", "bot_status"}, sorted(names))
        r = await client.call_tool("search_jobs", {"keyword": "LLM Engineer", "location": "Dublin"})
        check("search_jobs over MCP returns structured jobs",
              not r.is_error and r.structured_content["count"] == 2)
        r = await client.call_tool("search_jobs", {"keyword": "x", "work_type": "moon"})
        check("bad argument -> is_error with a readable message",
              r.is_error and "work_type must be one of" in r.content[0].text)
        r = await client.call_tool("check_visa", {"job_ids": []})
        check("schema rejects an empty job_ids list", r.is_error)
        res = await client.read_resource("jobs://regions")
        check("regions resource", "nordics" in json.loads(res.contents[0].text))
        p = await client.get_prompt("gitkb_update")
        check("gitkb_update prompt", "gitkb_import_summaries" in p.messages[0].content.text)

print("\n[3] MCP protocol (in-process client)")
asyncio.run(protocol_checks())


print("\n[4] gitkb tools against a throwaway repo")
tmp = pathlib.Path(tempfile.mkdtemp(prefix="mcp-test-")).resolve()
try:
    repo = tmp / "repo"; repo.mkdir(); make_repo(repo)
    os.environ["GITKB_REPO"] = str(repo)
    os.environ["GITKB_KNOWLEDGE_DIR"] = "knowledge"

    p = kb.pending(limit=2)
    check("pending pages with total_pending", len(p["commits"]) == 2 and p["total_pending"] == 3)
    p = kb.pending(limit=20)
    answers = {"model": "test/offline", "commits": {}}
    for c in p["commits"]:
        answers["commits"][c["git_sha"]] = {
            "summary": f"Summary of {c['subject']}.", "why": "Because tests.", "risks": "None.",
            "tags": ["test"],
            "files": [{"path": f["path"], "summary": f"Touches {f['path']}.", "kind": "feature",
                       "notable_symbols": []} for f in c["files"]],
        }
    r = kb.import_summaries(answers)
    check("import writes and indexes every commit", r["summarized"] == 3 and not r["failed"], r)
    check("nothing pending afterwards", kb.pending()["total_pending"] == 0)
    log = kb.log()
    check("log lists commits newest first", [c["subject"] for c in log["commits"]][0] == "third commit")
    check("search finds a summary", kb.search("tests")["count"] >= 1)
    first = log["commits"][-1]["git_sha"]
    check("show by sha prefix returns markdown", "Summary of first commit" in kb.show(first[:8])["markdown"])
    check("history of a.py", len(kb.history("a.py")["changes"]) == 3)
    check("bad FTS query -> KbError", raises(lambda: kb.search('"unterminated'), kb.KbError))
    check("unknown commit -> KbError", raises(lambda: kb.import_summaries(
        {"commits": {"f" * 40: {"summary": "x", "files": []}}}), kb.KbError))

    (repo / "knowledge" / "index.db").unlink()
    check("missing index is rebuilt from the notes", kb.log()["commits"] and kb.search("tests")["count"] >= 1)
finally:
    os.environ.pop("GITKB_REPO", None)
    shutil.rmtree(tmp, ignore_errors=True)


print("\n[5] Discord bot view (read-only)")
tmp = pathlib.Path(tempfile.mkdtemp(prefix="mcp-bot-")).resolve()
try:
    os.environ["JOBBOT_DB"] = str(tmp / "missing.db")
    check("missing bot DB -> BotDbMissing, not created",
          raises(subscriptions.bot_status, subscriptions.BotDbMissing) and not (tmp / "missing.db").exists())
    os.environ["JOBBOT_DB"] = str(tmp / "jobs.db")
    conn = bot_db.connect(tmp / "jobs.db")
    bot_db.add_subscription(conn, guild_id=1, channel_id=2, creator_id=3, keyword="LLM Engineer",
                            location="Dublin", work_type="", job_type="", english_only=False,
                            visa_check=True, max_results=10)
    conn.close()
    subs = subscriptions.list_subscriptions()
    check("lists subscriptions with a description",
          subs["count"] == 1 and "LLM Engineer" in subs["subscriptions"][0]["description"])
    st = subscriptions.bot_status()
    check("bot status counts", st["subscriptions"]["active"] == 1 and st["last_run"] is None)
finally:
    os.environ.pop("JOBBOT_DB", None)
    shutil.rmtree(tmp, ignore_errors=True)


print("\n[6] Langfuse trace shape (in-memory exporter)")
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-test"
os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-test"
os.environ["LANGFUSE_BASE_URL"] = "http://localhost:9"  # never contacted: spans go to the exporter
exporter = InMemorySpanExporter()
check("tracing starts with keys", obs.init(span_exporter=exporter, flush_at=1))


async def traced_calls():
    async with Client(mcp) as client:
        await client.call_tool("check_visa", {"job_ids": ["4100000001", "4100000002"]},
                               meta={"traceparent": tp})
        await client.call_tool("search_jobs", {"keyword": "LLM Engineer"})
        await client.call_tool("search_jobs", {"keyword": "x", "work_type": "moon"})

asyncio.run(traced_calls())
obs.flush()
spans = exporter.get_finished_spans()
by_name = {}
for s in spans:
    by_name.setdefault(s.name, []).append(s)
check("SDK server spans are not exported", not any(s.name.startswith("tools/") for s in spans),
      sorted(by_name))
root = by_name.get(obs.NAMES.CHECK_VISA, [None])[0]
check("check_visa root joins the client's trace",
      root is not None and format(root.context.trace_id, "032x") == "0af7651916cd43dd8448eb211c80319c"
      and format(root.parent.span_id, "016x") == "b7ad6b7169203331")
steps = by_name.get(obs.NAMES.CHECK_JOB_VISA, [])
check("one check-job-visa per job, under the root",
      len(steps) == 2 and all(s.parent.span_id == root.context.span_id for s in steps))
fetches = by_name.get(obs.NAMES.FETCH_DESCRIPTION, [])
check("fetch + classify nested under each job step",
      len(fetches) == 2 and {f.parent.span_id for f in fetches} == {s.context.span_id for s in steps}
      and len(by_name.get(obs.NAMES.CLASSIFY_VISA, [])) == 2)
check("observation types set", root.attributes.get("langfuse.observation.type") == "chain"
      and fetches[0].attributes.get("langfuse.observation.type") == "retriever")
check("session and user propagated",
      all(s.attributes.get("session.id") == obs.SESSION_ID and s.attributes.get("user.id") == obs.USER_ID
          for s in spans), {k: v for k, v in spans[0].attributes.items() if "session" in k or "user" in k})
searches = by_name.get(obs.NAMES.SEARCH_JOBS, [])
check("search without traceparent starts its own trace",
      len(searches) == 2 and searches[0].parent is None
      and searches[0].context.trace_id != root.context.trace_id)
check("failed call recorded as ERROR", any(s.attributes.get("langfuse.observation.level") == "ERROR"
                                           for s in searches))
dumped = json.dumps([dict(s.attributes) for s in spans], default=str)
check("recruiter email and phone masked in exported data",
      "jane.doe@example.com" not in dumped and "1234 5678" not in dumped and "[REDACTED EMAIL]" in dumped)
obs.shutdown()


print("\nmcp tests:", "OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
