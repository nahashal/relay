"""Do the safety tests actually catch broken safety code?

LESSON 16 - a green suite is not evidence until you have seen it go red.
The eval report's two headline claims are "zero unapproved actions" and "every
run ends in a named stop state". A passing suite proves neither on its own: it
might pass because the assertions are vacuous. So we break each safety property
on purpose and require the suite to notice.

Each mutant runs in its own subprocess, because mutations applied in one process
would stack and we would no longer know which one a failure came from.

Run it:  .venv/Scripts/python.exe scripts/mutation_check.py

Exit 0 means every mutant was caught. Exit 1 means at least one SURVIVED, which
is a real problem: that safety property could break in production and these
tests would stay green.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: name -> (what it sabotages, which tests must notice)
MUTANTS: dict[str, tuple[str, list[str]]] = {
    "no_side_effects": (
        "declares nothing a side effect, so the gate never engages",
        ["tests/test_approval_gate.py"],
    ),
    "binding_is_a_noop": (
        "stops resolving step references, so cards and tokens cover placeholders",
        ["tests/test_loop.py"],
    ),
    "everything_looks_bound": (
        "claims every action is bound, so the executor stops refusing placeholders",
        ["tests/test_loop.py", "tests/test_approval_gate.py"],
    ),
    "tokens_always_verify": (
        "accepts any token for any action, voiding the hash binding",
        ["tests/test_approval_gate.py"],
    ),
    "replans_are_free": (
        "removes the replan limit, so a rejection loop never stops",
        ["tests/test_loop.py"],
    ),
    "busy_errors_mean_free": (
        "ignores per-calendar errors, so an unreadable calendar looks free",
        ["tests/test_google_backend.py"],
    ),
    "live_falls_back_to_mock": (
        "silently mocks a misconfigured live run instead of refusing it",
        ["tests/test_backends.py"],
    ),
}


def apply_mutant(name: str) -> None:
    """Sabotage one safety property in this process."""
    import relay.approvals as approvals
    import relay.executor as executor
    import relay.loop as loop
    import relay.schemas as schemas

    match name:
        case "no_side_effects":
            schemas.SIDE_EFFECT_TOOLS = frozenset()
            executor.Action.has_side_effect = property(lambda self: False)
        case "binding_is_a_noop":
            loop.bind_action = lambda action, results: action
        case "everything_looks_bound":
            schemas.Action.is_bound = property(lambda self: True)
        case "tokens_always_verify":
            executor.verify_token = lambda token, run_id, action: True
            approvals.verify_token = lambda token, run_id, action: True
        case "replans_are_free":
            # Patching max_replans would do nothing - new_loop passes it
            # explicitly, so the instance attribute wins. Remove the guard
            # itself, which is what "no limit" actually means.
            def unlimited(self, reason: str):  # type: ignore[no-untyped-def]
                self.replans_used += 1
                self.pending = None
                return self._plan_and_advance()

            loop.AgentLoop._replan = unlimited
        case "busy_errors_mean_free":
            import relay.tools.google_backend as google_backend

            def lenient(response, people):
                out = []
                for person in people:
                    for interval in (response.get("calendars", {}).get(person) or {}).get(
                        "busy", []
                    ):
                        out.append(
                            (
                                __import__("datetime").datetime.fromisoformat(
                                    interval["start"].replace("Z", "+00:00")
                                ),
                                __import__("datetime").datetime.fromisoformat(
                                    interval["end"].replace("Z", "+00:00")
                                ),
                            )
                        )
                return out

            google_backend.parse_busy = lenient
        case "live_falls_back_to_mock":
            import relay.backends as backends

            backends.make_backend = lambda: backends.MockBackend()
        case _:
            raise SystemExit(f"unknown mutant {name!r}")


def run_mutant(name: str) -> bool:
    """True if the suite caught this mutant."""
    _, test_paths = MUTANTS[name]
    result = subprocess.run(
        [sys.executable, __file__, *test_paths],
        cwd=ROOT,
        env={**os.environ, "RELAY_MUTANT": name},
        capture_output=True,
        text=True,
    )
    return result.returncode != 0


def main() -> int:
    mutant = os.environ.get("RELAY_MUTANT")

    if mutant:
        # Child process: apply the mutation, then run the named tests.
        import pytest

        apply_mutant(mutant)
        return pytest.main(["-q", "--no-header", "-p", "no:cacheprovider", *sys.argv[1:]])

    survivors: list[str] = []
    for name, (what, _) in MUTANTS.items():
        caught = run_mutant(name)
        print(f"{'caught ' if caught else 'SURVIVED'}  {name}: {what}")
        if not caught:
            survivors.append(name)

    print()
    if survivors:
        print(f"{len(survivors)} mutant(s) survived: {', '.join(survivors)}")
        print("Those safety properties are not actually covered. Fix the tests.")
        return 1
    print(f"all {len(MUTANTS)} mutants caught - the safety tests have teeth")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
