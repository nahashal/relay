"""Choosing a backend, once, in a place you can point at.

LESSON 24 - make the dangerous mode the one you have to ask for.
``RELAY_BACKEND`` defaults to "mock". Nothing here falls back to live, guesses
from the presence of a token file, or decides that credentials lying around
mean you meant to use them. Going live is a thing you type.

The reverse default would be the kind of mistake that is only discovered
afterwards, by the person who received the email.
"""

from __future__ import annotations

import os

from .tools.base import Backend
from .tools.mock_backend import MockBackend


class BackendError(RuntimeError):
    """The requested backend cannot be built. Never silently downgraded."""


def backend_name() -> str:
    return (os.environ.get("RELAY_BACKEND") or "mock").strip().lower()


def is_live() -> bool:
    """True for any backend that really sends. Both live modes count."""
    return backend_name() in {"live", "local"} and not is_dry_run()


def is_dry_run() -> bool:
    """Plan and approve for real, but perform no side effect.

    LESSON 43 - separate "where the data comes from" from "is this real".
    Those were one switch: `mock` meant invented files AND no sending, `local`
    meant real files AND real sending. Someone who wants to plan against their
    own documents without emailing anyone had nowhere to stand - and the
    Executor has supported exactly that since the beginning, unreachable,
    because nothing read it from config.
    """
    return (os.environ.get("RELAY_DRY_RUN") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def make_backend() -> Backend:
    """The backend this process should use, per ``RELAY_BACKEND``.

    A misspelt value is an error, not a quiet fall back to mock: "RELAY_BACKEND=liv"
    silently mocking everything would waste an afternoon of a demo.
    """
    name = backend_name()
    if name == "mock":
        return MockBackend()
    if name == "local":
        return make_local_backend()
    if name != "live":
        raise BackendError(
            f"RELAY_BACKEND must be 'mock', 'local' or 'live', not {name!r}"
        )
    return make_google_backend()


def make_local_backend() -> Backend:
    """Local files + Brevo + .ics invitations. No Google Cloud, one API key.

    Note what this does NOT need: no OAuth, no token file, no consent screen.
    And note what it cannot do - see LESSON 34 - it proposes meeting times
    rather than finding free ones, because nothing here can read a calendar.
    """
    from .brevo import BrevoClient
    from .tools.base import ToolError
    from .tools.local_backend import LocalBackend

    me = os.environ.get("RELAY_GOOGLE_ACCOUNT") or os.environ.get("BREVO_SENDER_EMAIL")
    if not me:
        raise BackendError(
            "RELAY_BACKEND=local needs RELAY_GOOGLE_ACCOUNT (or BREVO_SENDER_EMAIL)"
            " set to the address RELAY organises meetings as"
        )
    try:
        client = BrevoClient()
    except ToolError as exc:
        raise BackendError(str(exc)) from exc

    return LocalBackend(
        me=me,
        brevo=client,
        timezone=os.environ.get("RELAY_TIMEZONE", "Asia/Kolkata"),
        working_hours=(
            os.environ.get("RELAY_DAY_START", "09:00"),
            os.environ.get("RELAY_DAY_END", "18:00"),
        ),
    )


def make_google_backend() -> Backend:
    """Build the live backend, failing loudly if anything is missing."""
    from .google_auth import AuthError, build_services, load_credentials
    from .tools.google_backend import GoogleBackend

    me = os.environ.get("RELAY_GOOGLE_ACCOUNT")
    if not me:
        # Everything in a plan is addressed relative to "me": the organiser of
        # the meeting and the sender of the email. Guessing it is not an option.
        raise BackendError(
            "RELAY_BACKEND=live needs RELAY_GOOGLE_ACCOUNT set to the test account's address"
        )

    try:
        # allow_browser=False: a server must fail at startup with a clear
        # message rather than block a request on a consent screen nobody sees.
        credentials = load_credentials(allow_browser=False)
    except AuthError as exc:
        raise BackendError(str(exc)) from exc

    hours = (
        os.environ.get("RELAY_DAY_START", "09:00"),
        os.environ.get("RELAY_DAY_END", "18:00"),
    )
    return GoogleBackend(
        services=build_services(credentials),
        me=me,
        timezone=os.environ.get("RELAY_TIMEZONE", "Asia/Kolkata"),
        working_hours=hours,
    )
