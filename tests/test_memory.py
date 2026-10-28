"""What RELAY remembers, and how it answers questions about it.

The store is the audit log, which has recorded every run since the beginning -
this adds retrieval, not storage. The thing being tested is that a question gets
an ANSWER (read-only, no approval) rather than a plan of action.
"""

from __future__ import annotations

import datetime as dt

import pytest

from relay.audit import AuditLog
from relay.loop import new_loop
from relay.memory import Memory, keywords
from relay.planner import GoalIntent, Planner, PlanningError, RegexIntentReader
from relay.schemas import StopState, ToolName

TUE = dt.date(2026, 10, 27)


def remember(audit: AuditLog, run_id: str, goal: str, notes: list[str], when: dt.datetime):
    """Write a past run straight into the log, as a real run would have."""
    audit._db.execute(
        "INSERT INTO runs (id, goal, created_at, stop_state, summary) VALUES (?,?,?,?,?)",
        (run_id, goal, when.isoformat(), "done", "Over to you"),
    )
    for note in notes:
        call = audit.begin_tool_call(run_id, "note", {"text": note}, f"h{hash(note)}", None)
        audit.end_tool_call(call, "ok", {"note": note})
    audit._db.commit()


@pytest.fixture
def memory(tmp_path):
    audit = AuditLog(tmp_path / "m.db")
    now = dt.datetime.now(dt.UTC)
    remember(audit, "r1", "plan a 15 day workout routine",
             ["Day 1-5: upper body", "Day 6-10: cardio", "Day 11-15: legs"],
             now - dt.timedelta(days=9))
    remember(audit, "r2", "plan a trip to manali",
             ["Book transport", "Book accommodation"], now - dt.timedelta(days=2))
    remember(audit, "r3", "plan a weekly study timetable", ["List subjects"], now)
    return Memory(tmp_path / "m.db")


# --------------------------------------------------------------------------- #
# Finding the right past run
# --------------------------------------------------------------------------- #


def test_a_distinctive_word_finds_its_run(memory):
    assert memory.search("manali")[0].goal == "plan a trip to manali"


def test_a_word_shared_by_everything_finds_nothing(memory):
    """Every goal here starts "plan". Counting raw overlap let "what is my
    workout plan" recall a trip to Manali on that one word."""
    assert memory.search("plan") == []


def test_the_question_still_matches_on_its_rare_word(memory):
    assert memory.search("what is my workout plan")[0].goal.startswith("plan a 15 day workout")


def test_an_unrelated_question_recalls_nothing(memory):
    assert memory.search("what is the capital of France") == []


def test_filler_words_are_not_matched_on(memory):
    assert keywords("what is the plan for my trip") == {"plan", "trip"}


def test_recent_needs_no_keywords(memory):
    assert [r.goal for r in memory.recent(2)][0] == "plan a weekly study timetable"


# --------------------------------------------------------------------------- #
# The dates, which are the whole point of "which day am I on?"
# --------------------------------------------------------------------------- #


def test_days_ago_counts_calendar_days(memory):
    workout = memory.search("workout")[0]
    assert workout.days_ago == 9


def test_a_run_from_yesterday_evening_is_not_today(tmp_path):
    """.days on a timedelta called a 14-hour-old plan "today", which is the one
    number that must not be wrong for "which day am I on?"."""
    audit = AuditLog(tmp_path / "y.db")
    remember(audit, "r", "plan something", ["Do the thing"],
             dt.datetime.now(dt.UTC) - dt.timedelta(hours=14))
    run = Memory(tmp_path / "y.db").search("something")[0]
    assert run.days_ago in (0, 1)  # depends on the hour, but never negative
    assert run.days_ago == (dt.datetime.now(dt.UTC).date()
                            - (dt.datetime.now(dt.UTC) - dt.timedelta(hours=14)).date()).days


def test_the_description_carries_the_date_and_the_steps(memory):
    text = memory.search("workout")[0].describe()
    assert "9 days ago" in text
    assert "Day 6-10: cardio" in text


# --------------------------------------------------------------------------- #
# A question is answered, not planned
# --------------------------------------------------------------------------- #


@pytest.fixture
def planner(backend, memory):
    return Planner(backend=backend, reader=RegexIntentReader(), today=TUE, memory=memory)


def test_a_question_produces_an_answer_step(planner):
    plan = planner.plan("what is my workout plan?")
    # The finish step is what makes the loop report the answer rather than
    # falling through to "all steps completed".
    assert [s.action.tool for s in plan.steps] == [ToolName.ANSWER, ToolName.FINISH]


