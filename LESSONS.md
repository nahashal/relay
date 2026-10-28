# RELAY, in the order the decisions matter

Seven lessons, each matching a `LESSON n` header in the source. Read them in order; the later
ones only make sense because of the earlier ones.

---

## Lesson 1 — Plans are data (`relay/schemas.py`)

The temptation is to let the model answer in prose and parse it later. Resist it, because
everything downstream needs a *stable* object:

- the approval card shows the user a specific recipient and slot,
- the token is minted from a hash of that object,
- the audit log replays it.

Prose has no stable hash. The moment plans are Pydantic models, "approve this exact action"
becomes computable.

Note the two small design choices that do real work:

- `StopState` has no `RUNNING` member. A function that returns one is announcing the run is over.
- `SIDE_EFFECT_TOOLS` is defined **once**. Every "does this need approval?" question in the codebase reads that one set, so the answer cannot drift between files.

**Exercise 1.** Add a `cancel_event` tool. Which single line decides whether it needs approval?
Add it, then run `pytest -k gate` and notice that nothing else needed changing.

---

## Lesson 2 — The audit log is a safety device (`relay/audit.py`)

Two of the project's promises are really just SQL:

```sql
-- "a retry can never send twice"
SELECT 1 FROM tool_calls WHERE run_id=? AND action_hash=? AND token IS NOT NULL AND outcome='ok';

-- "zero unapproved side effects"
SELECT * FROM tool_calls WHERE tool IN ('send_email','create_event') AND token IS NULL;
```

That is why the row is written **before** the send is attempted, not after it succeeds. A crash
between the two leaves evidence; a log written afterwards would not.

`spend_approval` is worth staring at. The `WHERE token=? AND spent_at IS NULL` clause means two
concurrent attempts to spend one token cannot both win — SQLite settles it, not our code.

**Exercise 2.** Write the second query above as a test asserting it returns zero rows after a full
run. That is the headline number in the eval report.

---

## Lesson 3 — Hash the action, do not trust a flag (`relay/approvals.py`)

A prompt saying "always ask first" is a *request to a model*. A token minted from
`sha256(tool + args)` is a *lock*.

Change the recipient after approval and the hash changes, so the old token no longer verifies.
That is the whole "any change after approval voids it" rule — four lines of `hmac`, no model
cooperation required.

Two details that are easy to get wrong:

- `json.dumps(..., sort_keys=True)` — otherwise the same action hashes differently depending on dict order, and a valid approval would randomly fail.
- `hmac.compare_digest`, not `==` — a plain comparison leaks how much of the token is right via timing.

**Exercise 3.** Delete `sort_keys=True` and run `pytest -k token`. The tests may still pass —
which is the point: this is a bug that hides until production. How would you test for it?

---

## Lesson 4 — The protocol is the promise (`relay/tools/base.py`)

The build plan promises Outlook can be added "without touching the agent loop". That promise is
only kept if the loop never imports `googleapiclient`. So the loop depends on `Backend`, and
`MockBackend` / `GoogleBackend` implement it.

The same seam buys three things for free: fast tests, dry-run mode, and an honest interface —
anything the mock cannot express is something the loop should not assume.

**Exercise 4.** Sketch `OutlookBackend` as a class with `...` bodies. If you need to change
anything in `executor.py` to make it fit, the protocol is wrong, not Outlook.

---

## Lesson 5 — Write the mock before the real thing (`relay/tools/mock_backend.py`)

Of the 15 test scenarios, eleven are failure or ambiguity cases: two Rahuls, no matching file,
several matching files, no free slot, an API failure mid-run. Staging those against a live Google
account is slow and unreliable. Staging them in a fixture file takes a line each.

The fixture is built so the awkward path is the *default*: `find_contact("Rahul")` returns two
people, and `find_file("Q3 report")` returns two files. A planner that guesses will fail the
happy-path test, not some obscure edge case.

`fail_on={"send_email"}` raises once and then clears, which is exactly the retry-after-timeout
scenario.

**Exercise 5.** Make `find_free_slots` honour a `prefer_mornings` flag without changing its
signature for existing callers. Then ask whether the agent should be choosing the slot at all, or
offering three.

---

## Lesson 6 — One door (`relay/executor.py`)

There is exactly one function in RELAY from which a side effect is reachable. That is what makes
"zero unapproved actions" testable instead of aspirational: there is one place to audit, and a
future tool that bypasses it is visibly a bug.

The order of checks is not arbitrary:

1. **budget** — refuse before doing work, so a runaway loop costs nothing
2. **gate** — no matching token, no backend call
3. **replay** — already succeeded in this run? Return the first result
4. **spend** — burn the token before executing, so a crash mid-send cannot leave a live token
5. **execute** — only now does anything leave the process
6. **verify** — read the state back; a 200 is not proof

Swap 4 and 5 and you have a real bug: a process killed during `send_email` leaves a token that
still works, and the retry sends a second email.

**Exercise 6.** Do exactly that swap and write the test that catches it. (Hint: `fail_on`.)

---

## Lesson 7 — The API shape enforces the gate too (`relay/api.py`)

