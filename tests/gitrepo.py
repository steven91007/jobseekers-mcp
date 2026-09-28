"""A throwaway git repository with three commits, for the gitkb tool checks."""
import os, subprocess

FIXED_ENV = {
    "GIT_AUTHOR_NAME": "Test Author", "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Test Committer", "GIT_COMMITTER_EMAIL": "committer@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
}


def git(repo, *args, date=None):
    env = dict(os.environ, **FIXED_ENV)
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)


def make_repo(root):
    """first commit: a.py + README.md; second: edit a.py, move README, add a binary;
    third: delete a.py. a.py therefore has three changes."""
    git(root, "init", "-q", "-b", "main")
    (root / "a.py").write_text("def f():\n    return 1\n")
    (root / "README.md").write_text("# demo\n")
    git(root, "add", "."); git(root, "commit", "-q", "-m", "first commit", date="2026-01-01T00:00:00+00:00")

    (root / "a.py").write_text("def f():\n    return 2\n\n\ndef g():\n    return 3\n")
    (root / "docs").mkdir(); git(root, "mv", "README.md", "docs/README.md")
    (root / "img.bin").write_bytes(b"\x00\x01\x02binary\x00")
    git(root, "add", "."); git(root, "commit", "-q", "-m", "second commit\n\nwith a body", date="2026-01-02T00:00:00+00:00")

    git(root, "rm", "-q", "a.py")
    git(root, "commit", "-q", "-m", "third commit", date="2026-01-03T00:00:00+00:00")
