# Going live

How to point RELAY at a real Google account. This is lifecycle stage 2
("Kickoff") from the build plan, and it is the last thing blocking the final two
test scenarios.

**Do this on a throwaway test account, not a real inbox.** Everything below
grants a program the ability to send mail as you.

---

## 0. Check as you go

```bash
.venv/Scripts/python.exe scripts/check_setup.py
```

Run this after every step below. It is read-only — it lists models, reads your own profile and
counts files; it sends nothing. Each thing it finds wrong prints a `fix:` line with the exact next
action, so you never have to guess whether a step took.

## 1. Google Cloud project

1. Create a project at <https://console.cloud.google.com/>.
2. **APIs & Services → Library**, enable all four:
   - Gmail API
   - Google Calendar API
   - Google Drive API
   - People API
3. **OAuth consent screen**: External, in **Testing** mode. Add the test account
   as a test user. Testing mode is deliberate — a token issued under it expires
   after 7 days, which is a feature here: an abandoned demo stops working rather
   than staying live indefinitely.
4. Add the seven scopes listed in `relay/google_auth.py`. They are the narrowest
   set that covers the eight tools; see LESSON 21 for why that is not laziness.
5. **Credentials → Create credentials → OAuth client ID → Desktop app.**
   Download the JSON and save it as `credentials.json` in this folder.

## 1b. Gemini API key

Separate from everything above, and much simpler: <https://aistudio.google.com/apikey> → **Create
API key**. This is the key that lets RELAY *read the goal*; without it the planner falls back to a
regex that only handles the example phrasing.

```bash
$env:GEMINI_API_KEY="..."
```

You also need `GEMINI_MODEL`, and there is deliberately no default in the code.

The build plan's `gemini-4-argon` **does exist** — but its free-tier quota is **zero**, so on a free
key it returns a 429 worded as rate limiting ("retry in 21h") rather than "this model needs billing".
Several other listed models answer 404 or 503. `check_setup.py` therefore makes a real one-token
call rather than trusting the model list: being listed is not being usable.

`gemini-3.1-flash-lite` works on a free key and plans the example goal correctly, including loose
phrasing like *"shoot Rahul the Q3 report and grab 45 mins with him and Anu sometime next week"*.

## 2. Authorise once

```bash
.venv/Scripts/python.exe -m relay.google_auth
```

A browser opens, you consent as the test account, and `token.json` is written.
Both files are gitignored. The command prints the scopes actually granted —
check them against the list it asked for, because a scope silently declined
shows up later as a confusing 403.

## 3. Sample data on the test account

The 15 scenarios need the same shapes the fixture has:

- **Two contacts called Rahul**, in different departments, so "which Rahul?" is reachable. Workspace: set the Department field. Personal Gmail: there is no directory, so `searchDirectoryPeople` 404s and only personal contacts are used — the backend handles this, but the department note will read "no department listed".
- **Two files matching "Q3 report"** in Drive, e.g. `Q3 report FINAL.pdf` and `Q3 report draft.docx`.
- **An Anu** with a visible calendar.
- **Busy blocks** across Mon–Fri of the target week, leaving one or two common 30-minute gaps. Leave at least one, or every run ends `blocked`.
- **Someone busy all week**, to reach the no-slot case.

Free/busy visibility matters: if the test account cannot see a calendar, RELAY
will refuse rather than assume free (LESSON 23), which is correct but will look
like a failure if you were not expecting it.

## 4. Run it

```bash
RELAY_BACKEND=live RELAY_GOOGLE_ACCOUNT=you@example.com .venv/Scripts/python.exe -m uvicorn relay.api:app --port 8111
```

PowerShell:

```bash
$env:RELAY_BACKEND="live"; $env:RELAY_GOOGLE_ACCOUNT="you@example.com"; .venv/Scripts/python.exe -m uvicorn relay.api:app --port 8111
```

The UI shows a black-and-pink banner across the top in live mode. If you do not
see it, you are still on the mock — check `/mode`.

Other variables: `RELAY_TIMEZONE` (default `Asia/Kolkata`), `RELAY_DAY_START`,
`RELAY_DAY_END`, `RELAY_TOKEN_FILE`.

## 5. First live run

Use a goal addressed to **yourself**, and approve only the email step the first
time:

> Email me the Q3 report.

Then check: the message is in Sent with the attachment; `/audit/{run_id}` shows
`unapproved_side_effects: 0`; and the run ended `done` rather than `blocked`
(a `blocked` here usually means verification could not read the label back,
which points at a missing `gmail.metadata` scope).

---

## What is not proven until you do this

`tests/test_google_backend.py` tests this code with service doubles. It proves
the queries, the MIME, the parsing and the error mapping are right **given that
the APIs behave as assumed**. It cannot prove the assumptions. Specifically,
these are the places to look first if something behaves oddly:

| Assumption | Where it could bite |
|---|---|
| `drafts.send` returns the message id | If it returns the draft id, verification fails and every run ends `blocked` |
| `gmail.metadata` can read `labelIds` | Too narrow a scope → 403 → verification returns false |
| `searchContacts` needs a warmup call | Google documents an empty-query warmup; a cold first search may return nothing |
| freebusy reports errors per calendar | The whole no-assume-free rule depends on this shape |
| `sendUpdates="all"` invites attendees | If invitations do not arrive, the meeting exists but nobody was told |

Two of the 15 scenarios — a real API failure mid-send, and a real timeout retry
— can only be exercised here. Against the mock they use `fail_on`, which is not
the same evidence.
