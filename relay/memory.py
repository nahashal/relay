"""What RELAY remembers: past runs, read back from the audit log.

LESSON 47 - the memory was already there; only the retrieval was missing.
Every run since the first one has written its goal, its plan, its tool calls and
its stop state to SQLite. That was built as *evidence* - the thing the eval
report counts "zero unapproved side effects" from. It turns out to be a complete
record of what the user has asked for and what RELAY planned, which is exactly
what "can it remember?" wants.

So this module adds no storage. It adds a way to ask the storage a question.

What a remembered run is NOT: a fact RELAY knows. It is a record that on some
date the user asked for something and RELAY planned these steps. Everything read
back is the user's own words and the model's own earlier output, so it is passed
on as data to be interpreted, never as instructions to follow.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .audit import DEFAULT_DB, AuditLog

#: Words that carry no signal when matching a question against past goals.
_NOISE = frozenset(
    """a an and are as at be by can could did do does for from had has have how
    i if in is it me my of on or our should so than that the their them then
    there these they this to was we were what when where which who why will
    with would you your""".split()
)

#: Past this, a "memory" is less recall than archaeology, and the prompt gets
#: long enough to cost real money.
MAX_RECALLED = 5

#: A word appearing in more than this share of past goals is treated as a
#: stopword for this user: "plan", for someone who mostly asks RELAY to plan.
COMMON_SHARE = 0.5

#: Below this many past runs, "appears in most goals" means nothing, so no word
#: is treated as a stopword.
MIN_CORPUS = 3

#: LESSON 49 - what is worth remembering is not the same as what was recorded.
#: A run earns a place in memory by having DONE something: a step written down,
#: an email sent, a meeting arranged. Two kinds of run fail that test:
#:
#:   * the run being planned right now. Its row is written with the goal before
#:     planning begins, so without this a question searched the log and found
#:     ITSELF, then reported the question back as "a request left unfinished".
#:   * a previous question. Answering is not doing, so yesterday's "what is my
#:     workout?" is noise competing with the workout plan it asked about.
#:
#: Both are "a run with no completed work", which is one condition, not two.
#:
#: Stated as a whitelist on purpose. Naming the tools that do NOT count left
#: "finish" behind - and every run has one, so question runs stayed memorable
#: and the next question recalled the previous question. A whitelist cannot
#: grow that hole: a tool added later is not work until it is listed here.
WORK_TOOLS = ("note", "send_email", "create_event")

MEMORABLE = (
    " AND id IN (SELECT run_id FROM tool_calls WHERE outcome = 'ok' AND tool IN ("
    + ",".join(f"'{tool}'" for tool in WORK_TOOLS)
    + "))"
)


@dataclass
class RememberedRun:
    """One past run, as much of it as is worth remembering."""

    run_id: str
    goal: str
    created_at: dt.datetime
    stop_state: str | None
    summary: str
    #: The steps RELAY left to the user - the useful content of a planning run.
    checklist: list[str] = field(default_factory=list)
    #: What it actually did, if anything.
    actions: list[str] = field(default_factory=list)

    @property
    def days_ago(self) -> int:
        """Calendar days, not elapsed hours.

        A run from 6pm yesterday is 14 hours old, which `.days` calls 0 - so a
        plan made yesterday was being described as "today". For "which day of
        the plan am I on?" that is the one number that must not be wrong.
        """
        return max(0, (dt.datetime.now(dt.UTC).date() - self.created_at.date()).days)

    def describe(self) -> str:
        """A compact rendering for a prompt.

        The DATE is the part that earns its place: "what is today's workout?"
        is unanswerable without knowing which day the plan started.
        """
        when = self.created_at.strftime("%a %d %b %Y")
        ago = "today" if self.days_ago == 0 else f"{self.days_ago} days ago"
        lines = [f'On {when} ({ago}) you asked: "{self.goal}"']
        if self.actions:
            lines.append("RELAY did: " + "; ".join(self.actions))
        if self.checklist:
            lines.append("RELAY planned these steps:")
            lines += [f"  {i}. {step}" for i, step in enumerate(self.checklist, 1)]
        if not self.actions and not self.checklist:
            lines.append(f"It ended: {self.stop_state or 'unfinished'}")
        return "\n".join(lines)


def keywords(text: str) -> set[str]:
    """The words worth matching on: lowercase, no punctuation, no filler."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _NOISE and len(w) > 2}


