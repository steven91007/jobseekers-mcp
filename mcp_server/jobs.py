"""LinkedIn search and visa checks exposed as MCP tools.

The server does no LLM work of its own. The visa check runs only the offline
rules from :mod:`visa`; for postings they leave ``unknown`` it hands back the
description so the calling agent can read it and judge for itself. That keeps
the server keyless (no ANTHROPIC_API_KEY) and puts the judgement in the model
that already holds the conversation.

Agents call tools in loops far faster than a person would, so every LinkedIn
request here goes through one process-wide throttle, and result sizes are capped.
"""

from __future__ import annotations

import os
import threading
import time

import requests

import linkedin_scraper as ls
import visa
from visa import VisaStatus

from . import observability as obs
from .observability import NAMES

MAX_RESULTS_CAP = 50
MAX_VISA_CHECKS = 15
DESCRIPTION_CHARS = 8000
SEARCH_TIMEOUT = 120.0
VISA_TIMEOUT = 180.0


class ToolInputError(ValueError):
    """Bad arguments; reported to the agent as a tool error it can correct."""


class LinkedInUnavailable(RuntimeError):
    """LinkedIn blocked, rate-limited or could not be reached."""


# --- throttle --------------------------------------------------------------------

_lock = threading.Lock()
_last_call = 0.0


def _min_gap() -> float:
    try:
        return max(0.0, float(os.getenv("MCP_LINKEDIN_MIN_GAP", "3")))
    except ValueError:
        return 3.0


def _throttle() -> float:
    """Block until the minimum gap since the previous LinkedIn tool call has passed."""
    global _last_call
    with _lock:
        wait = _last_call + _min_gap() - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        return max(wait, 0.0)


# --- tools -----------------------------------------------------------------------


def search(
    keyword: str,
    location: str = "",
    max_results: int = 10,
    work_type: str = "",
    job_type: str = "",
    english_only: bool = False,
    posted_within: str = "",
    root=obs.NOOP,
) -> dict:
    keyword = keyword.strip()
    if not keyword:
        raise ToolInputError("keyword must not be empty")
    work_type = work_type.strip().lower()
    job_type = job_type.strip().lower()
    if work_type and work_type not in ls.WORK_TYPE_MAP:
        raise ToolInputError(f"work_type must be one of {sorted(ls.WORK_TYPE_MAP)} or empty")
    if job_type and job_type not in ls.JOB_TYPE_MAP:
        raise ToolInputError(f"job_type must be one of {sorted(ls.JOB_TYPE_MAP)} or empty")
    posted_within = posted_within.strip().lower()
    if posted_within and posted_within not in ls.POSTED_WITHIN_MAP:
        raise ToolInputError(f"posted_within must be one of {sorted(ls.POSTED_WITHIN_MAP)} or empty")
    max_results = max(1, min(int(max_results), MAX_RESULTS_CAP))
    locations = ls._parse_locations(location)

    waited = _throttle()
    try:
        result = ls.search_jobs_strict(
            keyword=keyword,
            location=location,
            max_results=max_results,
            work_type=work_type,
            job_type=job_type,
            english_only=english_only,
            posted_within=posted_within,
            timeout=SEARCH_TIMEOUT,
        )
    except ls.ScraperError as e:
        root.update(metadata={"outcome": e.outcome.value, "locations": locations})
        raise LinkedInUnavailable(_failure_advice(e.outcome, str(e))) from e

    out = {
        "outcome": result.outcome.value,
        "count": len(result.jobs),
        "searched_locations": [loc or "(anywhere)" for loc in locations],
        "jobs": result.jobs,
    }
    if result.detail:
        out["note"] = result.detail
    if result.outcome is ls.Outcome.PARSE_DRIFT:
        out["note"] = "LinkedIn returned cards the parser could not read; the scraper selectors need updating."
    root.update(metadata={
        "outcome": result.outcome.value,
        "locations": locations,
        "pages_fetched": result.pages_fetched,
        "raw_card_count": result.raw_card_count,
        "throttle_wait_s": round(waited, 2),
    })
    return out


def job_detail(job_id: str, root=obs.NOOP) -> dict:
    job_id = _job_id(job_id)
    _throttle()
    detail = ls.get_job_detail(job_id)
    if detail.get("error"):
        raise LinkedInUnavailable(detail["error"])
    description = detail.get("description", "")
    with obs.span(NAMES.CLASSIFY_VISA, input={"job_id": job_id}) as s:
        verdict = visa.classify_visa(description)
        s.update(output=_verdict_dict(verdict))
    root.update(metadata={"description_chars": len(description)})
    return {
        "job_id": job_id,
        "url": ls._build_job_url(job_id),
        "criteria": detail.get("criteria", {}),
        "visa": _verdict_dict(verdict),
        "description": description,
    }


