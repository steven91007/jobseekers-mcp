"""Langfuse tracing for the MCP server, as an optional layer that never breaks a tool call.

Tracing turns on when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set. Without
them, or when the SDK fails to start, every helper here is a no-op.

One MCP tool call is one trace. The tool's root observation carries the tool
arguments as input and the tool result as output; the steps inside it nest as
children:

    search-linkedin-jobs          retriever  root of a search_jobs call
    get-job-detail                retriever  root of a get_job_detail call
    └── classify-visa-rules       span
    check-visa-sponsorship        chain      root of a check_visa call
    └── check-job-visa            chain      one per job (metadata: job_id)
        ├── fetch-job-description retriever
        └── classify-visa-rules   span
    search-git-history / show-git-note / list-git-commits / get-file-history   retriever
    export-pending-commits        retriever
    import-commit-summaries       tool       writes notes and the index
    list-subscriptions / get-bot-status        retriever

Every observation in one server process shares a session id, so one Claude Code
session reads as one Langfuse session. Names are an API: dashboards and
evaluators match on them, so run-specific values go in metadata.

Linked traces: when the MCP client sends W3C trace context (``traceparent``) in
the request's ``_meta``, the tool's root observation becomes a child of the
client's span, joining the client's trace. Otherwise each call starts a new trace.
The SDK's own OpenTelemetry server spans ("tools/call <name>") are not exported;
they would carry no input/output and bury the tool observation one level down.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

log = logging.getLogger(__name__)


class NAMES:
    """Observation names. Treat as an API: evaluators and dashboards match on them."""

    SEARCH_JOBS = "search-linkedin-jobs"
    JOB_DETAIL = "get-job-detail"
    CHECK_VISA = "check-visa-sponsorship"
    CHECK_JOB_VISA = "check-job-visa"
    FETCH_DESCRIPTION = "fetch-job-description"
    CLASSIFY_VISA = "classify-visa-rules"
    KB_SEARCH = "search-git-history"
    KB_SHOW = "show-git-note"
    KB_LOG = "list-git-commits"
    KB_HISTORY = "get-file-history"
    KB_PENDING = "export-pending-commits"
    KB_IMPORT = "import-commit-summaries"
    SUBS_LIST = "list-subscriptions"
    BOT_STATUS = "get-bot-status"


# The MCP SDK names its OpenTelemetry tracer this; its spans are dropped from export.
MCP_SDK_SCOPE = "mcp-python-sdk"

# --- masking ---------------------------------------------------------------------
# Job descriptions carry recruiter emails and phone numbers. Only +country-code
# phone numbers are matched, so 10-digit LinkedIn job ids in URLs survive.
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b", re.I)
_PHONE = re.compile(r"(?<![\w+])\+\d{1,3}(?:[\s./-]?\(?\d{1,5}\)?){2,5}\d")
_SECRET = re.compile(r"\b(?:sk|pk|rk)-(?:lf-|proj-|ant-)?[A-Za-z0-9_-]{16,}\b")


def mask_text(value: str) -> str:
    value = _SECRET.sub("[REDACTED KEY]", value)
    value = _EMAIL.sub("[REDACTED EMAIL]", value)
    return _PHONE.sub("[REDACTED PHONE]", value)


def _mask(*, data: Any, **_kwargs) -> Any:
    """Langfuse `mask` hook for input/output/metadata. Must never raise."""
    try:
        if isinstance(data, str):
            return mask_text(data)
        if isinstance(data, dict):
            return {k: _mask(data=v) for k, v in data.items()}
        if isinstance(data, (list, tuple)):
            return [_mask(data=v) for v in data]
        return data
    except Exception:
        return data


def _should_export(span) -> bool:
    from langfuse.span_filter import is_default_export_span

    scope = getattr(span, "instrumentation_scope", None)
    if scope is not None and scope.name == MCP_SDK_SCOPE:
        return False
    return is_default_export_span(span)


# --- lifecycle -------------------------------------------------------------------

_client = None
_warned = False
SESSION_ID = os.getenv("MCP_SESSION_ID", "").strip() or (
    f"mcp-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
)
USER_ID = os.getenv("JOBAGENT_USER_ID", "").strip() or "me"


def enabled() -> bool:
    return _client is not None


def init(**client_overrides: Any) -> bool:
    """Start the Langfuse client if configured. Returns True when tracing is live.

    ``client_overrides`` go straight to ``Langfuse(...)``; tests pass an in-memory
    ``span_exporter`` so trace shape can be checked without a network.
    """
    global _client
    if _client is not None:
        return True
    public = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    if not (public and secret):
        log.info("Langfuse disabled (LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set)")
        return False
    base_url = (
        os.getenv("LANGFUSE_BASE_URL", "").strip()
        or os.getenv("LANGFUSE_HOST", "").strip()
        or "https://cloud.langfuse.com"
    )
    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=public,
            secret_key=secret,
            base_url=base_url,
            # production | development: keeps test runs out of real dashboards
            environment=os.getenv("LANGFUSE_TRACING_ENVIRONMENT", "").strip() or "production",
            mask=_mask if os.getenv("MCP_LANGFUSE_MASK", "1").strip() != "0" else None,
            should_export_span=_should_export,
            **client_overrides,
        )
    except Exception as e:  # never fatal
        _warn(f"Langfuse failed to start, tracing off: {e}")
        _client = None
    return _client is not None


def auth_check() -> tuple[bool, str]:
    if _client is None:
        return False, "disabled"
    try:
        return (True, "ok") if _client.auth_check() else (False, "auth_check returned False")
    except Exception as e:
        return False, str(e)


def flush() -> None:
    if _client is None:
        return
    try:
        _client.flush()
    except Exception as e:
        _warn(f"Langfuse flush failed: {e}")


def shutdown() -> None:
    if _client is None:
        return
    try:
        _client.shutdown()
    except Exception as e:
        _warn(f"Langfuse shutdown failed: {e}")


def trace_url(trace_id: str | None) -> str | None:
    if _client is None or not trace_id:
        return None
    try:
        return _client.get_trace_url(trace_id=trace_id)
    except Exception:
        return None


def _warn(msg: str) -> None:
    global _warned
    if not _warned:
        log.warning(msg)
        _warned = True


# --- trace context from MCP _meta -----------------------------------------------

_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def parent_from_meta(meta: Any) -> dict[str, str] | None:
    """A Langfuse TraceContext from a request's ``_meta.traceparent``, or None.

    ``meta`` is the request's `_meta` (a mapping or a pydantic model with extra
    fields). All-zero ids are invalid per W3C and are ignored.
    """
    if meta is None:
        return None
    if isinstance(meta, Mapping):
        raw = meta.get("traceparent")
    else:
        raw = getattr(meta, "traceparent", None)
        if raw is None:
            extra = getattr(meta, "model_extra", None) or {}
            raw = extra.get("traceparent")
    if not isinstance(raw, str):
        return None
    m = _TRACEPARENT.match(raw.strip().lower())
    if not m or set(m.group(1)) == {"0"} or set(m.group(2)) == {"0"}:
        return None
    return {"trace_id": m.group(1), "parent_span_id": m.group(2)}


# --- observations ----------------------------------------------------------------


class _NoopObservation:
    trace_id = None
    id = None

    def update(self, **kwargs) -> None:
        pass


class _LiveObservation:
    def __init__(self, obs):
        self._obs = obs
        self.trace_id = getattr(obs, "trace_id", None)
        self.id = getattr(obs, "id", None)

    def update(self, **kwargs) -> None:
        try:
            self._obs.update(**kwargs)
        except Exception as e:
            _warn(f"Langfuse update failed: {e}")


NOOP = _NoopObservation()


@contextmanager
def _observation(cm_factory) -> Iterator[_LiveObservation | _NoopObservation]:
    try:
        cm = cm_factory()
        obs = cm.__enter__()
    except Exception as e:
        _warn(f"Langfuse observation failed to open, continuing untraced: {e}")
        yield NOOP
        return
    handle = _LiveObservation(obs)
    try:
        yield handle
    except BaseException as exc:
        handle.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}"[:1000])
        try:
            cm.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            pass
        raise
    else:
        try:
            cm.__exit__(None, None, None)
        except Exception as e:
            _warn(f"Langfuse observation failed to close: {e}")


@contextmanager
def span(
    name: str, *, as_type: str = "span", input: Any = None, metadata: Any = None
) -> Iterator[_LiveObservation | _NoopObservation]:
    """A child observation of whatever observation is current. Errors are recorded as ERROR."""
    if _client is None:
        yield NOOP
        return
    with _observation(
        lambda: _client.start_as_current_observation(
            name=name, as_type=as_type, input=input, metadata=metadata
        )
    ) as obs:
        yield obs


@contextmanager
def tool_call(
    name: str,
    *,
    as_type: str,
    input: Any,
    tool: str,
    feature: str,
    meta: Any = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[_LiveObservation | _NoopObservation]:
    """The root observation of one MCP tool call, with session/user/tags propagated.

    The observation is opened from an empty OpenTelemetry context (or the client's
    ``traceparent``), never under the SDK's own server span, which is not exported.
    """
    if _client is None:
        yield NOOP
        return
    try:
        from langfuse import propagate_attributes
        from opentelemetry import context as otel_context

        token = otel_context.attach(otel_context.Context())
    except Exception as e:
        _warn(f"Langfuse tool_call setup failed, continuing untraced: {e}")
        yield NOOP
        return

    parent = parent_from_meta(meta)
    try:
        with propagate_attributes(
            session_id=SESSION_ID,
            user_id=USER_ID,
            tags=["mcp", feature],
            trace_name=name,
            metadata={"mcp_tool": tool, "mcp_server": "jobseekers"},
        ):
            with _observation(
                lambda: _client.start_as_current_observation(
                    trace_context=parent,
                    name=name,
                    as_type=as_type,
                    input=input,
                    metadata={**(metadata or {}), "linked_to_client_trace": parent is not None},
                )
            ) as obs:
                yield obs
    finally:
        try:
            otel_context.detach(token)
        except Exception:
            pass
