"""Gmail (read-only) and the job-application Google Sheet, via Jobseekers' jobtracker.

Sign-in happens once in a terminal (``python -m jobtracker auth``); the server only
loads and refreshes the saved token, so it never opens a browser. Clients are built
on first use and reused for the life of the process.
"""

from __future__ import annotations

import functools
from typing import Any, Callable

from ._core import ROOT


class TrackerError(RuntimeError):
    pass


def _tracker():
    try:
        from jobtracker import config, gmail, google_auth, sheets
    except ImportError as e:
        raise TrackerError(f"this Jobseekers checkout ({ROOT}) has no jobtracker package or its "
                           f"Google dependencies are missing ({e}); update it and install "
                           "google-api-python-client and google-auth-oauthlib") from e
    return config, gmail, google_auth, sheets


@functools.cache
def _settings():
    config, *_ = _tracker()
    return config.load()


@functools.cache
def _gmail():
    _, gmail, *_ = _tracker()
    return gmail.GmailClient.from_settings(_settings())


@functools.cache
def _sheet():
    *_, sheets = _tracker()
    return sheets.ApplicationSheet.from_settings(_settings())


def reset() -> None:
    """Forget cached settings and clients (after re-auth or a .env change)."""
    for fn in (_settings, _gmail, _sheet):
        fn.cache_clear()


def _guard(fn: Callable[[], dict]) -> dict:
    """Run fn, turning known tracker and Google API failures into TrackerError."""
    config, gmail, google_auth, sheets = _tracker()
    try:
        return fn()
    except (config.ConfigError, gmail.GmailError, google_auth.AuthError, sheets.SheetError) as e:
        raise TrackerError(str(e)) from e
    except Exception as e:
        from googleapiclient.errors import HttpError

        if isinstance(e, HttpError):
            status = getattr(e.resp, "status", "?")
            hint = {403: " (is the API enabled in the Google Cloud project, and is the sheet shared "
                         "with the signed-in account?)",
                    404: " (check JOBTRACKER_SHEET_ID / the message id)"}.get(int(status) if str(status).isdigit() else 0, "")
            raise TrackerError(f"Google API {status}: {e.reason}{hint}") from e
        raise


# --- Gmail -----------------------------------------------------------------------


def gmail_search(query: str = "", max_results: int = 20) -> dict[str, Any]:
    def run():
        q = query.strip() or _settings().gmail_query
        mails = _gmail().search(q, max_results)
        return {"query": q, "count": len(mails), "emails": [m.to_dict() for m in mails]}
    return _guard(run)


def gmail_read(message_id: str, max_chars: int = 8000) -> dict[str, Any]:
    return _guard(lambda: _gmail().get(message_id.strip(), max_chars=max_chars).to_dict())


# --- Sheet -----------------------------------------------------------------------


def _sheet_meta(sheet) -> dict[str, Any]:
    cols = sheet.columns()
    return {"spreadsheet_id": sheet.spreadsheet_id, "tab": sheet.tab, "headers": cols.headers,
            "columns": {k: cols.headers[i] for k, i in cols.fields.items()}}


def sheet_applications(find: str = "") -> dict[str, Any]:
    def run():
        sheet = _sheet()
        apps = sheet.find(find) if find.strip() else sheet.applications()
        return {**_sheet_meta(sheet), "count": len(apps), "applications": [a.to_dict() for a in apps]}
    return _guard(run)


def sheet_update(row: int, changes: dict[str, str], expect_company: str = "") -> dict[str, Any]:
    return _guard(lambda: _sheet().update(row, {k: str(v) for k, v in changes.items()},
                                          expect_company=expect_company or None))


def sheet_append(values: dict[str, str]) -> dict[str, Any]:
    return _guard(lambda: _sheet().append({k: str(v) for k, v in values.items()}))
