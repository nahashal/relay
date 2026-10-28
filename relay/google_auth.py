"""OAuth for the test Google account, and the four service clients.

LESSON 21 - ask for the narrowest scopes that do the job.
It is tempting to request `gmail.modify` and `calendar` and be done with it. The
reason not to is the same reason the approval gate exists: RELAY should not be
*able* to do things nobody approved. A token that cannot delete mail cannot
delete mail because of a bug, a bad plan or a prompt injection. So:

  gmail.compose      create a draft and send that draft - not read the inbox
  gmail.metadata     read labels only, which is all verification needs
  calendar.events    create and read events - not delete other calendars
  calendar.freebusy  when people are busy, never what they are doing
  drive.readonly     find and download the attachment - never write
  contacts.readonly  resolve a name to an address
  directory.readonly the same inside a Workspace domain

Note what is absent: no gmail.readonly (RELAY never reads the inbox), no
drive.file write scope, nothing that can delete. If a future tool needs more,
that is a decision to make deliberately, not by widening this list in passing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

#: Narrowest scopes that cover the eight tools. See the module docstring.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.metadata",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/directory.readonly",
)

DEFAULT_TOKEN = Path("token.json")
DEFAULT_SECRETS = Path("credentials.json")


class AuthError(RuntimeError):
    """Credentials are missing, expired beyond refresh, or lack a needed scope."""


def load_credentials(
    secrets: Path | str | None = None,
    token: Path | str | None = None,
    *,
    allow_browser: bool = True,
) -> Any:
    """Return usable OAuth credentials, refreshing or prompting as needed.

    ``allow_browser=False`` makes this non-interactive, which is what a server
    wants: better to fail at startup with a clear message than to block a
    request waiting for a consent screen nobody is looking at.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = Path(token or os.environ.get("RELAY_TOKEN_FILE") or DEFAULT_TOKEN)
    secrets_path = Path(secrets or os.environ.get("GOOGLE_CLIENT_SECRETS") or DEFAULT_SECRETS)

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), list(SCOPES))

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if not allow_browser:
        raise AuthError(
            f"no usable credentials at {token_path}."
            " Run `python -m relay.google_auth` once to authorise the test account."
        )

    if not secrets_path.exists():
        raise AuthError(
            f"{secrets_path} not found. Download the OAuth client from the Google Cloud"
            " console (Desktop app) and save it there."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), list(SCOPES))
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_services(credentials: Any) -> dict[str, Any]:
    """The four API clients RELAY needs, built from one set of credentials.

    ``cache_discovery=False`` because the default file cache warns noisily under
    modern google-auth and buys nothing for four clients.
    """
    from googleapiclient.discovery import build

    options = {"credentials": credentials, "cache_discovery": False}
    return {
        "gmail": build("gmail", "v1", **options),
        "calendar": build("calendar", "v3", **options),
        "drive": build("drive", "v3", **options),
        "people": build("people", "v1", **options),
    }


def main() -> int:
    """Authorise once, interactively, and write token.json.

    Kept as a module entry point so the OAuth consent step is a thing you run
    deliberately, not something that ambushes you mid-request.
    """
    creds = load_credentials(allow_browser=True)
    token_path = Path(os.environ.get("RELAY_TOKEN_FILE") or DEFAULT_TOKEN)
    print(f"authorised; token written to {token_path.resolve()}")
    print("scopes granted:")
    for scope in sorted(getattr(creds, "scopes", []) or SCOPES):
        print(f"  {scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
