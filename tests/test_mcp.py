"""pytest entry for tests/mcp_checks.py.

The checks patch linkedin_scraper and install a process-wide OpenTelemetry tracer
provider for Langfuse, so they run in their own interpreter rather than inside the
pytest process, where they would leak into the jobagent tests.
"""
import pathlib, subprocess, sys

SCRIPT = pathlib.Path(__file__).with_name("mcp_checks.py")


def test_mcp_server():
    r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, str(SCRIPT)]))
