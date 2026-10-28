# RELAY

One plain-language goal in. An approved plan, real email and calendar actions, and a named stop state out.

> Example goal: *"Email Rahul the Q3 report and set a 30-minute review with him and Anu next week."*

Nothing is sent, booked or changed without an approval tied to that exact action.

## Where this is in the build plan

This repo covers **build steps 1-7** of `RELAY-Build-Plan.pdf`. All 15 scenarios pass against the mock; what remains is the deployed demo and a run against a real account. (The video in the plan's deliverable list has been dropped.)

| Build step | State |
|---|---|
| 1. Setup: repo, schemas, audit store, config | done, except the Google Cloud / OAuth half (needs the test account) |
| 2. Tool layer: 8 tools plus mocks | **written** — `MockBackend` and `GoogleBackend` behind one protocol; the live one is tested with service doubles, never against Google |
| 3. Planner: goal → validated plan | **done** — offline reader + Gemini reader behind one interface; `POST /run` is live |
| 4. Executor and approval gate | **done and tested** (landed early: the schemas were useless without it) |
| 5. Verify and stop states | **done** — the loop, all five stop states, late binding, the replan limit; `scripts/demo.py` runs the example goal end to end |
| 6. UI | **done** — one-file approval UI at `/`, driven in a browser end to end |
| 7. Test, demo, document | **15 of 15 scenarios pass**, 204 tests, 7 of 7 mutants caught, [EVAL-REPORT.md](EVAL-REPORT.md) generated from an actual run. Demo deploy outstanding — see the note below |

The approval gate arrived ahead of schedule because it is the one claim the whole project
rests on, and it was cheap to prove before there was a planner to argue with.

## Layout

```
relay/
  schemas.py       Plan, Step, Action, StopState - plans are validated data, not prose
  backends.py      mock, local or live, chosen by env - never a silent fallback
  brevo.py         transactional email, transport injected so it is testable
  ics.py           .ics invitations: METHOD:REQUEST makes it an invite, not a file
  google_auth.py   OAuth for the test account; the narrowest scopes that work
  slots.py         free-slot arithmetic, shared by both backends
  scenarios.py     the 15 acceptance scenarios, as records
  scenario_runner.py  drives one scenario to a stop state
  loop.py          the state machine: plan, approve, act, verify, stop
  planner.py       goal -> GoalIntent (model) -> resolved Plan (tools only)
  dates.py         "next week" -> Mon 2 - Fri 6 Nov 2026, in the user's timezone
  memory.py        past runs, read back from the audit log - storage was already there
  approvals.py     action hashing, HMAC tokens, the void-on-change rule
  audit.py         SQLite: runs, plans, approvals, tool_calls
  executor.py      the ONE door to a side effect: budget, gate, replay, spend, execute, verify
  api.py           FastAPI: /run, /answer, /decide, /redraft, /status, /audit
  static/
    index.html     the approval UI: one file, no build step
  tools/
    base.py        the Backend protocol - the seam that lets Outlook be added later
    mock_backend.py  Google stand-in that sends nothing
    google_backend.py  the live one: Gmail, Calendar, Drive, People
    local_backend.py   the no-Google one: local files, Brevo, .ics invitations
fixtures/
  sample_data.json two Rahuls, two Q3 reports, busy blocks for Mon 2 - Fri 6 Nov 2026
scripts/
  check_setup.py     read-only: is the Gemini key and the Google OAuth actually working?
  demo.py            the example goal end to end in a terminal
  eval_report.py     runs the 15 scenarios and writes EVAL-REPORT.md
  mutation_check.py  breaks 7 safety properties on purpose; the suite must go red
tests/
  test_approval_gate.py  "zero unapproved actions", as assertions
  test_audit.py          the database backstop under the replay guard
  test_tools_mock.py     each tool, plus the awkward scenarios
  test_dates.py          date arithmetic, including the Sunday off-by-one
  test_loop.py           one test per stop state, plus late binding
  test_planner.py        right plan, or a question - never a guess
  test_api.py            plan, approve, send over HTTP
  test_ui.py             what the browser needs: options, slots, redraft
  test_backends.py       live is the mode you have to ask for
  test_google_backend.py the live backend, driven with service doubles
  test_local_backend.py  the no-Google backend, Brevo transport faked
  test_scenarios.py      the 15 acceptance scenarios
```

## Run it

Needs Python 3.12 (`winget install Python.Python.3.12` on Windows).

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pytest -q
```

The tests need no configuration at all - they pin themselves to the mock
backend and strip any credentials from the environment, so the suite behaves
the same on a machine with keys and one without.

### Before the first real run

Two files are deliberately **not** in the repository, because they hold your
data rather than the project's. Copy the templates:

```bash
cp .env.example .env                      # then fill in the keys you need
cp contacts.example.json contacts.json    # who RELAY can email
```

Without `contacts.json`, `RELAY_BACKEND=local` stops with *"no contacts file"* -
which is correct, but puzzling if you have not read this far.

Then check what is actually working, rather than guessing:

```bash
.venv/Scripts/python.exe scripts/check_setup.py
```

It is read-only: it lists models, reads your own profile and counts files, and
sends nothing. Everything it finds wrong prints a `fix:` line with the next
action.

See the example goal run end to end:

```bash
.venv/Scripts/python.exe scripts/demo.py
```

Open the approval UI:

```bash
.venv/Scripts/python.exe -m uvicorn relay.api:app --port 8111 --reload
```

Then visit <http://localhost:8111/>.

`RELAY_BACKEND=mock` (the default in `.env.example`) means no credentials are needed and nothing leaves the process.

## The five stop states

Every run ends in exactly one, and `StopState` has no "still running" member, so code
holding one can never loop:

| State | Means |
|---|---|
| `done` | every step ran **and** was verified — message in Sent, event on the calendar |
| `rejected` | you declined and asked for no revision |
| `needs_input` | a question is unanswered (which Rahul?). It pauses; it does not guess |
| `blocked` | hard failure: no common slot, no matching file |
| `budget_hit` | more than 20 tool calls or 2 replans |

## How the gate actually works

1. The user approves one card. The server hashes that **exact** action — recipient, attachment, slot, subject, body — and mints an HMAC token for it.
2. `Executor.run_action` is the only path to `send_email` or `create_event`. A side-effect action with no token, a forged token, a token from another run, or a token whose hash no longer matches is refused.
3. The token is spent before the send, so it authorises exactly one action.
4. Before sending, the executor asks the audit log whether this hash already succeeded. A retried timeout replays the first result instead of sending twice.
5. After sending, state is read back. A 200 response is not accepted as proof.

The model never sees a token. "Edit" authorises nothing — it produces a new card that must be
approved afresh.

## Goals in plain language

RELAY takes **any** goal, breaks it into steps, performs the ones it has tools for, and reports the
rest as a checklist. Planning is general; acting is not.

```bash
.venv/Scripts/python.exe scripts/demo.py "plan a weekly study timetable"
```

```
STOPPED: done
SUMMARY: Over to you:
  1. List all subjects or topics that require study time.
  2. Identify your fixed commitments for the week, such as classes or work.
  3. Allocate specific blocks of time for each subject in your weekly calendar.
  4. Include short breaks between study sessions to maintain focus.
  5. Review and adjust the timetable at the end of each week.
```

No email, no meeting, nothing to approve — and a real plan rather than a refusal. A **mixed** goal
does both:

```
GOAL: Email Rahul the Q3 report, book a 30-minute review next week,
      and prepare talking points before the meeting.

APPROVAL NEEDED - step 2: send_email        <- gated, as always
APPROVAL NEEDED - step 4: create_event      <- gated, as always
STOPPED: done
SUMMARY: Done: the email was sent, the meeting was booked.

         Over to you:
           1. Draft talking points based on the Q3 report before the meeting.
```

Note the report keeps those apart. "Done" is what RELAY did; "Over to you" is what it did not —
blurring the two is how an agent takes credit for work nobody performed.

Steps RELAY cannot perform become `note` steps: they appear in the plan marked **for you**, cost a
tool call (so the budget still bounds a long plan), and change nothing outside the process.

| Reader | Handles |
|---|---|
| `GeminiIntentReader` *(when `GEMINI_API_KEY` and `GEMINI_MODEL` are set)* | arbitrary goals, decomposed into real steps |
| `RegexIntentReader` *(offline fallback)* | the example goal and close variants; anything else becomes a single honest manual step |

Shapes it handles, each one a defect found by running varied goals through the live model:

- **A message with no attachment** — *"Tell Anu I'll be late"* becomes a subject and a body.
- **Several recipients** — *"send the Q3 report to both Rahul and Anu"*.
- **Several meetings** — *"two 30-minute catch-ups next week"* plans **two** bookings from one slot search.
- **Days named the way people name them** — *"on Thursday"*, *"next Thursday"*, *"tomorrow"*.
- **Goals with no tools at all** — a checklist, not a refusal.
- **Goals RELAY must refuse** — see below.

### Refusing beats reshaping

Not everything becomes a checklist. There are two different "cannot":

| Goal | Asks for | Result |
|---|---|---|
| *"Plan a weekly study timetable"* | a **plan** | steps, as a checklist |
| *"Delete all my emails from Rahul"* | an **action** RELAY must not approximate | **refused by name** |

The sharpest failure found by probing was the second one: it was read as **an email to Rahul**, and
the run proceeded to ask which Rahul. A model asked to describe any goal in terms of
`{email, meeting}` will describe a destructive request as the nearest thing it has a field for.

So `GoalIntent` has an `unsupported` field and the system instruction states plainly what RELAY
cannot do. A goal naming any of it is refused with the user's own words:

```
GOAL   : Delete all my emails from Rahul.
STOPPED: blocked
SUMMARY: RELAY cannot do that: Delete all my emails from Rahul.
         It can send one email and arrange meetings, nothing else.
```

The offline reader has a blocklist as a backstop, deliberately narrow: an earlier version listed
`call`, and *"set up a 45-minute **call** with Anu"* was promptly refused as a phone call. A list of
bare verbs cannot tell a noun from a verb, which is why the model with an explicit capability list
does the real work.

## Asking about past runs

Plan a 15-day workout on the 1st; on the 10th ask **"which day of my workout am
I on?"** and RELAY answers from what it planned, counting from the date it
planned it.

```
STOPPED: done
SUMMARY: You are on day 10 of your 15-day workout routine. The plan was made on
         Wed 1 Oct 2026, 9 days ago, which puts you in the cardio block.
```

There is no second store. `memory.py` reads the audit log, which has recorded
every goal, plan and tool call since the first run — built as evidence, and one
query away from being a feature (Lesson 47).

Three things it is careful about:

- **A question is not a goal** (Lesson 48). "what is my medication *schedule*?"
  must not be planned as a request just because it contains the word "schedule".
- **An answer is still a run.** It plans one `answer` step and one `finish`
  step, so it appears in the audit log like everything else rather than
  returning early and leaving no trace.
- **Not every run is a memory** (Lesson 49). A run only counts once it has
  recorded real work — a step written down, an email sent, a meeting arranged.
  Without that rule a question recalled *itself*, because the run row is written
  before planning starts.

A question never reaches the approval gate, because answering is not acting:
`answer` is not in `SIDE_EFFECT_TOOLS`.

## Fallback models

Free-tier models are overloaded often — three runs in a row died on *"experiencing high demand"*
during one testing session. `GEMINI_FALLBACK_MODELS` is a comma-separated list tried in order when
the primary fails:

```
GEMINI_MODEL=gemini-3.1-flash-lite
GEMINI_FALLBACK_MODELS=gemini-3.5-flash,gemini-3.5-flash-lite
```

**It falls back only on failures another model can fix.** Overloaded (503), out of quota (429) and
missing (404) are about *that* model, so the next one is worth a try. A **rejected key** (401/403)
fails identically everywhere, so it fails fast instead of turning one clear error into five slow
ones — measured at 1.3s with three models configured.

This is safe for the same reason the send is not retried: reading a goal has **no side effect**. The
rule from the retry lesson — never auto-retry a send, because a timeout may mean it already went —
does not apply to thinking.

**A quiet downgrade is reported**, because a weaker model writes a weaker plan:

```
READER : Gemini gemini-3.1-flash-lite, falling back to gemini-3.5-flash, gemini-3.5-flash-lite
NOTE: the primary model was unavailable; gemini-3.5-flash planned this run.
```

`scripts/check_setup.py` **probes** for usable fallbacks and prints a line to paste, because being
*listed* is not being *usable* — on this key `gemini-2.5-flash` is listed and answers 404.

## How the planner avoids making things up

The model is asked for **structure only** — is this an email, a meeting, how many minutes, which
phrase describes the window — and the schema it answers in (`GoalIntent`) contains no field for an
email address, a file id or a date. There is nothing to hallucinate into.

Everything risky is then resolved by tools: `find_contact`, `find_file`, `dates.resolve_window`.
More than one match is a **question**, never a pick by confidence — which is why the example goal
itself comes back as `needs_input` on a fresh fixture ("Q3 report" matches both the final and the
draft). Answering re-plans the same goal with one more entry in `Choices`, and an answer still has
to name a candidate a lookup returned.

`_assert_resolved_only` is the backstop: if an address or attachment id in a finished plan was
never returned by a lookup, planning fails rather than showing you a card to approve.

**You can give an address directly** — two ways, both treated as authoritative because they come
from you rather than from the model:

- **In the goal**: *"Email rahul.menon@example.com the Q3 report"* works, with no contact record needed.
- **As an answer**: when RELAY says *"I could not find a contact called 'Zoltan'. What is their address?"*, supplying one resolves it. (It asked that question for a long time and then ignored the answer, looping until the replan budget stopped the run.)

The two are told apart from a model-invented address by evidence: an address-shaped *name* is
accepted only if it **literally appears in the goal you typed**. So pasting an address works, and a
reader that invents `attacker@evil.example` is still refused — *"looks like an address but does not
appear in your goal"*.

One asymmetry worth knowing: if the directory found **no** candidates, your answer can be any
address. If it found **two**, your answer has to be one of them — a third address at that point is a
mistake, not new information.

Content RELAY reads goes into the prompt through `as_untrusted_data`, fenced and labelled, with the
closing marker stripped from the content so a document cannot end its own fence and have the rest
read as trusted prompt.

## The 15 scenarios and the eval report

```bash
.venv/Scripts/python.exe scripts/eval_report.py
```

Runs all 15 scenarios, the full suite and the mutation check, then writes
[EVAL-REPORT.md](EVAL-REPORT.md) and `eval-results.json`. Exit code is non-zero if anything fails,
so it can gate a submission.

The scenarios live in `relay/scenarios.py` as records — goal, answers, decisions, expected stop
state, and how many side effects are permitted. `tests/test_scenarios.py` and the report call the
**same runner**, so the suite and the report cannot disagree. Twelve come from the build plan's own
sentence; the last three cover the budgets and the gate, which are the project's own claims.

Current state: **15 of 15 pass, 0 unapproved side effects.** What that does not prove is in the
report's last section, and it is the important part — every number was produced against the mock.

## Verifying the tests have teeth

```bash
.venv/Scripts/python.exe scripts/mutation_check.py
```

It breaks five safety properties on purpose, each in its own subprocess, and requires the suite
to go red for each:

| Mutant | Sabotages |
|---|---|
| `no_side_effects` | declares nothing a side effect, so the gate never engages |
| `binding_is_a_noop` | stops resolving step references, so cards and tokens cover placeholders |
| `everything_looks_bound` | claims every action is bound, so the executor stops refusing placeholders |
| `tokens_always_verify` | accepts any token for any action, voiding the hash binding |
| `replans_are_free` | removes the replan limit, so a rejection loop never stops |
| `busy_errors_mean_free` | ignores per-calendar errors, so an unreadable calendar looks free |
| `live_falls_back_to_mock` | silently mocks a misconfigured live run instead of refusing it |

Exit 0 means all seven were caught; exit 1 names the survivors. One did survive the first time I ran
it, and the mutant was wrong rather than the tests — worth knowing that a survivor needs diagnosing
in both directions.

## Late binding: resolve, then hash, then show

A plan's send step does not contain a draft id, because no draft exists when the plan is made. It
carries a typed `StepRef` — "the `draft_id` from step 1". This is the single most important ordering
in the codebase:

1. `bind_action` resolves every reference against real step results.
2. The card is built from the **bound** action, so the user reads the actual recipient, attachment and slot.
3. `mint_token` hashes **that same bound object**.

Reverse 2 and 3 and the gate still passes all its tests while guaranteeing nothing: a token hashed
over `"<from step 1>"` would authorise sending *any* draft. `Executor.run_action` refuses an unbound
action outright, so the unsafe path is not merely avoided — it is unreachable.

## The UI

One HTML file at `relay/static/index.html`, served from `/`. No framework and no build step, so
there is nothing between the page and the endpoints it calls.

The design language is borrowed from the MIT-licensed
[print studio template](https://github.com/8bsite/print-studio-website-template): two flat inks
(ultramarine `#11138d` and fluorescent pink `#ed008e` on paper white), full-width colour bands and
halftone dots drawn in CSS. The piece that actually earns its place is
the **perforated job ticket** — an approval card *is* a job ticket: a run priced out, waiting for a
signature. The scallop is the template's own mask, reused unchanged.

**The type is monospace throughout** — one family, `JetBrains Mono`, with weight carrying the
contrast that separate display and body faces usually carry.

That is a look, but on this screen it is also a reading decision: this is where someone checks an
address and a timestamp *before* authorising a real send. Monospace helps there — `l`, `1` and `I`
are plainly different, the zero is dotted, and columns of times line up. What it costs is width, so
display sizes come **down** rather than up, and large text gets negative tracking to close the gaps
a fixed advance leaves. Uppercase labels get wide tracking, which is the thing this style does best.
The halftone dots are square to match; round dots beside monospace read as a different system.

**The two bands answer different questions, so they never share content.** The blue plan band is
**RELAY's work** — the steps it runs, which ones are done, which need approval. The paper outcome
band is **your work** — the checklist. `note` and `finish` steps are filtered out of the plan,
because the first belongs to you and the second is how a run ends rather than a thing anyone does.

Before that split, a goal with no tool steps listed its five steps *twice*: once in the plan marked
"for you", and again under "Over to you". When RELAY has no work of its own, the plan band hides
entirely and the goal box stays visible, so the checklist has something on screen explaining what it
is for.

The client is one function: `render(status)`. Every decision about what happens next belongs to the
server, so the page only draws what it is handed. Anything branched in JavaScript would be logic the
tests and the terminal demo never exercise.

**The two edits it offers are narrow, on purpose.** A free-text action editor cannot produce a valid
`Action`, and a form over raw `args` would expose internals nobody should have to read. So:

- **Pick a different slot.** The search already found several; showing only the proposed one would be hiding them. Choosing another is an *edit*, so it gets a new hash and goes back for approval.
- **Rewrite the email.** Editing cannot patch an approved action in place, because the *draft* is what gets sent. `POST /redraft` builds a new draft (read-only, no token, still audited), and the new draft id changes the hash — which is exactly why the edited card must be approved again.

Anything else is a rejection with a note.

## The live backend

`GoogleBackend` implements the same eight methods as the mock, so the loop cannot
tell them apart. Three things shaped it:

**Pure functions first.** Nothing here can be tested against the real APIs without
an account, a consent screen and a network. So everything that can be wrong on its
own — escaping a Drive query, building a MIME message, reading a free/busy
response, deciding a send landed — is a plain function taking dicts and returning
dicts, tested directly. The class is a thin wrapper. That split is not a testing
trick: a mangled query or a misread response is far likelier than `files().list()`
itself failing.

**An error is not an empty result.** Google reports per-calendar errors *inside a
200 response*: ask about a calendar you cannot see and you get
`{"errors": [...], "busy": []}`. Read that carelessly and an inaccessible calendar
is indistinguishable from a free one, and RELAY books over someone's day. `parse_busy`
raises unless the calendar is present, error-free and one we asked about.

**The narrowest scopes that work.** `gmail.compose` and `gmail.metadata`, not
`gmail.modify`; `calendar.events` and `calendar.freebusy`, not `calendar`;
`drive.readonly`, never a write scope. Nothing in the token can delete anything.
A token that cannot delete mail cannot delete mail *because of a bug*.

Live is never a fallback: `RELAY_BACKEND` defaults to `mock`, a misspelt value is
an error rather than a quiet mock, and a token file lying around does not turn it
on. In live mode the UI carries a black-and-pink banner, because someone
approving a send deserves to know it will reach a real inbox.

**[GOING-LIVE.md](GOING-LIVE.md)** is the runbook, including what the doubles
cannot prove.

## Deploying the demo: a note before you pick a host

The build plan's stack table says **Vercel**. That does not fit this design, and the mismatch is
worth settling before the deploy rather than during it.

Two pieces of state assume one long-lived process:

- `_RUNS` in `api.py` is an in-process dict. A run created by `POST /run` lives in that instance's memory, so a later `POST /decide` served by a different instance answers `404 unknown run`.
- The audit log is a SQLite **file**. Serverless filesystems are read-only apart from `/tmp`, which is neither shared between instances nor durable.

An approval flow is by definition several requests with a human pause in the middle, which is the
one shape serverless handles worst.

Two ways out:

1. **Host one persistent process** — Render, Fly, Railway, or any container. No code change at all, and the demo behaves exactly as it does locally. This is the recommendation for a demo.
2. **Make the run state durable** — rehydrate the loop from the audit log on each request. The plan, the step results, the cursor and the answers would all have to be persisted and replayed. That is real work, and it is the right answer for production rather than for a demo link.

If the Vercel line has already gone to the client in the plan, that stack row needs correcting.

## Three backends, one protocol

`RELAY_BACKEND` picks one. All three implement the same nine methods, so the planner, the loop,
the approval gate and the UI are identical across them.

| | What it needs | What it does |
|---|---|---|
| `mock` *(default)* | nothing | fixtures; sends nothing |
| `local` | **one Brevo API key** | real email via Brevo, attachments from `documents/`, contacts from `contacts.json`, meetings as `.ics` invitations |
| `live` | Google Cloud + OAuth | the full Gmail / Calendar / Drive / People stack |

### The `local` route, and the one claim it weakens

It exists because Google Cloud setup is a wall for a demo. One API key, no OAuth, no consent screen:

- **`find_file`** — a local `documents/` folder. The filename *is* the id, so there is nothing to register. Two files matching "Q3 report" keep the ask-rather-than-guess path reachable.
- **`find_contact`** — a local `contacts.json`.
- **`send_email`** — Brevo, 300/day on the free tier. Attachments are read straight off disk.
- **`create_event`** — an `.ics` invitation, emailed. "Book the meeting" becomes "send the invitation", which is arguably the more honest thing for an agent to do. Guests get `METHOD:REQUEST` and see Yes/Maybe/No; the organiser gets a `METHOD:PUBLISH` copy so the meeting reaches *their* calendar too. Both renderings were tested on real Gmail accounts, because a `REQUEST` addressed to its own organiser is **delivered and then never shown** — it appears in neither the inbox nor the calendar.

**`find_free_slots` is the weakened one, and it is weakened badly enough to say twice.** There is no
way to read a calendar here — Google's CalDAV does not offer free/busy, and the REST endpoint needs
OAuth. So this backend **proposes** times inside working hours; it does not **find** times that are
free. `LocalBackend.proposes_only` is `True` so a UI can say so. The approval card is the only thing
between a proposal and a clash, which makes the human approval *more* load-bearing here, not less.

That is a change to the build plan's Done criteria ("find 30-minute slots next week when all three
of you are free"), and a client decision rather than an implementation detail.

Verification is weaker too, and the first real send measured how much. Gmail's check was "is it in
Sent", a fact about a mailbox. Brevo's is "has an event been recorded", a fact about an asynchronous
queue. Immediately after a successful send the events feed was **empty** and the aggregate report
read `requests: 0`; a minute later it showed `requests` then `delivered`, about a second apart. The
send was real throughout — the only immediate signal was the account credit counter.

So `message_in_sent` falls back to the acknowledged `messageId`, and `done` here means **accepted by
Brevo**, not **delivered**. The code and the lessons both say so.

## What is deliberately missing

- **A live run.** The code is written; nothing in this repo has ever sent an email. It needs the test account and OAuth consent (lifecycle stage 2, "Kickoff") — see [GOING-LIVE.md](GOING-LIVE.md).
- **The UI.** The endpoints and the terminal demo are there; there is no front end.
- **A real failure and a real timeout.** Scenarios 8 and 9 pass, but they are *staged* with `fail_always` and `fail_on`. The real versions need the live backend — and the timeout one matters most, because a timeout on a send is ambiguous: the message may already have gone.
- **A paid Gemini key, if you want the plan's model.** `gemini-4-argon` is real and the build plan's stack table is correct — but its *free-tier* quota is zero, so a free key gets a 429 phrased as rate limiting. `gemini-3.1-flash-lite` works on a free key and plans the example goal correctly. `scripts/check_setup.py` makes a real one-token call, because being listed is not being usable.

## Teaching notes

`LESSONS.md` walks the seven design decisions in build order, with exercises. Each source file
carries the matching `LESSON n` header.