Look at what is *absent*: no endpoint takes a tool name and runs it. The client can only answer a
card RELAY offered, and the token was minted server-side. A hostile client can approve nothing
that RELAY did not itself propose.

`POST /run` raising `NotImplementedError` is deliberate. A stub that returns a plausible fake plan
is how a demo accidentally ships.

**Exercise 7.** Add `POST /answer` for the `needs_input` state. Who is allowed to answer, and what
stops an answer from arriving after the run already stopped?

---

## Lesson 8 — Don't let the model do arithmetic you can do exactly (`relay/dates.py`)

Ask a model what dates "next week" covers and you may get a confident wrong answer — and the error
is invisible on an approval card, because "Tue 10 Nov" looks exactly as plausible as "Tue 3 Nov".

Date resolution has one right answer, so it lives in Python, tested. The model is only asked which
*phrase* the user used.

The off-by-one worth knowing: ISO weeks end on **Sunday**, so on a Sunday `_monday_of(today)` points
at the Monday just gone, and "next week" is the Monday two days away — not nine. My first version
special-cased Sunday and got it wrong in the other direction; the test caught it.

**Exercise 8.** Add "the week of the 16th". What makes this harder than "next week", and at what
point should the planner give up and ask instead?

---

## Lesson 9 — Resolve with tools, not with the model (`relay/planner.py`)

This is the most important idea in the project, and it is a split:

| | Who does it | Can it be wrong? |
|---|---|---|
| "this is an email with an attachment and a 30-minute meeting" | the model | yes, and you'd notice |
| "Rahul means rahul.menon@…" | `find_contact` | no — it came from the directory |
| "next week means Mon 2 Nov" | `dates.py` | no — it's arithmetic |

`GoalIntent` has no field for an email address, a file id or a date. There is literally nothing for
the model to hallucinate into. Then `_assert_resolved_only` checks the finished plan and refuses any
address or attachment a lookup never returned.

The rule for ambiguity is blunt on purpose: **more than one match is a question, not a choice.**
Not "pick the newest", not "pick the one in Finance". This is why the example goal from the build
plan comes back as `needs_input` against a fresh fixture — two files are called "Q3 report". That
surprised me when the test failed, and it is the correct behaviour.

**Exercise 9.** Make the planner prefer an exact filename match when there is exactly one, and ask
otherwise. Then argue the other side: is "Q3 report FINAL.pdf" really what someone means by "the Q3
report", if a newer draft exists?

---

## Lesson 10 — Content is data, never instructions (`as_untrusted_data`)

Everything RELAY reads — a file body, a fetched email, a calendar description — goes into the prompt
fenced in `<untrusted_data>` markers, and the system instruction says plainly that text inside them
describes the world and is never a request.

The detail that makes it hold: the closing marker is **stripped from the content**. Otherwise a
document containing `</untrusted_data>` ends the block early, and everything after it is read as
trusted prompt. That is the delimiter-escape attack, and it is one line to prevent.

Fencing is necessary, not sufficient. The real defence is structural: the planner cannot act on a
recipient that `find_contact` did not return, so even a perfectly persuasive instruction inside a
document has no address to send to.

**Exercise 10.** Write a document that tries to get RELAY to email someone who *is* in the
directory. Which layer stops it now, and is that the layer you'd want to rely on?

---

## Lesson 11 — A question needs a way back in (`Choices`)

Asking "which Rahul?" is only useful if the answer can re-enter planning. `Choices` is that door,
and `plan(goal, choices)` is a pure function of the two — so resuming a `needs_input` run is just
re-planning with one more answer, not a mutable half-built plan sitting in memory.

The constraint that keeps it safe: an answer still has to name something a lookup returned.
Otherwise "which file did you mean?" becomes a way to inject an arbitrary file id.

I did not design this up front. The happy-path test failed, I went looking for the bug, and the bug
was that there was no way to answer the question the planner was correctly asking.

**Exercise 11.** `Choices` is keyed by the name as the user wrote it. What breaks if the user writes
"Rahul" in one goal and "rahul" in the next?

---

## Lesson 12 — A placeholder must be machine-readable (`StepRef`)

The first planner emitted `{"draft_id": "<from step 1>"}`. It reads beautifully and is actively
dangerous: nothing can resolve it, and if it ever reached `mint_token` the approval would be bound to
the *placeholder string*. The same token would then authorise sending any draft at all.

A typed `StepRef` cannot be approved by accident, because binding it is the only way to get an action
the executor will accept. `Action.is_bound` is the check; `UnboundAction` is the refusal.

**Exercise 12.** `is_bound` looks one level into lists and dicts. Construct an action it would wave
through. Is the right fix a deeper walk, or a rule that arguments stay shallow?

---

## Lesson 13 — The loop is driven, not autonomous (`relay/loop.py`)

RELAY waits for a human twice: to answer a question, and to approve a side effect. A loop that ran to
completion by itself would have to *hold* those waits — a parked thread, a callback — and neither
survives a page reload or a restarted server.

So `advance()` never blocks. It runs as far as it can and returns **why it stopped**: a question, a
card, or a stop state. Every pause is a value, which is exactly what makes the five stop states
testable — `test_loop.py` has one test per state and never mocks the loop itself.

