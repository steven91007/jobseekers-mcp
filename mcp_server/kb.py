"""gitkb (the git-history knowledge base) exposed as MCP tools.

The read tools answer "why is this code the way it is" before an agent edits it.
``pending`` and ``import_summaries`` replace the file shuffle of the /gitkb slash
command: the agent gets the canonical inputs as a tool result and hands its
summaries back as a tool argument, with no pending.json / summaries.json on disk.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from gitkb import build as build_mod
from gitkb import db, gitio, hashing, notes
from gitkb.config import Config, load
from gitkb.summarize import MappingSummarizer, parse_summaries

from . import observability as obs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HEX64 = 64


class KbError(RuntimeError):
    """Reported to the agent as a tool error."""


def _config() -> tuple[Config, Path]:
    start = Path(os.getenv("GITKB_REPO", "").strip() or PROJECT_ROOT)
    root = gitio.repo_root(start)
    return load(root), root


@contextmanager
def _open() -> Iterator[tuple[Config, Path, sqlite3.Connection]]:
    """Config, repo root and an index connection; rebuilds a missing index from the notes.

    index.db is gitignored, so a fresh clone has notes but no index.
    """
    cfg, root = _config()
    conn = db.connect(cfg.db_path)
    try:
        if db.counts(conn)["commits"] == 0 and any((cfg.knowledge_dir / "commits").glob("*.md")):
            build_mod.index_from_notes(conn, cfg.knowledge_dir)
        yield cfg, root, conn
    finally:
        conn.close()


def search(query: str, limit: int = 20) -> dict:
    with _open() as (_cfg, _root, conn):
        try:
            hits = db.search(conn, query, max(1, min(int(limit), 100)))
        except db.DbError as e:
            raise KbError(f"{e}. The query uses SQLite FTS5 syntax; quote phrases with double quotes.") from None
    return {"count": len(hits), "hits": [asdict(h) for h in hits]}


def show(ident: str) -> dict:
    ident = ident.strip().lower()
    with _open() as (cfg, _root, conn):
        if len(ident) == HEX64:
            if db.get_commit_by_note(conn, ident):
                kind = "commit"
            elif db.get_change(conn, ident):
                kind = "change"
            else:
                raise KbError(f"no note with sha256 {ident}")
            path = notes.note_path(cfg.knowledge_dir, kind, ident)
        else:
            row = db.get_commit(conn, ident)
            if row is None:
                raise KbError(f"no indexed commit matches {ident!r} (run gitkb_pending to see unsummarized commits)")
            kind, path = "commit", notes.note_path(cfg.knowledge_dir, "commit", row.note_sha256)
    return {"kind": kind, "note_path": str(path.relative_to(cfg.repo_root)), "markdown": path.read_text("utf-8")}


def log(limit: int = 30) -> dict:
    with _open() as (_cfg, _root, conn):
        rows = db.list_commits(conn, max(1, min(int(limit), 500)))
    return {
        "commits": [
            {
                "git_sha": r.git_sha,
                "short": r.git_sha[:7],
                "authored_at": r.authored_at,
                "subject": r.subject,
                "summary": r.summary_short,
                "tags": r.tags,
                "file_count": r.file_count,
                "note_sha256": r.note_sha256,
                "placeholder": r.model == "dry-run",
            }
            for r in rows
        ]
    }


def history(path: str) -> dict:
    with _open() as (_cfg, _root, conn):
        rows = db.changes_for_path(conn, path.strip())
    return {
        "path": path,
        "changes": [
            {
                "git_sha": ch.git_sha,
                "short": ch.git_sha[:7],
                "status": ch.status,
                "kind": ch.kind,
                "summary": ch.summary,
                "notable_symbols": ch.notable_symbols,
                "note_sha256": ch.note_sha256,
            }
            for ch in rows
        ],
    }


def pending(limit: int = 3, include_placeholders: bool = True, root=obs.NOOP) -> dict:
    with _open() as (cfg, repo, conn):
        data = build_mod.pending(repo, cfg, conn, include_placeholders=include_placeholders)
    # Diffs are large; hand back a few commits per call so the result fits the agent's context.
    remaining = len(data["commits"])
    data["commits"] = data["commits"][: max(1, min(int(limit), 20))]
    data["total_pending"] = remaining
    data["next_step"] = (
        "Write one summary per commit in answer_format and pass the whole object to "
        "gitkb_import_summaries. Call gitkb_pending again afterwards if total_pending "
        "was larger than the number of commits returned."
    )
    root.update(metadata={"returned": len(data["commits"]), "total_pending": remaining})
    return data


def import_summaries(summaries: dict, allow_recipe_change: bool = False, root=obs.NOOP) -> dict:
    try:
        analyses, file_model = parse_summaries(summaries, "summaries")
    except Exception as e:
        raise KbError(str(e)) from None
    if not analyses:
        raise KbError("summaries.commits is empty")
    model = file_model or "claude-code"

    with _open() as (cfg, repo, conn):
        known = db.get_meta(conn, "hash_recipe")
        recipe = hashing.recipe_string(cfg)
        if known and known != recipe and not allow_recipe_change:
            raise KbError(
                "the gitkb hash recipe changed since the index was built, so new notes will not "
                f"share hashes with old ones (old: {known}; new: {recipe}). Ask the user before "
                "retrying with allow_recipe_change=true."
            )
        unknown = sorted(set(analyses) - set(gitio.rev_list(repo, "HEAD")))
        if unknown:
            raise KbError(f"not commits on HEAD: {', '.join(s[:7] for s in unknown)}")
        stats = build_mod.build(
            repo, cfg, conn, MappingSummarizer(analyses, model),
            only=set(analyses), replace=True,
        )
        counts = db.counts(conn)
    out = {
        "model": model,
        "summarized": stats.summarized,
        "failed": [{"git_sha": sha, "error": why} for sha, why in stats.failed],
        "index": counts,
    }
    root.update(metadata={"commits": len(analyses), "summarized": stats.summarized,
                          "failed": len(stats.failed)})
    if stats.failed:
        root.update(level="WARNING", status_message=f"{len(stats.failed)} commit(s) failed")
    return out
