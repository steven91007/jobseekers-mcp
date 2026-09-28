"""Entry point: python -m mcp_server [--check]

Serves MCP over stdio. stdout is the protocol channel, so logs go to stderr.
`--check` prints the registered tools and the Langfuse connection state, then exits.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    # MCP clients start the server from any working directory; resolve .env from the project.
    env_file = os.getenv("JOBSEEKERS_ENV_FILE", "").strip() or str(PROJECT_ROOT / ".env")
    load_dotenv(env_file)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m mcp_server")
    p.add_argument("--check", action="store_true", help="list tools, test Langfuse auth, and exit")
    args = p.parse_args(argv)

    _load_env()  # before importing the server: observability reads env at import time
    logging.basicConfig(
        stream=sys.stderr,
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    sys.path.insert(0, str(PROJECT_ROOT))

    from . import observability as obs
    from .server import mcp

    live = obs.init()

    def _terminate(signum, _frame):
        # Clients stop stdio servers with SIGTERM; Python's default skips finally blocks.
        obs.shutdown()
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, _terminate)
    try:
        if args.check:
            tools = asyncio.run(mcp.list_tools())
            print(f"{len(tools)} tools: {', '.join(t.name for t in tools)}", file=sys.stderr)
            ok, msg = obs.auth_check() if live else (False, "keys not set; tracing off")
            print(f"Langfuse: {'auth OK' if ok else msg} (session {obs.SESSION_ID})", file=sys.stderr)
            return 0 if (ok or not live) else 1
        mcp.run("stdio")
        return 0
    finally:
        obs.shutdown()


if __name__ == "__main__":
    sys.exit(main())