The small structural choice that keeps this readable: `_run_step` returns `None` for "that worked,
keep going", and a `RunStatus` for "we have paused or stopped". My first version had `_run_step` call
`advance()` recursively while `advance()` also looped, which worked and was very hard to follow.

**Exercise 13.** The loop keeps `results` in memory. What breaks if the server restarts mid-run, and
what would you have to write to `tool_calls` to survive it?

---

## Lesson 14 — Resolve, then hash, then show

The most important three lines of ordering in the project:

```python
action = bind_action(step.action, self.results)   # 1. resolve
card   = self._card(step.number, action)          # 2. show the bound action
token, digest = mint_token(self.run_id, action)    # 3. hash that same object
```

Swap 2 and 3 — hash before resolving — and every test in `test_approval_gate.py` still passes while
the gate guarantees nothing. That is why `binding_is_a_noop` is one of the five mutants: it is the
failure this ordering exists to prevent, and it must be caught by something.

**Exercise 14.** Write the test that would catch the swap directly, rather than via the mutant. What
has to be true of the recorded `action_hash`?

---

## Lesson 15 — The API shape enforces the gate too (`relay/api.py`)

Look at what is *absent*: no endpoint takes a tool name and runs it. A client can only answer a
question the loop asked or decide a card the loop offered, and the token behind that card was minted
server-side over a bound action. A hostile client can approve nothing RELAY did not propose, and can
approve it only in the form RELAY showed.

The handlers are deliberately thin — translate HTTP to a loop call and back. Any decision made in a
handler instead of in `loop.py` is a decision the terminal demo and the tests do not exercise.

`/audit/{run_id}` returns `unapproved_side_effects` as a number. The eval report's headline claim
should be a thing you can curl, not a sentence in a document.

**Exercise 15.** Two browser tabs decide the same card at once. Which layer stops the second one, and
what does that client see?

---

## Lesson 16 — A green suite is not evidence (`scripts/mutation_check.py`)

All 25 tests passed the first time I ran them, which is precisely when to be suspicious: a suite can
pass because its assertions are vacuous. So break each safety property on purpose and require the
suite to go red. Five mutants now, each in its own subprocess — mutations applied in one process
would stack, and a failure would not be attributable.

It has earned its keep twice:

1. It showed the duplicate-send index was firing **after** the send rather than before it, because a row is only marked `ok` once the tool has already run. The guard could notice a double send but never prevent one. It is now a conditional `INSERT … WHERE NOT EXISTS`.
2. `replans_are_free` **survived** on the first run — and the mutant was wrong, not the tests. It patched `AgentLoop.max_replans`, but `new_loop` passes that value explicitly, so the instance attribute won. A surviving mutant needs diagnosing in both directions.

**Exercise 16.** Add a sixth mutant that makes `_verify` always return `True`. Which tests catch it?
If none do, what does that say about what "done" currently means?

---

## Lesson 17 — Prose is not an interface (`Question`)

Open questions started as plain strings. They read well in a terminal and are useless to a browser:

> Which Rahul did you mean? I found: Rahul Menon (Finance), Rahul Shetty (Design team).

A UI cannot turn that back into a `Choices` payload without parsing English. So a question now also
carries `kind` (which map to answer under), `key` (the name as the user wrote it) and `options` (the
candidates the lookup returned, each a label and a value). The prose stays as `text`.

Both are built in one place, so they cannot drift apart — and the distinction `options == []` is
what tells the UI to show a text box instead of radio buttons.

This is the shape of most UI work on an agent: the backend already knew the answer, it was just
flattening it into a sentence on the way out.

**Exercise 17.** The date question has no options. What would it take to offer some, and would
guessing three candidate windows be helpful or presumptuous?

---

## Lesson 18 — Borrow the structure, not the pixels

The design came from a print studio template: two inks, full-width bands, halftone dots, a job
ticket with a perforated edge. Almost none of that template was *about* an approval UI, and copying
its sections would have produced a brochure with buttons.

What transferred was one idea that happened to fit exactly: **an approval card is a job ticket.** A
run, priced out, waiting for a signature. The scalloped `mask` is the template's own CSS, reused
unchanged, because the metaphor was already right.

What did not transfer: the scroll animations, the pinned sections, the GSAP dependency. An approval
screen is read once and acted on; motion that rewards scrolling would be in the way of a decision.

**Exercise 18.** The template's halftone dots mark progress between steps here. Find one more of its
devices that maps onto something RELAY already does — and one that would be decoration.

---

## Lesson 19 — Keep the client dumb

`render(status)` is the whole UI. The server returns what it is waiting for, and the page draws
that. There is no client-side state machine, no optimistic update, no "if the last step was a send,
then…".

The payoff shows up in the awkward cases. When a run stops as `blocked`, the page needs no special
handling — the same `render` sees `awaiting: "nothing"` and a stop state, and draws the stamp. When
a slot is edited, the server sends back an `approval` status with a different card, and the page
redraws. Neither path required UI code.

It also keeps one promise honest: the browser cannot approve anything the server did not offer,
because the browser has no idea what the next step is.

**Exercise 19.** Add a "Start again" that keeps the goal text. Does it belong in the client, or is
it a new endpoint?

---

