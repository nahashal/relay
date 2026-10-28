"""Loading configuration from a .env file.

LESSON 30 - a secret typed into a terminal is a secret in three places.
`.env.example` existed from day one and nothing ever read it, so the only way to
configure RELAY was to export variables by hand. That is worse than untidy:

  * PowerShell needs the value QUOTED. `$env:K=AQ.xyz` without quotes tries to
    RUN the key as a command, fails, and leaves the variable unset - with an
    error message that talks about cmdlets rather than about quoting.
  * The value lands in the scrollback, and usually in the shell's history file.
  * It is gone the moment the window closes, so it gets pasted again. And again.

A .env file is read from disk, never echoed, already gitignored, and survives a
restart. The key is typed once, into an editor.

``override=False`` matters: a variable already set in the environment wins over
the file. That keeps `RELAY_BACKEND=live python -m ...` working as a one-off
without editing anything, and keeps the tests' monkeypatching authoritative.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Where to look, relative to the project root.
DEFAULT_ENV = Path(__file__).resolve().parent.parent / ".env"

_loaded = False


def load_env(path: Path | str | None = None, *, override: bool = False) -> bool:
    """Read a .env file into the environment. Returns True if one was read.

    Safe to call repeatedly - it only does the work once per process, so a
    script and the module it imports cannot fight over it.
    """
    global _loaded
    if _loaded and path is None:
        return False

    env_path = Path(path) if path else DEFAULT_ENV
    if not env_path.exists():
        _loaded = True
        return False

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Tolerate quotes, because everyone adds them out of habit and a literal
        # pair of quotes inside an API key is never what was meant.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value

    _loaded = True
    return True


def describe_source(name: str) -> str:
    """Where a variable's value came from, for the setup checker's output."""
    if name not in os.environ:
        return "not set"
    return ".env or the shell"