class Memory:
    """Past runs, searchable. Read-only: it never writes to the audit log."""

    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self._audit = AuditLog(path)

    def close(self) -> None:
        self._audit.close()

    # --- reading one run ----------------------------------------------------

    def _load(self, row) -> RememberedRun:
        checklist: list[str] = []
        actions: list[str] = []
        for call in self._audit.rows(row["run_id"] if "run_id" in row.keys() else row["id"]):
            if call["outcome"] != "ok":
                continue
            if call["tool"] == "note":
                checklist.append(str(json.loads(call["args_json"]).get("text", "")))
            elif call["tool"] == "send_email":
                actions.append("sent an email")
            elif call["tool"] == "create_event":
                actions.append("arranged a meeting")
        return RememberedRun(
            run_id=row["id"],
            goal=row["goal"],
            created_at=dt.datetime.fromisoformat(row["created_at"]),
            stop_state=row["stop_state"],
            summary=row["summary"] or "",
            checklist=checklist,
            actions=actions,
        )

    # --- searching ----------------------------------------------------------

    def search(self, question: str, limit: int = MAX_RECALLED) -> list[RememberedRun]:
        """Past runs most likely to answer this question, best first.

        Scored by shared keywords rather than embeddings: at a few hundred runs
        this is instant, needs no index to keep in step, and is explainable -
        when it recalls the wrong thing you can see which word did it.

        Ties break towards the RECENT, because "what is today's workout?" means
        the workout plan you made this month, not the one from last year.
        """
        wanted = keywords(question)
        if not wanted:
            return []

        rows = list(
            self._audit._db.execute(
                "SELECT id, goal, created_at, stop_state, summary FROM runs"
                " WHERE goal IS NOT NULL AND goal != ''" + MEMORABLE
                + " ORDER BY created_at DESC"
            )
        )
        if not rows:
            return []

        # How many past goals each word appears in. A word in half of them -
        # "plan", for someone who asks RELAY to plan things - carries almost no
        # signal, and counting raw overlap let "what is my workout plan" recall
        # "plan a trip to manali" on that one word alone.
        goal_words = [keywords(row["goal"]) for row in rows]
        seen_in: dict[str, int] = {}
        for words in goal_words:
            for word in words:
                seen_in[word] = seen_in.get(word, 0) + 1

        # A word in most of the goals is a stopword FOR THIS USER, whatever the
        # dictionary says. An absolute score threshold cannot do this job: with
        # three runs "plan" scores 0.33 and with fifty it scores 0.02, so any
        # fixed cut-off is wrong at one end or the other.
        # ...but only once there is a distribution to learn from. With a single
        # past run every word appears in 100% of goals, which this rule would
        # read as "carries no signal" when the truth is "nothing to compare to".
        common = (
            {word for word, n in seen_in.items() if n > len(rows) * COMMON_SHARE}
            if len(rows) >= MIN_CORPUS
            else set()
        )
        useful = wanted - common
        if not useful:
            return []

        scored: list[tuple[float, str, dict]] = []
        for row, words in zip(rows, goal_words):
            score = sum(1.0 / seen_in[word] for word in useful & words)
            if score > 0:
                scored.append((score, row["created_at"], row))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [self._load(row) for _, _, row in scored[:limit]]

    def recent(self, limit: int = MAX_RECALLED) -> list[RememberedRun]:
        """The last few runs, for "what was I doing?" with no keyword to go on."""
        rows = self._audit._db.execute(
            "SELECT id, goal, created_at, stop_state, summary FROM runs"
            " WHERE goal IS NOT NULL AND goal != ''" + MEMORABLE
            + " ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._load(row) for row in rows]

    def count(self) -> int:
        """How many runs are actually recallable - not how many rows exist."""
        return int(
            self._audit._db.execute(
                "SELECT COUNT(*) c FROM runs WHERE goal IS NOT NULL AND goal != ''"
                + MEMORABLE
            ).fetchone()["c"]
        )