## Lesson 20 — Shared behaviour belongs in one function (`relay/slots.py`)

Both backends have to answer "when are these three free for 30 minutes next week".
If each did its own arithmetic they would eventually disagree — the demo offering
a slot the live run rejects, or worse, the other way round.

So a backend's only job is **fetching busy intervals**. Turning those into
offerable slots happens once, in a pure function with no network and no clock.
Which means every slot test written against the mock is really a test of the code
the live backend runs — the refactor added coverage to `GoogleBackend` before a
line of it existed.

One detail worth copying: `_at()` builds a wall-clock time from parts in a real
timezone, rather than parsing a string with a fixed `+05:30`. An offset that is
right in November is wrong in July, and a meeting booked an hour out is exactly
the error nobody spots on an approval card.

**Exercise 20.** Attendees in two timezones. Whose working hours win, and is that
a question for the code or for the user?

---

## Lesson 21 — Ask for the narrowest scopes that do the job (`google_auth.py`)

It is tempting to request `gmail.modify` and `calendar` and stop thinking about it.
The reason not to is the same reason the approval gate exists: **RELAY should not
be *able* to do what nobody approved.**

A token that cannot delete mail cannot delete mail because of a bug, a bad plan,
or a prompt injection that gets further than it should. So: `gmail.compose` and
`gmail.metadata`, not `gmail.modify`. `calendar.events` and `calendar.freebusy`,
not `calendar`. `drive.readonly`, never a write scope. No `gmail.readonly` at all
— RELAY never reads the inbox.

This is defence that costs nothing at the time and is awkward to retrofit, because
widening a scope later means every test user re-consents.

**Exercise 21.** A new tool needs to cancel an event. Which scope does that need,
and what else does that scope let a bug do?

---

## Lesson 22 — Separate the request from the reasoning, or you cannot test it

Nothing in `google_backend.py` can be tested against the real APIs without an
account, a consent screen and a network. That could have meant "this file is
untested until kickoff". Instead:

- escaping a Drive query → `drive_query`
- building a MIME message → `build_message`
- reading a free/busy response → `parse_busy`
- deciding a send landed → `was_sent`

…are plain functions taking dicts and returning dicts. The class is a thin wrapper
that calls an API and hands the result to one of them. 40 tests, no credentials.

The split is not a testing trick — it is **where the bugs live.** `files().list()`
failing is unlikely; a mangled query or a misread response is not. The apostrophe
test is the concrete case: "Tom's copy" would have ended the single-quoted Drive
literal early.

**Exercise 22.** `_call` catches `HttpError` and maps it to `ToolError`. Which
HTTP statuses should be retried instead of reported, and whose decision is that —
the tool's or the loop's?

---

## Lesson 23 — An error is not an empty result

The sharpest edge in the whole project. Google's freebusy reports per-calendar
errors **inside a 200 response**:

```json
{"calendars": {"rahul@…": {"errors": [{"reason": "notFound"}], "busy": []}}}
```

The obvious code reads `busy` and moves on. That code cannot tell "Rahul is free
all week" from "I cannot see Rahul's calendar" — and books over his day.

`parse_busy` raises unless the calendar is **present**, **error-free**, and **one
we asked about**. Empty means free; anything else means we do not know, and not
knowing is not free. The mock has the same rule for an unknown attendee, which is
how the shape was agreed before the live code existed.

This generalises past calendars: any API that reports partial failure in a success
response has this trap, and the default reading is always the dangerous one.

**Exercise 23.** Find another place in RELAY where a missing value and a failed
lookup currently look the same.

---

## Lesson 24 — Make the dangerous mode the one you ask for (`backends.py`)

`RELAY_BACKEND` defaults to `mock`. Nothing falls back to live, infers it from a
token file lying around, or decides credentials present means live intended.

And the failure mode is deliberately noisy in both directions: `RELAY_BACKEND=liv`
raises rather than quietly mocking (which would waste a demo), and a live backend
that cannot build returns 503 rather than silently mocking (which would waste
something worse — the trust of whoever was told the email went out).

The UI earns its part of this: a black-and-pink banner, the only element on the
page in neither of the two inks. Someone approving a send deserves to know it will
reach a real inbox *before* they click.

**Exercise 24.** The token expires after 7 days in Testing mode. Where should that
surface — at startup, on the banner, or when a run fails?

---

## Lesson 25 — A type change is a legibility change

The page went pixel, then monospace. Both are looks; on this screen both are also decisions about
**reading**, because this is where someone checks an address and a timestamp before authorising a
real send. A misread character is not a cosmetic bug.

Each switch came with a budget of adjustments, and they pull in opposite directions — which is the
lesson. Picking a face is not the end of the work, it is the start of it:

| | pixel (Pixelify / Press Start 2P / VT323) | monospace (JetBrains Mono) |
|---|---|---|
| body size | **up** — small apparent x-height | **down** — tall x-height, runs wide |
| display size | normal | **down** — a mono headline is far wider |
| letter-spacing | never negative; gaps are structural | negative on large text only |
| smoothing | **off** — glyphs drawn on a grid | **on** — outline face, aliasing is just ragged |
| faces needed | three, by job | one, weight does the rest |

