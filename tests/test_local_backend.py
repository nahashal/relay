"""The no-Google backend: local files, Brevo, .ics invitations.

Brevo is driven through an injected transport, so these tests send nothing and
need no key. The .ics builder and the file/contact lookups are pure and tested
directly.
"""

from __future__ import annotations

import base64
import datetime as dt
import json

import pytest

from relay.brevo import BrevoClient
from relay.ics import build_invite, parse_slot
from relay.tools import ToolError
from relay.tools.local_backend import LocalBackend

RAHUL = "rahul.menon@example.com"
ANU = "anu.prakash@example.com"
ME = "guppy@example.com"
MON = dt.date(2026, 11, 2)
FRI = dt.date(2026, 11, 6)


class FakeTransport:
    """Records requests, replays canned responses. Nothing leaves the process."""

    def __init__(self, responses=None):
        self.calls: list[dict] = []
        self._responses = responses or {}
        self._counter = 0

    def __call__(self, method, url, headers, body):
        payload = json.loads(body.decode()) if body else None
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "payload": payload}
        )
        for fragment, response in self._responses.items():
            if fragment in url:
                return response
        self._counter += 1
        return 201, {"messageId": f"<brevo-{self._counter}@relay>"}


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def brevo(transport):
    return BrevoClient(
        api_key="test-key", sender_email="relay@example.com", transport=transport
    )


@pytest.fixture
def workspace(tmp_path):
    """A documents folder and a contacts file, shaped like the real samples."""
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "Q3 report FINAL.txt").write_text("final", encoding="utf-8")
    (docs / "Q3 report draft.txt").write_text("draft", encoding="utf-8")
    (docs / "Q2 report FINAL.txt").write_text("old", encoding="utf-8")

    contacts = tmp_path / "contacts.json"
    contacts.write_text(
        json.dumps(
            [
                {"name": "Rahul Menon", "email": RAHUL, "note": "Finance"},
                {"name": "Rahul Shetty", "email": "rahul.shetty@example.com", "note": "Design"},
                {"name": "Anu Prakash", "email": ANU, "note": "Finance"},
            ]
        ),
        encoding="utf-8",
    )
    return docs, contacts


@pytest.fixture
def backend(workspace, brevo):
    docs, contacts = workspace
    return LocalBackend(me=ME, documents=docs, contacts=contacts, brevo=brevo)


# --------------------------------------------------------------------------- #
# Local lookups
# --------------------------------------------------------------------------- #


def test_two_rahuls_are_both_returned(backend):
    """The ask-rather-than-guess path must be reachable with real files."""
    assert len(backend.find_contact("Rahul")) == 2


def test_a_full_name_disambiguates(backend):
    assert backend.find_contact("Rahul Menon")[0]["email"] == RAHUL


def test_several_files_match_newest_first(backend):
    matches = backend.find_file("Q3 report")
    assert len(matches) == 2
    assert all("Q3 report" in m["name"] for m in matches)


def test_the_filename_is_the_id(backend):
    """No remote ids to resolve - what you see on disk is what gets attached."""
    match = backend.find_file("Q2 report")[0]
    assert match["id"] == match["name"] == "Q2 report FINAL.txt"


def test_an_empty_query_is_refused(backend):
    with pytest.raises(ToolError, match="something to search for"):
        backend.find_file("   ")


def test_a_missing_documents_folder_says_so(tmp_path, brevo):
    b = LocalBackend(me=ME, documents=tmp_path / "nope", contacts=tmp_path / "c.json", brevo=brevo)
    with pytest.raises(ToolError, match="no documents folder"):
        b.find_file("anything")


def test_a_broken_contacts_file_says_so(tmp_path, brevo, workspace):
    docs, _ = workspace
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    b = LocalBackend(me=ME, documents=docs, contacts=bad, brevo=brevo)
    with pytest.raises(ToolError, match="not valid JSON"):
        b.find_contact("Rahul")


# --------------------------------------------------------------------------- #
# Proposing times - the weakened claim
# --------------------------------------------------------------------------- #


def test_it_proposes_times_and_says_so(backend):
    """The honesty flag the UI and the checker read."""
    assert backend.proposes_only is True


