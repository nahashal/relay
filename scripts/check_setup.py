"""Check the Gemini and Google setup, one step at a time, and say what is wrong.

LESSON 29 - make "did that work?" a command, not a judgement call.
Setting up two APIs means a dozen places to go slightly wrong: a missing env
var, a declined scope, an API not enabled, a model id that does not exist. Any
of them surfaces later as a confusing 403 or an empty result, usually in the
middle of a demo. So each step gets a check that either passes or says exactly
what to do about it.

Everything here is READ-ONLY. It lists models, reads your own profile, counts
files. It sends no email and books nothing.

Run it:  .venv/Scripts/python.exe scripts/check_setup.py
"""

from __future__ import annotations

import os
import time
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from relay.config import load_env  # noqa: E402

load_env()

OK = "  [ok]  "
NO = "  [--]  "
WARN = "  [!!]  "


class Check:
    """One step, its result, and what to do if it failed."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, message: str) -> None:
        print(f"{OK}{message}")

    def fail(self, message: str, fix: str) -> None:
        print(f"{NO}{message}")
        print(f"        fix: {fix}")
        self.failures.append(message)

    def warn(self, message: str, note: str) -> None:
        print(f"{WARN}{message}")
        print(f"        {note}")


def check_gemini(c: Check) -> None:
    print("\n--- 1. Gemini (reads the goal) ---")

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        c.fail(
            "GEMINI_API_KEY is not set",
            "get a key at https://aistudio.google.com/apikey, then put it in .env as"
            " GEMINI_API_KEY=... (no quotes, no spaces)",
        )
        return
    c.ok(f"GEMINI_API_KEY is set (ends ...{key[-4:]})")

    try:
        from google import genai
    except ImportError:
        c.fail("the google-genai package is not installed", "pip install -r requirements.txt")
        return

    try:
        client = genai.Client(api_key=key)
        models = [
            m.name.removeprefix("models/")
            for m in client.models.list()
            if "generateContent" in (getattr(m, "supported_actions", None) or [])
        ]
    except Exception as exc:  # the API itself told us something
        c.fail(f"the key was rejected: {exc}", "check the key, or that billing/quota is available")
        return

    if not models:
        c.fail("the key works but no usable models came back", "check the key's project and quota")
        return

    c.ok(f"the key works - {len(models)} usable models")

    wanted = os.environ.get("GEMINI_MODEL")
    if not wanted:
        c.fail(
            "GEMINI_MODEL is not set",
            "pick one of the models listed below and put it in .env as GEMINI_MODEL=...",
        )
    elif "," in wanted or " " in wanted.strip():
        # Pasting the whole suggestion list is an easy mistake, and the plain
        # "not in the list" message does not hint at the real problem.
        first = wanted.replace(",", " ").split()[0]
        c.fail(
            f"GEMINI_MODEL looks like a list, not one name: {wanted!r}",
            f"set exactly one, e.g. GEMINI_MODEL={first}",
        )
    elif wanted in models:
        # Listed is not usable. gemini-4-argon appears in the list with a
        # free-tier quota of ZERO, and some listed models answer 404 or 503.
        # The only way to know is to ask for one token.
        try:
            from google.genai import types

            client.models.generate_content(
                model=wanted,
                contents="Reply with the single word: ok",
                config=types.GenerateContentConfig(temperature=0, max_output_tokens=10),
            )
            c.ok(f"GEMINI_MODEL={wanted} answered a real request")
        except Exception as exc:
            text = str(exc)
            if "limit: 0" in text:
                why = "this model is paid-only on your key (free-tier quota is 0)"
            elif "429" in text:
                why = "quota is exhausted right now"
            elif "404" in text:
                why = "listed, but not actually available to this key"
            elif "503" in text:
                why = "the model is overloaded right now - try again shortly"
            else:
                why = text[:120]
            c.fail(f"GEMINI_MODEL={wanted} is listed but did not answer", why)
    else:
        c.fail(
            f"GEMINI_MODEL={wanted} is not in this key's model list",
            "the build plan names a model I could not verify - pick a real one below",
        )

    # --- fallbacks ---------------------------------------------------------
    # Listed is not usable, so the only honest way to recommend a fallback is
    # to try it. Free-tier models are overloaded often; this finds ones that
    # actually answer right now.
    from relay.planner import _env_fallbacks

    configured = _env_fallbacks()
    if configured:
        missing = [name for name in configured if name not in models]
        if missing:
            c.fail(
                f"GEMINI_FALLBACK_MODELS names {len(missing)} model(s) this key does not have",
                "remove or replace: " + ", ".join(missing),
            )
        else:
            c.ok(f"fallback models configured: {', '.join(configured)}")
    else:
        c.warn(
            "no GEMINI_FALLBACK_MODELS set",
            "without one, an overloaded model ends the run - probing for usable ones now",
        )
        from google.genai import types

        working: list[tuple[float, str]] = []
        candidates = [
            m for m in sorted(models)
            if "flash" in m and not any(x in m for x in ("image", "tts", "preview"))
        ]
        for name in candidates:
            if name == wanted or len(working) >= 2:
                continue
            try:
                # The probe is already a real call, so time it. A fallback is
                # only reached when the primary has ALREADY failed, so its
                # latency lands on top of a wait the user has endured once.
                started = time.perf_counter()
                client.models.generate_content(
                    model=name,
                    contents="Reply with the single word: ok",
                    config=types.GenerateContentConfig(temperature=0, max_output_tokens=10),
                )
                took = time.perf_counter() - started
                working.append((took, name))
                print(f"          {name:26} usable ({took:.1f}s)")
            except Exception as exc:
                why = "paid-only" if "limit: 0" in str(exc) else str(exc).split(".")[0][:34]
                print(f"          {name:26} no ({why})")
        if working:
            # Fastest first: one sample each is crude, but a 2x difference is
            # not noise, and the alternative is alphabetical order - which is
            # not a fact about anything.
            working.sort()
            names = ",".join(name for _, name in working)
            print(f"\n        add to .env:  GEMINI_FALLBACK_MODELS={names}")
            print("        (fastest first - a fallback's wait is added to the"
                  " failure that caused it)")


def check_brevo(c: Check) -> None:
    """The no-Google route: one API key, local files, .ics invitations."""
    print("\n--- 2. Brevo + local files (the no-Google-Cloud route) ---")

    key = os.environ.get("BREVO_API_KEY")
    sender = os.environ.get("BREVO_SENDER_EMAIL")
    if not key:
        c.warn(
            "BREVO_API_KEY is not set",
            "only needed for RELAY_BACKEND=local. Key: app.brevo.com > SMTP & API",
        )
    if key and not sender:
        c.fail(
            "BREVO_SENDER_EMAIL is not set",
            "an address VERIFIED in Brevo; unverified senders are rejected at send time",
        )
    if key and sender:
        from relay.brevo import BrevoClient
        from relay.tools.base import ToolError

        try:
            name = BrevoClient().account_name()
            c.ok(f"Brevo key works (account: {name})")
            c.ok(f"sending as {sender}")
        except ToolError as exc:
            c.fail(f"Brevo rejected the key: {exc}", "check BREVO_API_KEY in .env")

    # Local data, which costs nothing to check and breaks scenarios when absent.
    docs = pathlib.Path(os.environ.get("RELAY_DOCUMENTS") or "documents")
    contacts = pathlib.Path(os.environ.get("RELAY_CONTACTS") or "contacts.json")

    if docs.is_dir():
        matching = [f for f in docs.iterdir() if "q3 report" in f.name.lower()]
        if len(matching) >= 2:
            c.ok(f"{docs}/ has {len(matching)} files matching 'Q3 report'")
        else:
            c.warn(
                f"{docs}/ has {len(matching)} file(s) matching 'Q3 report'",
                "two are needed for the 'several files match' scenario",
            )
    else:
        c.warn(f"no {docs}/ folder", "create it and put the attachments in it")

    if contacts.is_file():
        import json

        try:
            rows = json.loads(contacts.read_text(encoding="utf-8"))
            rahuls = [r for r in rows if "rahul" in str(r.get("name", "")).lower()]
            c.ok(f"{contacts} has {len(rows)} contacts ({len(rahuls)} called Rahul)")
            if len(rahuls) < 2:
                c.warn(
                    "fewer than two contacts share a first name",
                    "the two-Rahuls scenario needs two",
                )
        except Exception as exc:
            c.fail(f"{contacts} is not valid JSON: {exc}", "fix the file")
    else:
        c.warn(f"no {contacts}", 'create it: [{"name": "...", "email": "...", "note": "..."}]')


def check_google(c: Check) -> None:
    print("\n--- 3. Google Workspace (the full route; optional) ---")

    account = os.environ.get("RELAY_GOOGLE_ACCOUNT")
    if account:
        c.ok(f"RELAY_GOOGLE_ACCOUNT={account}")
    else:
        c.fail(
            "RELAY_GOOGLE_ACCOUNT is not set",
            "the throwaway account RELAY acts as: put it in .env as"
            " RELAY_GOOGLE_ACCOUNT=you@gmail.com",
        )

    secrets = pathlib.Path(os.environ.get("GOOGLE_CLIENT_SECRETS") or "credentials.json")
    token = pathlib.Path(os.environ.get("RELAY_TOKEN_FILE") or "token.json")

    if secrets.exists():
        # The single most common wrong turn: creating a "Web application"
        # client instead of a "Desktop app" one. The JSON looks similar, the
        # console calls both "OAuth client ID", and the failure arrives much
        # later as a redirect_uri_mismatch in a browser tab.
        import json

        try:
            data = json.loads(secrets.read_text(encoding="utf-8"))
        except Exception:
            c.fail(
                f"{secrets} is not valid JSON",
                "re-download it from Cloud Console > Credentials (the download arrow)",
            )
            return

        if "installed" in data:
            project = data["installed"].get("project_id", "?")
            c.ok(f"{secrets} is a Desktop app client (project: {project})")
        elif "web" in data:
            c.fail(
                f"{secrets} is a WEB application client, not a Desktop app",
                "Cloud Console > Credentials > Create credentials > OAuth client ID >"
                " Application type: Desktop app. Download that one instead.",
            )
            return
        else:
            c.fail(
                f"{secrets} does not look like an OAuth client file",
                "download the JSON from the OAuth client you created, not the API key",
            )
            return
    else:
        c.fail(
            f"{secrets} is missing",
            "Cloud Console > Credentials > Create OAuth client ID > Desktop app,"
            f" download the JSON and save it as {secrets}",
        )
        return

    if not token.exists():
        c.fail(
            f"{token} is missing - you have not authorised yet",
            ".venv/Scripts/python.exe -m relay.google_auth  (opens a browser once)",
        )
        return
    c.ok(f"{token} is present (you have authorised)")

    from relay.google_auth import SCOPES, AuthError, build_services, load_credentials

    try:
        creds = load_credentials(allow_browser=False)
    except AuthError as exc:
        c.fail(f"the token is not usable: {exc}", "delete token.json and authorise again")
        return
    except Exception as exc:
        c.fail(f"the token could not be read: {exc}", "delete token.json and authorise again")
        return
    c.ok("the token is valid (or refreshed cleanly)")

    granted = set(getattr(creds, "scopes", []) or [])
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        c.fail(
            f"{len(missing)} scope(s) were not granted",
            "delete token.json, re-run relay.google_auth, and tick every box."
            " Missing: " + ", ".join(s.rsplit("/", 1)[-1] for s in missing),
        )
    else:
        c.ok(f"all {len(SCOPES)} scopes granted")

    # --- read-only probes, one per API, so a disabled API is named ---
    services = build_services(creds)
    probes = [
        ("Gmail", lambda: services["gmail"].users().getProfile(userId="me").execute()),
        (
            "Calendar",
            lambda: services["calendar"].calendarList().list(maxResults=1).execute(),
        ),
        ("Drive", lambda: services["drive"].files().list(pageSize=1, fields="files(id)").execute()),
        (
            "People",
            lambda: services["people"]
            .people()
            .get(resourceName="people/me", personFields="emailAddresses")
            .execute(),
        ),
    ]
    for name, call in probes:
        try:
            result = call()
        except Exception as exc:
            text = str(exc)
            hint = (
                f"enable the {name} API in Cloud Console > APIs & Services > Library"
                if "has not been used" in text or "disabled" in text
                else "check the scopes and that this account is a test user"
            )
            c.fail(f"{name} API call failed", hint)
            continue

        if name == "Gmail":
            address = result.get("emailAddress", "?")
            c.ok(f"Gmail reachable as {address}")
            if account and address.lower() != account.lower():
                c.warn(
                    f"RELAY_GOOGLE_ACCOUNT is {account} but the token is for {address}",
                    "these must match, or 'me' in a plan means the wrong person",
                )
        elif name == "Drive":
            c.ok("Drive reachable")
        else:
            c.ok(f"{name} reachable")


def check_sample_data(c: Check) -> None:
    """The scenarios need particular shapes of data to be reachable at all."""
    print("\n--- 4. Sample data on the Google test account ---")
    if not (pathlib.Path(os.environ.get("RELAY_TOKEN_FILE") or "token.json")).exists():
        print("        (skipped - authorise first)")
        return

    from relay.google_auth import build_services, load_credentials

    try:
        services = build_services(load_credentials(allow_browser=False))
    except Exception:
        print("        (skipped - credentials not usable)")
        return

    try:
        files = (
            services["drive"]
            .files()
            .list(q="name contains 'Q3 report' and trashed = false", fields="files(id,name)")
            .execute()
            .get("files", [])
        )
    except Exception as exc:
        c.fail(f"could not search Drive: {exc}", "check the Drive API is enabled")
        return

    # Contacts: scenario 2 needs two people sharing a first name.
    try:
        people = (
            services["people"]
            .people()
            .searchContacts(query="Rahul", readMask="names,emailAddresses")
            .execute()
            .get("results", [])
        )
        if len(people) >= 2:
            c.ok(f"{len(people)} contacts match 'Rahul' - the two-Rahuls scenario is reachable")
        elif len(people) == 1:
            c.warn(
                "only one contact matches 'Rahul'",
                "scenario 2 needs a second person with the same first name",
            )
        else:
            c.warn(
                "no contact matches 'Rahul'",
                "the People API needs a warm-up call and a few seconds after you add"
                " contacts; add two Rahuls and an Anu, then re-run",
            )
    except Exception as exc:
        c.warn(f"could not search contacts: {str(exc)[:90]}", "check the People API is enabled")

    if len(files) >= 2:
        c.ok(f"{len(files)} files match 'Q3 report' - the ambiguity scenario is reachable")
    elif len(files) == 1:
        c.warn(
            "only one file matches 'Q3 report'",
            "scenario 4 (several matching files) needs a second one, e.g. a draft",
        )
    else:
        c.warn(
            "no file matches 'Q3 report'",
            "scenarios 1, 4 and 11 need at least one; upload a PDF named 'Q3 report FINAL.pdf'",
        )


def main() -> int:
    print("RELAY setup check - read-only, sends nothing")
    c = Check()
    check_gemini(c)
    check_brevo(c)
    check_google(c)
    check_sample_data(c)

    print("\n" + "-" * 60)
    if c.failures:
        print(f"{len(c.failures)} thing(s) still to do - see 'fix:' above.")
        return 1
    print("Everything checks out. To run for real:")
    print("  RELAY_BACKEND=local  - Brevo + local files, no Google Cloud")
    print("  RELAY_BACKEND=live   - the full Google Workspace backend")
    print("  set one in .env, then:")
    print(r"  .venv\Scripts\python.exe -m uvicorn relay.api:app --port 8111")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