The monospace version is the better fit for what this screen actually is. `l` / `1` / `I` are plainly
distinct, the zero is dotted, and the evidence row sets in tabular numerals so the counts line up in
a column. None of that is decoration on a page whose job is checking details.

The layout bugs the pixel pass flushed out are worth keeping in mind, because they were **latent
before any font changed** and the wider glyphs merely exposed them: a long address pushed the "needs
approval" label clean out of its grid row (a grid child will not shrink below its content without
`min-width: 0`), the goal textarea clipped its third line, and the header counters crowded the
wordmark at 375px. None were type bugs. They were sizing assumptions that happened to hold at the
old widths — and they stayed fixed through the second switch, which is how you know the fix was
right rather than lucky.

**Exercise 25.** The two ink colours survived both type changes untouched. Why did the palette carry
over when none of the type did?

---

## Lesson 26 — What may be retried is decided by the side effect, not the error

The build plan lists "a retry after a timeout" as a scenario, and writing that scenario found that
the loop had **no retry at all**: every `ToolError` went straight to `blocked`. The obvious fix is a
retry loop. The obvious fix is wrong.

A failed *lookup* can be repeated freely — worst case we read the same calendar twice. A failed
*send* cannot, and the reason is specific: the failure that matters most is a **timeout**, which is
exactly the case where the message may already have gone. An automatic retry there risks the one
thing this entire project exists to prevent.

So:

- read-only tools: retried once, automatically. Each attempt costs a tool call, so the budget still bounds it.
- side-effect tools: never retried automatically. The run stops with a message saying so in plain words — *"it may already have happened. Check, then approve again if it did not."*

Note which way the asymmetry runs. The safe-looking choice (retry everything, be resilient) is the
dangerous one, and the conservative choice produces a worse-looking demo — a run that stops and asks
a human. That trade is the whole product.

The test that proves this is not vacuous is the one asserting `retries_used == 1`: without it,
scenario 9 would pass just as happily if the staged failure never fired.

**Exercise 26.** `find_free_slots` is read-only, so it retries. But a retry after a *successful*
`create_event` would see different availability. Does that matter, and where would you check?

---

## Lesson 27 — Acceptance criteria belong in the code, not the document

The build plan names the 15 scenarios in one sentence of prose. Left there, two things go wrong:
"13 of 15 pass" is a claim someone has to take on trust, and the prose drifts from what the tests
actually do.

`relay/scenarios.py` makes each one a record — goal, answers, decisions, expected stop state, and
how many side effects are permitted. `tests/test_scenarios.py` runs them as tests;
`scripts/eval_report.py` runs **the same records through the same runner** to produce the report.
A scenario therefore cannot pass in the suite and fail in the report.

Two details worth copying. First, every scenario asserts `unapproved == 0` regardless of what else
it is testing — the headline claim is checked fifteen times, not once. Second, there is a test that
*deliberately breaks* a scenario and asserts the runner reports the failure, because a harness that
cannot fail makes every green result meaningless.

Writing the records also exposed that three of the fifteen had no home in the plan's sentence. Rather
than pad the list, they became the project's own claims: the tool-call budget, the replan limit, and
the gate. The report labels which is which.

**Exercise 27.** Scenario 12 books a meeting with no approval decision listed, so the runner
approves by default. Is that a convenience or a hole in the harness?

---

## Lesson 28 — A report you write by hand is a claim; one you generate is evidence

`EVAL-REPORT.md` has no hand-typed numbers in it. The scenarios come from running them, the test
count from pytest, the mutant count from `mutation_check.py`. Regenerate it and it tells the truth
about the code as it is now, which a hand-maintained document stops doing the moment anything
changes — and the first person to notice is the client.

The generator exits non-zero when anything fails, so it can gate a submission rather than being a
document someone remembers to update.

The section that took the most care is the one headed **"What this does NOT prove"**, and it is the
one worth writing first next time. Fifteen of fifteen passing looks like completion. It is not: every
number was produced against `MockBackend`, and two of the scenarios simulate the failures they claim
to cover. A report that leads with "15 of 15" and buries that is a report designed to be misread.

**Exercise 28.** The report counts emails sent across all scenarios (6) and events booked (5). What
would you have to add to make those numbers meaningful to a client rather than just internally
consistent?

---

## Lesson 31 — An attachment can be an action (`relay/ics.py`)

Without the Calendar API there is no way to write to someone's calendar. But there is a way to
**ask**: an `.ics` file carrying `METHOD:REQUEST` is not a document, it is an invitation. Gmail and
Outlook render it with Yes / Maybe / No, and accepting puts the event on the recipient's own calendar.

So "book the meeting" becomes "send the invitation" — which, for an agent, is arguably the more
honest act. It asks the attendees rather than writing into their day.

Three details that decide whether it renders as an invite or as a mystery attachment: `METHOD:REQUEST`,
`RSVP=TRUE` on each attendee, and CRLF line endings. Clients that insist on the last one fail
*silently*, showing the file instead of the buttons.

**Exercise 31.** Send the same UID again with `METHOD:CANCEL`. What does that let RELAY do, and which
tool would it become?

---

## Lesson 32 — Inject the transport, and the network stops being untestable (`relay/brevo.py`)