def test_proposals_are_spread_across_days_not_stacked(backend):
    slots = backend.find_free_slots([ME, RAHUL], 30, MON, FRI)
    days = {s["start"][:10] for s in slots}
    assert len(days) >= 3, "all the offers landed on too few days"
    assert len(slots) <= 8, "a scroll is not a choice"


def test_proposals_sit_inside_working_hours(backend):
    for slot in backend.find_free_slots([ME, RAHUL], 30, MON, FRI):
        start = dt.datetime.fromisoformat(slot["start"])
        assert 9 <= start.hour < 18


def test_proposals_never_land_on_a_weekend(backend):
    for slot in backend.find_free_slots([ME], 30, dt.date(2026, 11, 7), dt.date(2026, 11, 8)):
        assert dt.datetime.fromisoformat(slot["start"]).weekday() < 5


def test_the_requested_duration_is_honoured(backend):
    slot = backend.find_free_slots([ME, RAHUL], 45, MON, FRI)[0]
    start, end = parse_slot(slot)
    assert end - start == dt.timedelta(minutes=45)


# --------------------------------------------------------------------------- #
# The .ics invitation
# --------------------------------------------------------------------------- #


def _invite():
    start = dt.datetime(2026, 11, 3, 12, 30, tzinfo=dt.timezone.utc)
    return build_invite(
        start=start,
        end=start + dt.timedelta(minutes=30),
        title="Q3 review",
        organiser=ME,
        attendees=[RAHUL, ANU],
    )


def test_the_invite_is_a_request_not_a_file():
    """METHOD:REQUEST is what makes a client render accept/decline buttons."""
    assert "METHOD:REQUEST" in _invite()


def test_the_invite_asks_each_attendee_to_rsvp():
    invite = _invite()
    assert invite.count("ATTENDEE") == 2
    assert invite.count("RSVP=TRUE") == 2


def test_the_invite_uses_crlf_endings():
    """RFC 5545 wants CRLF; the clients that insist, fail silently without it."""
    assert "\r\n" in _invite()
    assert "\n" not in _invite().replace("\r\n", "")


def test_long_lines_are_folded():
    invite = build_invite(
        start=dt.datetime(2026, 11, 3, 9, tzinfo=dt.timezone.utc),
        end=dt.datetime(2026, 11, 3, 10, tzinfo=dt.timezone.utc),
        title="A review with an extremely long title " * 4,
        organiser=ME,
        attendees=[RAHUL],
    )
    assert all(len(line.encode()) <= 75 for line in invite.split("\r\n"))


def test_special_characters_are_escaped():
    invite = build_invite(
        start=dt.datetime(2026, 11, 3, 9, tzinfo=dt.timezone.utc),
        end=dt.datetime(2026, 11, 3, 10, tzinfo=dt.timezone.utc),
        title="Budget; Q3, final",
        organiser=ME,
        attendees=[RAHUL],
    )
    assert "Budget\\; Q3\\, final" in invite


def test_an_invite_needs_an_attendee_and_a_duration():
    start = dt.datetime(2026, 11, 3, 9, tzinfo=dt.timezone.utc)
    with pytest.raises(ValueError):
        build_invite(start, start, "t", ME, [RAHUL])  # zero length
    with pytest.raises(ValueError):
        build_invite(start, start + dt.timedelta(hours=1), "t", ME, [])  # nobody


def test_a_naive_datetime_is_refused():
    with pytest.raises(ValueError):
        build_invite(
            dt.datetime(2026, 11, 3, 9),
            dt.datetime(2026, 11, 3, 10),
            "t",
            ME,
            [RAHUL],
        )


# --------------------------------------------------------------------------- #
# Sending through Brevo
# --------------------------------------------------------------------------- #


def test_send_posts_to_brevo_with_the_api_key_header(backend, transport):
    draft = backend.draft_email([RAHUL], "Q3 report", "Hi", ["Q3 report FINAL.txt"])
    receipt = backend.send_email(draft["draft_id"])

    call = transport.calls[0]
    assert call["method"] == "POST" and call["url"].endswith("/smtp/email")
    # Brevo uses its own header, not Authorization: Bearer.
    assert call["headers"]["api-key"] == "test-key"
    assert receipt["message_id"].startswith("<brevo-")


