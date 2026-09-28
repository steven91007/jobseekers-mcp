"""Locate the Jobseekers project whose modules this server wraps.

The server does not copy linkedin_scraper, visa, gitkb or bot.db; it imports them
from a Jobseekers checkout (https://github.com/steven91007/Jobseekers-). That
checkout is found in this order:

1. ``JOBSEEKERS_ROOT``
2. the directory above this repository, which is the Jobseekers checkout when this
   repository is mounted there as the ``jobseekers-mcp`` submodule
3. the current working directory or one of its parents (MCP clients start stdio
   servers in the project they belong to)

The root is appended to ``sys.path``, after installed packages, so nothing in the
Jobseekers checkout can shadow them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MARKERS = ("linkedin_scraper.py", "visa.py", "gitkb", "bot")


class JobseekersNotFound(RuntimeError):
    pass


def is_root(path: Path) -> bool:
    return all((path / marker).exists() for marker in MARKERS)


def find_root() -> Path:
    env = os.getenv("JOBSEEKERS_ROOT", "").strip()
    if env:
        root = Path(env).expanduser().resolve()
        if not is_root(root):
            raise JobseekersNotFound(
                f"JOBSEEKERS_ROOT={env} is not a Jobseekers checkout "
                f"(expected {', '.join(MARKERS)} there)"
            )
        return root
    submodule_parent = Path(__file__).resolve().parent.parent.parent
    cwd = Path.cwd().resolve()
    for candidate in (submodule_parent, cwd, *cwd.parents):
        if is_root(candidate):
            return candidate
    raise JobseekersNotFound(
        "could not find the Jobseekers project; set JOBSEEKERS_ROOT to a checkout of "
        "https://github.com/steven91007/Jobseekers-"
    )


ROOT = find_root()
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))