`BrevoClient` takes a `transport` callable. Production passes urllib; tests pass a function that
records the request and returns a canned response. That single parameter is why the auth header, the
base64 attachment encoding, the 401 handling and the unverified-sender message are all covered by
tests that touch no network and need no key.

Same idea as the Google service doubles, and worth noticing as a pattern: the boundary where bytes
leave the process is almost always the right place to put a seam.

**Exercise 32.** The transport returns `(status, json)`. What would you have to change to test a
timeout, and is that worth a different seam?

---

## Lesson 33 — Weaker evidence, honestly labelled

Gmail's verification was "is this message in Sent" — a fact about a mailbox. Brevo's is "has an event
been recorded for this messageId" — a fact about an asynchronous queue. A message that *will* be
delivered can be unknown for several seconds after a successful send.

So `message_in_sent` falls back to the acknowledged `messageId`, and both the docstring and the README
say plainly that this is a weaker guarantee than the Gmail one. The temptation is to let the two
backends look equally confident because they implement the same method. Resisting that is the whole
point: a protocol equalises *interfaces*, not *evidence*.

**Then the first real send measured it.** RELAY reported `done`; checked immediately afterwards,
Brevo's events feed held **zero** events and the aggregate report said `requests: 0`. For a minute
the only evidence that anything had happened was the account's credit counter dropping by one.
The feed then caught up:

```
17:00:59  requests   Q3 Report
17:01:00  delivered  Q3 Report
```

So the send was real and delivery took about a second — but RELAY had already said `done` before any
of that existed. The gap between "we were told it was accepted" and "it arrived" is small here and
completely invisible from inside the run. Worth knowing before anyone reads `done` as `delivered`.

**Exercise 33.** Brevo can call a webhook on delivery. What would it take to turn `done` into "actually
delivered", and where would the run have to wait?

---

## Lesson 34 — Say which claim you just weakened, in the code

Five of the six substitutions in `LocalBackend` are straight swaps: a file instead of an API, a
different mail provider. One is not.

`find_free_slots` cannot see anyone's calendar, because free/busy needs the Calendar API and Google's
CalDAV explicitly does not offer it. So this backend **proposes** times rather than **finding** free
ones. The method keeps its protocol name, every comment around it says "proposal", and
`proposes_only = True` exists so a UI can tell the user.

The consequence is worth stating: with the Google backend, the approval card confirms a time RELAY
*knows* is free. With this one, it is the only thing standing between a proposal and a clash. The
human approval becomes **more** load-bearing, not less — and that is exactly the sort of shift that
disappears if you only document the interface.

**Exercise 34.** The local backend could read the organiser's own busy times from a local file. That
would make proposals better for one of the three attendees. Is a half-informed proposal more useful
or more misleading than an openly uninformed one?

---

## Lesson 35 — An invariant that only holds for one backend is not an invariant

`_assert_resolved_only` is the planner's tripwire against a plausible-looking address that nobody owns.
It worked by listing the whole directory — `find_contact("")` and `find_file("")` — which **only
MockBackend** treats as "match everything". Both real backends refuse an empty query.

So the safety net raised `ToolError` the moment it met a real backend. It would have failed against
Google exactly as it failed against local files, and nothing caught it for several turns because every
test ran on the mock.

The fix is also the better design: record what the resolvers actually returned during *this* planning
pass and check the plan against that. No backend call, works everywhere, and it is a stricter claim —
"a lookup returned it just now" beats "it exists somewhere in the directory".

The general shape is worth remembering: **a safety check that has only ever run against a test double
has not been tested, it has been rehearsed.** The second backend is what found it; a third would
probably find more.

**Exercise 35.** The new invariant is nearly tautological today, because the resolvers are the only
path by which an address can enter a plan. Is a tripwire that cannot currently fire worth keeping?

---

## Lesson 36 — Probe the goal space, or you have built a demo for one sentence

The brief says *an agent that takes a goal in plain language*. The example goal is one case. For a
long stretch this project was quietly optimising for that one sentence, and it took seven varied
goals through the live model to find out. Four defects, in under a minute:

| Goal | What happened | Severity |
|---|---|---|
| *"Delete all my emails from Rahul"* | read as an **email to Rahul**; asked which Rahul | **serious** |
| *"two 30-minute catch-ups"* | booked **one**, said nothing | silent wrong answer |
| *"book an hour on Thursday"* | could not resolve a weekday | asked unnecessarily |
| *"Book a meeting room for Friday"* | a 0-minute meeting with no attendees | nonsense plan |

The first one is the lesson. A model asked to describe a goal in terms of `{email, meeting}` **will**
describe *anything* that way, because those are the only fields it has. The fix is not a better
prompt — it is giving the model somewhere honest to put a request: an `unsupported` field, and an
instruction that names what RELAY cannot do. **Refusing has to be representable, or the model will
reshape instead.**

The second is the quieter danger. Booking one meeting when two were asked for produces a plan that
looks entirely correct on the approval card. Nothing warns anyone. A count field and two booking
steps cost twenty lines.