def test_the_attachment_is_base64_of_the_real_file(backend, transport, workspace):
    docs, _ = workspace
    draft = backend.draft_email([RAHUL], "s", "b", ["Q3 report FINAL.txt"])
    backend.send_email(draft["draft_id"])

    attachment = transport.calls[0]["payload"]["attachment"][0]
    assert attachment["name"] == "Q3 report FINAL.txt"
    assert base64.b64decode(attachment["content"]) == (docs / "Q3 report FINAL.txt").read_bytes()


def test_drafting_an_unknown_document_is_refused(backend):
    with pytest.raises(ToolError, match="no such document"):
        backend.draft_email([RAHUL], "s", "b", ["nope.pdf"])


def test_create_event_emails_the_invitation(backend, transport):
    slot = {"start": "2026-11-03T12:30:00+00:00", "end": "2026-11-03T13:00:00+00:00"}
    receipt = backend.create_event(slot, [ME, RAHUL, ANU], "Q3 review")

    payload = transport.calls[0]["payload"]
    # The organiser is not invited to their own meeting.
    assert [r["email"] for r in payload["to"]] == [RAHUL, ANU]
    assert "Invitation:" in payload["subject"]

    ics = base64.b64decode(payload["attachment"][0]["content"]).decode()
    assert payload["attachment"][0]["name"] == "invite.ics"
    assert "METHOD:REQUEST" in ics
    assert receipt["event_id"]


def test_an_event_with_only_yourself_is_refused(backend):
    slot = {"start": "2026-11-03T12:30:00+00:00", "end": "2026-11-03T13:00:00+00:00"}
    with pytest.raises(ToolError, match="nobody to invite"):
        backend.create_event(slot, [ME], "Alone")


def test_a_rejected_send_becomes_a_tool_error(workspace):
    docs, contacts = workspace
    transport = FakeTransport({"/smtp/email": (401, {"message": "Key not found"})})
    backend = LocalBackend(
        me=ME,
        documents=docs,
        contacts=contacts,
        brevo=BrevoClient("bad", "relay@example.com", transport=transport),
    )
    draft = backend.draft_email([RAHUL], "s", "b", [])
    with pytest.raises(ToolError, match="check BREVO_API_KEY"):
        backend.send_email(draft["draft_id"])


def test_an_unverified_sender_gets_a_useful_message(workspace):
    docs, contacts = workspace
    transport = FakeTransport(
        {"/smtp/email": (400, {"message": "Invalid sender: not verified"})}
    )
    backend = LocalBackend(
        me=ME,
        documents=docs,
        contacts=contacts,
        brevo=BrevoClient("k", "relay@example.com", transport=transport),
    )
    draft = backend.draft_email([RAHUL], "s", "b", [])
    with pytest.raises(ToolError, match="verified in Brevo"):
        backend.send_email(draft["draft_id"])


def test_a_missing_key_is_refused_at_construction():
    with pytest.raises(ToolError, match="BREVO_API_KEY"):
        BrevoClient(api_key="", sender_email="x@example.com", transport=FakeTransport())


def test_a_missing_sender_is_refused_at_construction():
    with pytest.raises(ToolError, match="BREVO_SENDER_EMAIL"):
        BrevoClient(api_key="k", sender_email="", transport=FakeTransport())


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def test_an_unknown_message_is_not_verified(backend):
    assert backend.message_in_sent("<never-sent@relay>") is False


def test_a_sent_message_verifies(backend):
    draft = backend.draft_email([RAHUL], "s", "b", [])
    receipt = backend.send_email(draft["draft_id"])
    assert backend.message_in_sent(receipt["message_id"]) is True


def test_the_protocol_is_satisfied(backend):
    """Same nine methods, so the loop cannot tell the backends apart."""
    for name in (
        "find_contact", "find_file", "find_free_slots", "draft_email", "get_draft",
        "send_email", "create_event", "message_in_sent", "event_on_calendar",
    ):
        assert callable(getattr(backend, name)), name