def check_visa(job_ids: list[str], root=obs.NOOP) -> dict:
    ids = []
    for raw in job_ids:
        jid = _job_id(raw)
        if jid not in ids:
            ids.append(jid)
    if not ids:
        raise ToolInputError("job_ids must contain at least one LinkedIn job id")
    if len(ids) > MAX_VISA_CHECKS:
        raise ToolInputError(
            f"at most {MAX_VISA_CHECKS} jobs per call (each one is a LinkedIn request); got {len(ids)}"
        )

    _throttle()
    deadline = time.monotonic() + VISA_TIMEOUT
    session = requests.Session()
    results = []
    try:
        for i, jid in enumerate(ids):
            if ls._remaining(deadline) <= 0 or (i and not ls._pause(visa.DETAIL_PAUSE, deadline)):
                results.append({"job_id": jid, "status": VisaStatus.UNCHECKED.value,
                                "note": "time budget exhausted"})
                continue
            results.append(_check_one(jid, session, deadline))
    finally:
        session.close()

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    needs_review = [r["job_id"] for r in results if r["status"] == VisaStatus.UNKNOWN.value]
    root.update(metadata={"job_count": len(ids), "counts": counts})
    out = {"counts": counts, "results": results}
    if needs_review:
        out["needs_review"] = needs_review
        out["how_to_review"] = (
            "The offline rules found no sponsorship statement in these postings. Their "
            "descriptions are included; read them and decide supported / not_supported / "
            "unknown yourself. 'unknown' only means the posting does not say."
        )
    return out


def regions() -> dict:
    aliases: dict[str, list[str]] = {}
    for alias, key in ls.REGION_ALIASES.items():
        aliases.setdefault(key, []).append(alias)
    return {
        key: {"countries": list(countries), "aliases": aliases.get(key, [])}
        for key, countries in ls.REGION_PRESETS.items()
    }


# --- helpers ---------------------------------------------------------------------


def _check_one(job_id: str, session: requests.Session, deadline: float) -> dict:
    with obs.span(NAMES.CHECK_JOB_VISA, as_type="chain", input={"job_id": job_id},
                  metadata={"job_id": job_id}) as step:
        with obs.span(NAMES.FETCH_DESCRIPTION, as_type="retriever", input={"job_id": job_id}) as f:
            detail = ls.get_job_detail(job_id, session=session, deadline=deadline)
            description = "" if detail.get("error") else detail.get("description", "")
            f.update(output={"chars": len(description), "error": detail.get("error")})
        if not description or description == "No description found.":
            result = {"job_id": job_id, "status": VisaStatus.UNCHECKED.value,
                      "note": detail.get("error") or "no description found"}
            step.update(output=result, level="WARNING")
            return result
        with obs.span(NAMES.CLASSIFY_VISA, input=description) as c:
            verdict = visa.classify_visa(description)
            c.update(output=_verdict_dict(verdict))
        result = {"job_id": job_id, **_verdict_dict(verdict)}
        if verdict.status is VisaStatus.UNKNOWN:
            result["description"] = description[:DESCRIPTION_CHARS]
            if len(description) > DESCRIPTION_CHARS:
                result["description_truncated"] = True
        step.update(output={k: v for k, v in result.items() if k != "description"})
        return result


def _verdict_dict(v: visa.VisaVerdict) -> dict:
    return {"status": v.status.value, "evidence": v.evidence, "source": v.source,
            "confidence": v.confidence}


def _job_id(raw) -> str:
    text = str(raw).strip()
    if not text.isdigit():
        text = ls._extract_job_id_from_href(text)  # accept a full LinkedIn job URL too
    if not text.isdigit():
        raise ToolInputError(f"not a LinkedIn job id or job URL: {raw!r}")
    return text


def _failure_advice(outcome: ls.Outcome, detail: str) -> str:
    advice = {
        ls.Outcome.BLOCKED: "LinkedIn blocked this IP. Do not retry for a few hours; retries turn this into a longer ban.",
        ls.Outcome.RATE_LIMITED: "LinkedIn is rate limiting. Wait several minutes before searching again.",
    }.get(outcome, "LinkedIn could not be reached. Retrying later may work.")
    return f"{outcome.value}: {advice} ({detail})"