And a fix can introduce its own bug: the offline blocklist I added to catch "delete" also caught
*"set up a 45-minute **call** with Anu"*, refusing a perfectly ordinary meeting. A blocklist of bare
verbs cannot distinguish a noun from a verb. The suite caught it immediately, which is the only
reason it was a five-minute detour rather than a demo failure.

**Exercise 36.** Write ten goals a real user might send. How many does RELAY handle, how many does it
refuse correctly, and how many does it reshape into something it can do?

---

## Lesson 37 — The data you need is usually already in hand

The first real email RELAY sent opened:

> **Hi Nahslll969,**

The body builder derived a name from the left half of the address and title-cased it. The fix people
reach for is a cleverer parser — strip digits, split on dots, handle `firstname.lastname`. All of that
is guessing, and all of it is unnecessary, because **`find_contact` had been returning the person's
actual name the whole time** and the resolver threw it away, keeping only the address.

So the fix was three lines of *remembering* rather than a parser: keep `address -> name` as the
lookups resolve, and greet from that. The failure was never "we could not work out the name"; it was
"we discarded it, then tried to reconstruct it".

Worth asking whenever a derivation looks hard: did something upstream already have this?

The rest is about knowing when not to guess. A name with a digit in it, or one character long, falls
back to "Hi there" — "Hi X7" is worse than greeting nobody. And if *any* recipient lacks a usable
name, everyone gets the neutral greeting, because "Hi Rahul and there" is the worst of both.

The same bug had a sibling: *"I have attached the report"* assumed every attachment was a report, and
the subject line was a flat "For your review". Both now name what was actually asked for. Fixture
data hid all of it — `rahul.menon@` yields "Rahul", so the address-derived name looked fine for
months. **One real send found what a hundred passing tests did not.**

**Exercise 37.** `_greeting` is all-or-nothing across recipients. Is that right for five people where
one is a shared alias like `team@`, or should the alias be dropped from the greeting instead?

---

## Lesson 38 — The user is allowed to answer; the model is not

A question exposed two defects at once: *does the person typing the goal specify the address?*

The intended answer was "no, they type a name and RELAY resolves it". Testing it found that the
intended answer was also the **only** answer, and both alternatives were broken:

1. *"Email rahul.menon@example.com the Q3 report"* — did not parse at all. `PlanningError: could not read an email or a meeting from this goal`. Pasting an address is an entirely natural thing to do.
2. Worse: RELAY asks *"I could not find a contact called 'Zoltan'. **What is their address?**"* — and then ignored the answer. The `if not matches: ask; return` ran *before* the code that reads a supplied answer, so it asked the same question every replan until the budget stopped the run. **A question you cannot accept the answer to is worse than no question at all**, because it promises a way forward that does not exist.

The fix needed a distinction, not just a code path. An address can reach the planner from the user
(authoritative — it is their contact) or from the model (never trusted — a plausible address nobody
owns is the signature failure of an LLM planner). Both arrive as a plain string.

They are told apart by **evidence**: an address-shaped name is accepted only if it literally appears
in the goal text the user typed. That keeps "the model cannot invent a recipient" exactly as true as
before, while letting someone paste an address. The case-insensitive comparison matters — readers
routinely lowercase an address, and that is not evidence of invention.

The asymmetry in the answer path is deliberate too. No candidates found: any address is accepted,
because the user knows something RELAY does not. Two candidates found: the answer must be one of
them, because a third address there is a typo rather than new information.

**Exercise 38.** The goal-text check is a substring match. Construct a goal where that accepts an
address it should not, and decide whether it matters.

---

## Lesson 39 — A provider failure is a stop state, not a traceback

Gemini returned `503 UNAVAILABLE` ("experiencing high demand"). That exception escaped
`planner.plan()`, sailed past the loop — which catches only `PlanningError` — and became an HTTP
500. The run ended in **no stop state at all**, which is precisely the guarantee the whole project
is built on.

Two things made it hard to see. The UI rendered nothing useful, so it read as "I clicked and nothing
happened". And every test passed, because every test used a reader that cannot fail.

The fix is the same shape as Lesson 23: an error from outside is a *result*, not a crash. Provider
exceptions now become `blocked` with a message a person can act on — overloaded, quota used up,
model is paid-only, key rejected — each saying "nothing was planned and nothing was sent", because
that is the first thing anyone wants to know when a run dies mid-goal.

Worth noting what this does NOT do: retry. The SDK already retries internally, so a 503 that reaches
us is a genuine outage, and Lesson 26 applies — the loop decides, the tool does not.

**Exercise 39.** Every test here uses a reader that raises on command. What would it take to test
the real SDK's behaviour without a network, and is it worth it?

---

## Lesson 40 — Planning is general; acting is not

The brief says *an agent that takes a goal in plain language, plans the steps, uses tools to send
email and schedule meetings*. I read the tool list as the scope of the whole agent, so
*"plan a weekly study timetable"* came back as:

> RELAY cannot do that. It can send one email and arrange meetings, nothing else.

That is a planner refusing to plan. The brief scopes what RELAY can **do**, not what it can **think
about**, and collapsing those two is the mistake.

The correction is a distinction, not a feature:

| Goal | Asks for | Right answer |
|---|---|---|
| *"Plan a weekly study timetable"* | a **plan** | break it into steps; none have tools; report them |
| *"Delete all my emails from Rahul"* | an **action** RELAY must not approximate | refuse, by name |

