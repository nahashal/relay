"""A small Brevo client: send a transactional email, and ask what happened to it.

LESSON 32 - inject the transport, and the network stops being untestable.
``BrevoClient`` takes a ``transport`` callable. In production it is urllib; in
tests it is a function that records the request and returns a canned response.
That one parameter is why every rule below - the auth header, the base64
attachment, the error mapping - is covered by tests that touch no network.

Only stdlib: one more dependency on the path to a client handover is not worth
it for two POSTs and a GET.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol

from .tools.base import ToolError

BASE_URL = "https://api.brevo.com/v3"

#: Brevo's documented attachment ceiling. Refusing here beats a 413 after the
#: user has already approved the send.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


class Transport(Protocol):
    """How a request actually leaves the process."""

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, dict[str, Any]]: ...


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, dict[str, Any]]:
    """The real one. Returns ``(status, parsed json)``."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8") or "{}"
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") or "{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"message": raw}
    except Exception as exc:  # DNS, TLS, timeouts
        raise ToolError(f"could not reach Brevo: {exc}") from exc


class BrevoClient:
    """Send mail and look it up again. Nothing else from the API is needed."""

    def __init__(
        self,
        api_key: str | None = None,
        sender_email: str | None = None,
        sender_name: str = "RELAY",
        transport: Transport = urllib_transport,
    ) -> None:
        self.api_key = api_key or os.environ.get("BREVO_API_KEY") or ""
        self.sender_email = sender_email or os.environ.get("BREVO_SENDER_EMAIL") or ""
        self.sender_name = sender_name or os.environ.get("BREVO_SENDER_NAME") or "RELAY"
        self._transport = transport

        if not self.api_key:
            raise ToolError("BREVO_API_KEY is not set")
        if not self.sender_email:
            raise ToolError(
                "BREVO_SENDER_EMAIL is not set - it must be an address verified in Brevo"
            )

    # --- plumbing -----------------------------------------------------------

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict[str, Any]:
        headers = {
            # Brevo uses its own header, not Authorization: Bearer.
            "api-key": self.api_key,
            "accept": "application/json",
        }
        body = None
        if payload is not None:
            headers["content-type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")

        status, data = self._transport(method, f"{BASE_URL}{path}", headers, body)

        if status >= 400:
            message = data.get("message") or data.get("error") or str(data)
            hint = ""
            if status == 401:
                hint = " - check BREVO_API_KEY"
            elif status == 400 and "sender" in str(message).lower():
                hint = " - the sender address must be verified in Brevo first"
            raise ToolError(f"Brevo {method} {path} failed ({status}): {message}{hint}")
        return data

    # --- sending ------------------------------------------------------------

    def send(
        self,
        to: list[str],
        subject: str,
        text: str,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> str:
        """Send one email, return Brevo's messageId."""
        if not to:
            raise ToolError("an email needs at least one recipient")

        payload: dict[str, Any] = {
            "sender": {"email": self.sender_email, "name": self.sender_name},
            "to": [{"email": address} for address in to],
            "subject": subject,
            "textContent": text,
        }

        for name, blob in attachments or []:
            if len(blob) > MAX_ATTACHMENT_BYTES:
                raise ToolError(
                    f"{name} is {len(blob) // 1024 // 1024} MB;"
                    f" Brevo's limit is {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB"
                )
            payload.setdefault("attachment", []).append(
                {"name": name, "content": base64.b64encode(blob).decode("ascii")}
            )

        data = self._call("POST", "/smtp/email", payload)
        message_id = data.get("messageId")
        if not message_id:
            raise ToolError(f"Brevo accepted the request but returned no messageId: {data}")
        return str(message_id)

    # --- verification -------------------------------------------------------

    def message_exists(self, message_id: str) -> bool:
        """Does Brevo know this message?

        LESSON 33 - weaker evidence, honestly labelled.
        Gmail's check was "is it in Sent", which is a fact about a mailbox.
        This is "has Brevo recorded an event for it", which is a fact about a
        queue - and the queue is asynchronous, so a message that WILL be
        delivered can be unknown here for a few seconds after sending.

        So this is not the same guarantee, and the README says so. It is still
        worth doing: a wrong or rejected messageId never appears at all.
        """
        try:
            data = self._call("GET", f"/smtp/statistics/events?messageId={message_id}&limit=1")
        except ToolError:
            return False
        return bool(data.get("events"))

    def account_name(self) -> str:
        """Used by the setup checker to prove the key works. Read-only."""
        data = self._call("GET", "/account")
        return str(data.get("companyName") or data.get("email") or "unknown")
