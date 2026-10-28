"""Approval tokens: the gate the model cannot talk its way past.

LESSON 3 - why hash the action instead of trusting a boolean.
A prompt that says "always ask first" is a request. This module is a lock.
The token is minted from a hash of the *exact* action - recipient, attachment,
slot, subject, body. Change one character after approval and the hash changes,
so the old token no longer matches and the executor refuses. That is what the
plan means by "any change after approval voids it".
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets

from .schemas import Action, Decision

#: Signing key for tokens. Generated per process in dev; in deployment this
#: comes from the environment so tokens survive a restart.
_SECRET = os.environ.get("RELAY_TOKEN_SECRET") or secrets.token_hex(32)


class ApprovalError(RuntimeError):
    """Raised when a side-effect action is attempted without a valid approval."""


def action_hash(action: Action) -> str:
    """A stable fingerprint of one action.

    ``sort_keys`` makes the hash independent of dict ordering, so the same
    action planned twice fingerprints the same. ``default=str`` keeps dates
    and other non-JSON values hashable rather than raising mid-run.
    """
    payload = json.dumps(
        {"tool": action.tool.value, "args": action.args},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mint_token(run_id: str, action: Action) -> tuple[str, str]:
    """Return ``(token, action_hash)`` for an action the user just approved.

    The token is an HMAC, so it cannot be forged by anything that has not seen
    the secret - including the model, which never sees tokens at all.
    """
    digest = action_hash(action)
    nonce = secrets.token_urlsafe(8)
    signature = hmac.new(
        _SECRET.encode(), f"{run_id}:{digest}:{nonce}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{nonce}.{signature}", digest


def verify_token(token: str, run_id: str, action: Action) -> bool:
    """True only if ``token`` was minted for this run and this exact action."""
    try:
        nonce, signature = token.split(".", 1)
    except ValueError:
        return False
    expected = hmac.new(
        _SECRET.encode(),
        f"{run_id}:{action_hash(action)}:{nonce}".encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    # compare_digest, not ==, so a wrong token cannot be guessed by timing.
    return hmac.compare_digest(signature, expected)


def action_for_decision(original: Action, decision: Decision) -> Action | None:
    """The action a decision actually authorises.

    "edit" authorises nothing yet: it returns the revised action so the caller
    can send a *fresh* card for it. That is the whole re-approve rule.
    """
    if decision.verdict == "approve":
        return original
    if decision.verdict == "edit":
        return decision.revised_action
    return None  # reject
