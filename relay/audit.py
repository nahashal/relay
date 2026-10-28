"""Every plan, approval and tool call, written to SQLite before it matters.

LESSON 2 - the audit log is a safety device, not paperwork.
Two of the plan's guarantees are really just queries over this table:
  * "a retry can never send twice"  -> was this action hash already executed?
  * "zero unapproved side effects"  -> every executed side-effect row has a token.
So the log is written *before* a send is attempted, not after it succeeds.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB = Path(__file__).resolve().parent.parent / "relay.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    goal        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    stop_state  TEXT,
    summary     TEXT
);

CREATE TABLE IF NOT EXISTS plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    revision    INTEGER NOT NULL,       -- 0, then 1 and 2 for the two allowed replans
    plan_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    token       TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    action_hash TEXT NOT NULL,          -- binds the token to one exact action
    verdict     TEXT NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL,
    spent_at    TEXT                    -- set when used, so a token is single-use
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(id),
    tool        TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    action_hash TEXT NOT NULL,
    token       TEXT,                   -- NULL is only legal for read-only tools
    started_at  TEXT NOT NULL,
    outcome     TEXT,                   -- 'ok' or 'error'
    result_json TEXT
);

-- The duplicate-send guard leans on this: one successful side effect per hash.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_calls_effect_once
    ON tool_calls(run_id, action_hash)
    WHERE token IS NOT NULL AND outcome = 'ok';
"""


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class DuplicateSideEffect(RuntimeError):
    """The unique index refused a second successful side effect for one action hash.

    Reaching this means the replay guard in the executor was bypassed - a logic
    bug, not a user error. It is raised *before* anything is sent, so the backstop
    holds; the executor turns it into a BLOCKED run rather than a 500.
    """


class AuditLog:
    """Thin wrapper over SQLite. No ORM: the queries are the point, keep them visible."""

    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path)
        # check_same_thread=False so FastAPI's threadpool can share one log.
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # --- runs ---------------------------------------------------------------

    def start_run(self, run_id: str, goal: str) -> None:
        self._db.execute(
            "INSERT INTO runs (id, goal, created_at) VALUES (?, ?, ?)",
            (run_id, goal, _now()),
        )
        self._db.commit()

    def finish_run(self, run_id: str, stop_state: str, summary: str) -> None:
        self._db.execute(
            "UPDATE runs SET stop_state = ?, summary = ? WHERE id = ?",
            (stop_state, summary, run_id),
        )
        self._db.commit()

    # --- plans --------------------------------------------------------------

    def record_plan(self, run_id: str, revision: int, plan_json: str) -> None:
        self._db.execute(
            "INSERT INTO plans (run_id, revision, plan_json, created_at) VALUES (?, ?, ?, ?)",
            (run_id, revision, plan_json, _now()),
        )
        self._db.commit()

    # --- approvals ----------------------------------------------------------

    def record_approval(
        self, token: str, run_id: str, action_hash: str, verdict: str, note: str | None
    ) -> None:
        self._db.execute(
            "INSERT INTO approvals (token, run_id, action_hash, verdict, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (token, run_id, action_hash, verdict, note, _now()),
        )
        self._db.commit()

    def find_approval(self, token: str) -> sqlite3.Row | None:
        cur = self._db.execute("SELECT * FROM approvals WHERE token = ?", (token,))
        return cur.fetchone()

    def spend_approval(self, token: str) -> bool:
        """Mark a token used. Returns False if it was already spent.

        The ``spent_at IS NULL`` clause does the work: two concurrent attempts
        to spend the same token cannot both update a row.
        """
        cur = self._db.execute(
            "UPDATE approvals SET spent_at = ? WHERE token = ? AND spent_at IS NULL",
            (_now(), token),
        )
        self._db.commit()
        return cur.rowcount == 1

    # --- tool calls ---------------------------------------------------------

    def already_executed(self, run_id: str, action_hash: str) -> bool:
        """Has this exact side effect already succeeded in this run?"""
        cur = self._db.execute(
            "SELECT 1 FROM tool_calls"
            " WHERE run_id = ? AND action_hash = ? AND token IS NOT NULL AND outcome = 'ok'",
            (run_id, action_hash),
        )
        return cur.fetchone() is not None

    def begin_tool_call(
        self,
        run_id: str,
        tool: str,
        args: dict[str, Any],
        action_hash: str,
        token: str | None,
    ) -> int:
        # Conditional insert: the duplicate check and the write are ONE statement,
        # so two concurrent attempts cannot both get past it. Doing this here -
        # before the tool runs - is what makes the guard able to prevent a second
        # send rather than merely notice one.
        #
        # The NOT EXISTS clause only matches rows that carry a token and
        # succeeded, so read-only calls may repeat freely, and a side effect that
        # FAILED may legitimately be retried.
        cur = self._db.execute(
            "INSERT INTO tool_calls (run_id, tool, args_json, action_hash, token, started_at)"
            " SELECT ?, ?, ?, ?, ?, ?"
            " WHERE NOT EXISTS ("
            "   SELECT 1 FROM tool_calls"
            "   WHERE run_id = ? AND action_hash = ? AND token IS NOT NULL AND outcome = 'ok'"
            " )",
            (
                run_id,
                tool,
                json.dumps(args, default=str),
                action_hash,
                token,
                _now(),
                run_id,
                action_hash,
            ),
        )
        self._db.commit()
        if cur.rowcount == 0:
            raise DuplicateSideEffect(
                f"{tool} refused: this exact action already succeeded in run {run_id}"
            )
        return int(cur.lastrowid)

    def end_tool_call(self, call_id: int, outcome: str, result: Any) -> None:
        try:
            self._db.execute(
                "UPDATE tool_calls SET outcome = ?, result_json = ? WHERE id = ?",
                (outcome, json.dumps(result, default=str), call_id),
            )
        except sqlite3.IntegrityError as exc:
            # The index is on (run_id, action_hash) WHERE outcome='ok', so a
            # duplicate can also surface when the row is marked successful.
            raise DuplicateSideEffect(
                f"refused to record a second successful side effect for call {call_id}"
            ) from exc
        self._db.commit()

    def tool_calls_used(self, run_id: str) -> int:
        cur = self._db.execute(
            "SELECT COUNT(*) AS n FROM tool_calls WHERE run_id = ?", (run_id,)
        )
        return int(cur.fetchone()["n"])

    def rows(self, run_id: str) -> Iterator[sqlite3.Row]:
        yield from self._db.execute(
            "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY id", (run_id,)
        )

    def close(self) -> None:
        self._db.close()