def test_an_answer_needs_no_approval(planner):
    """Telling you what it already planned is not an action."""
    plan = planner.plan("what is my workout plan?")
    assert not any(s.action.has_side_effect for s in plan.steps)


def test_the_answer_contains_the_recalled_steps(planner):
    text = planner.plan("what is my workout plan?").steps[0].action.args["text"]
    assert "Day 6-10: cardio" in text


def test_an_unknown_question_says_so_rather_than_guessing(planner):
    """The important one: no record must not become an invented answer."""
    text = planner.plan("what is my medication schedule?").steps[0].action.args["text"]
    assert "no record" in text.lower()


def test_a_request_is_still_planned_not_answered(planner):
    """"Email Rahul..." starts with no question word and must not be answered."""
    plan = planner.plan("Email Rahul Menon the Q3 report.",
                        __import__("relay.planner", fromlist=["Choices"]).Choices(
                            files={"Q3 report": "f_q3_final"}))
    assert ToolName.ANSWER not in [s.action.tool for s in plan.steps]


def test_a_question_that_asks_for_action_is_not_a_question(planner):
    """"what time should I email Rahul" is a request wearing a question mark."""
    intent = RegexIntentReader().read("what time should I email Rahul Menon")
    assert intent.question is None


def test_without_memory_it_refuses_rather_than_pretending(backend):
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TUE, memory=None)
    with pytest.raises(PlanningError, match="no memory"):
        planner.plan("what is my workout plan?")


# --------------------------------------------------------------------------- #
# End to end through the loop
# --------------------------------------------------------------------------- #


def test_a_question_run_ends_done_with_the_answer(backend, audit, memory):
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TUE, memory=memory)
    status = new_loop(planner, audit, run_id="ask").start("what is my workout plan?")

    assert status.finished and status.stop_state is StopState.DONE
    assert "Day 6-10: cardio" in status.summary
    assert backend.sent_count == 0 and backend.event_count == 0


def test_the_answer_is_recorded_in_the_audit_log(backend, audit, memory):
    """An answer that bypassed the loop would be the one thing RELAY does with
    no record of it."""
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TUE, memory=memory)
    loop = new_loop(planner, audit, run_id="ask2")
    loop.start("what is my workout plan?")
    assert [r["tool"] for r in audit.rows("ask2")] == ["answer", "finish"]


# --------------------------------------------------------------------------- #
# What is NOT a memory
# --------------------------------------------------------------------------- #


def test_a_question_does_not_recall_itself(backend, audit, memory, tmp_path):
    """The defect this exists for: the run row is written with the goal BEFORE
    planning starts, so searching the log found the question itself and
    answered "your request was left unfinished"."""
    shared = tmp_path / "m.db"
    planner = Planner(backend=backend, reader=RegexIntentReader(), today=TUE,
                      memory=Memory(shared))
    loop = new_loop(planner, AuditLog(shared), run_id="asking")
    status = loop.start("what is my medication schedule?")

    assert status.stop_state is StopState.DONE
    assert "no record" in status.summary.lower()


def test_a_past_question_is_not_a_memory(tmp_path):
    """Answering is not doing, so yesterday's question is noise competing with
    the plan it was asking about."""
    audit = AuditLog(tmp_path / "q.db")
    now = dt.datetime.now(dt.UTC)
    remember(audit, "plan", "plan a 15 day workout routine", ["Day 1-5: upper body"],
             now - dt.timedelta(days=3))
    audit._db.execute(
        "INSERT INTO runs (id, goal, created_at, stop_state, summary) VALUES (?,?,?,?,?)",
        ("asked", "what is my workout for today", (now - dt.timedelta(days=1)).isoformat(),
         "done", "You are on day 2"),
    )
    # Both calls a real question run makes. The "finish" is the one that
    # mattered: excluding only "answer" left it memorable, because every run
    # finishes, so the next question recalled this question.
    for tool, args in (("answer", {"text": "You are on day 2"}), ("finish", {"summary": "x"})):
        call = audit.begin_tool_call("asked", tool, args, f"h{tool}", None)
        audit.end_tool_call(call, "ok", {})
    audit._db.commit()

    found = Memory(tmp_path / "q.db").search("what is my workout for today")
    assert [r.run_id for r in found] == ["plan"]


def test_an_empty_run_is_not_a_memory(tmp_path):
    """A run that recorded nothing has nothing to remember."""
    audit = AuditLog(tmp_path / "e.db")
    remember(audit, "nothing", "plan a trip to manali", [], dt.datetime.now(dt.UTC))
    assert Memory(tmp_path / "e.db").search("manali") == []