Both were landing in the same bucket. Splitting them took one new field (`manual_steps`), one new
step kind (`note`), and a system instruction that says plan anything, refuse only these verbs.

Three details that keep the guarantees intact:

- **Notes cost a tool call.** A thirty-step checklist hits the budget rather than running on, so "knows when to stop" still holds for goals with no actions in them.
- **The report keeps them apart.** "Done: the email was sent" and "Over to you: 1. Draft talking points" are separate sections. Folding a checklist into "done" is how an agent takes credit for work nobody did.
- **The refusal survives.** `unsupported` still wins over `manual_steps`, so a destructive request cannot be downgraded into a polite suggestion.

The general lesson: when a scope decision feels like it is about *capability*, check whether it is
actually about *two different verbs* — doing and planning — that happen to share a sentence.

**Exercise 40.** A note is inert: RELAY writes it and moves on. Should the run be able to pause on a
note and wait for the user to say they have done it, and what would that do to the stop states?

---

## Lesson 41 — Fall back on the failures another model can fix

Free-tier models are overloaded constantly. Three runs in a row died on *"this model is currently
experiencing high demand"* while testing, each one a dead end for the user.

A fallback list fixes that, but only if it is applied to the right failures. The distinction:

| Failure | Another model? | Why |
|---|---|---|
| 503 overloaded | **yes** | about this model's load right now |
| 429 quota | **yes** | about this model's allowance |
| 404 missing | **yes** | listed is not usable; the next may be |
| 401/403 bad key | **no** | the key is wrong for every model |
| anything unrecognised | **no** | unknown is not evidence a different model helps |

Getting the last two wrong turns one clear error into five slow ones. With a bad key and three
models configured, RELAY refuses in **1.3 seconds** and never mentions the other two.

Two things make this different from the retry rule in Lesson 26. Reading a goal has **no side
effect**, so "never auto-retry a send" does not apply — that rule is about ambiguity after a
timeout, and there is no ambiguity in a plan that was never produced. And a fallback is not a retry:
it is a *different* attempt, which is exactly why 404 belongs on the list and a plain retry would be
pointless.

The part worth copying is reporting the downgrade. A weaker model writes a weaker plan, so the run
says which model actually answered. Silently degrading quality is the kind of thing that is
invisible until someone wonders why the output got worse.

And the checker **probes** rather than recommends from a list: on this key `gemini-2.5-flash` is
listed and answers 404, so any suggestion built from the catalogue would have been wrong.

**Exercise 41.** The fallback list is fixed order. Should a model that just failed with 503 be
skipped for the next few minutes, and where would that state live?

---

## Lesson 45 — The organiser needs the invitation too

Without the Calendar API nothing can write to a calendar. The only route is a
mail client recognising an invitation and offering to add it. So the first
version of `create_event` excluded the organiser from the recipients - correct
for a real Google invite, where the organiser's copy is created server-side,
and exactly wrong here. **The person running RELAY was the one person whose
calendar never got the meeting.**

The fix is one line, and the lesson is about where it came from: a user asking
"can it save the meeting to my calendar?" - not from a test, because the tests
asserted the old behaviour was right.

**Exercise 45.** A meeting with only yourself is now allowed. What else in the
planner still assumes a meeting has guests?

---

## Lesson 46 — Measure the rendering; do not reason about it

Two real invitations, same second, same inbox:

```
10:32:39  delivered  RELAY test A - add to calendar (PUBLISH)
10:32:39  delivered  RELAY test B - invitation (REQUEST)
```

**A arrived and added to the calendar. B never appeared at all** - not the
inbox, not the calendar - despite Gmail reporting it delivered. A client can
accept a message and silently decide not to show it, which no amount of
RFC-reading would have predicted.

The test also had a flaw worth remembering: B listed the user as organiser AND
attendee, a shape that never occurs. A real meeting gives the organiser a
REQUEST where they are ORGANIZER with no ATTENDEE line of their own - an
invitation they are not invited to - which is, if anything, more likely to be
dropped.

So the two audiences now get different mail: guests a REQUEST they can RSVP to,
the organiser the PUBLISH rendering that was *observed* to reach a calendar. Two
sends instead of one.

Note what the evidence actually covers. The organiser's copy is proven, because
test A was exactly that rendering. The guest's copy is not - both tests went to
one inbox - it is simply the standard shape every calendar system sends.

**Exercise 46.** Verifying the guest copy needs a second real mailbox. What
would you check, and what would "it worked" look like from the sender's side?

---

## What to build next

**Verify the guest copy.** The organiser's rendering is measured; the guest's is
assumed. It needs a second real address.

**Keep probing.** Every round has found real defects, and the last several came
from the user trying it rather than from any test: Lessons 36-46.

**Then pick the backend for the deliverable:**

- `local` — one Brevo key, no Google Cloud. Real email delivered, invitations that reach a calendar. Meeting times are *proposed*, not *found*.
- `live` — the full Google stack. Written and unit-tested against doubles; has never made an API call.

**Then the deployed demo.** The plan's Vercel row does not fit, so one
persistent process is the cheap answer and durable run state the proper one.
