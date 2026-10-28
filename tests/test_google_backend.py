"""GoogleBackend without a Google account.

Everything that can be wrong on its own - escaping, MIME, parsing, the decision
that a send landed - is a pure function, so it is tested directly. The class is
then driven with service doubles to check it asks Google the right questions and
maps the answers back correctly.

What this CANNOT prove: that the real APIs behave as assumed. That needs the
test account, and the README says so plainly.
"""

from __future__ import annotations

import base64
import datetime as dt
import email
from email import policy

import pytest

from fake_google import FakeHttpError, fake_services
from relay.tools import ToolError
from relay.tools.google_backend import (
    GoogleBackend,
    attachment_filename,
    build_message,
    dedupe_contacts,
    drive_query,
    event_is_live,
    export_format,
    parse_busy,
    parse_contacts,
    parse_files,
    rfc3339,
    was_sent,
)

ME = "guppy@relay-test.example"
RAHUL = "rahul.menon@relay-test.example"
ANU = "anu.prakash@relay-test.example"
MON = dt.date(2026, 11, 2)
FRI = dt.date(2026, 11, 6)


# --------------------------------------------------------------------------- #
# Drive query escaping
# --------------------------------------------------------------------------- #


def test_drive_query_matches_names_and_excludes_the_bin():
    assert drive_query("Q3 report") == "name contains 'Q3 report' and trashed = false"


def test_an_apostrophe_cannot_end_the_query_string():
    """\"Tom's copy\" must not break out of the single-quoted literal."""
    q = drive_query("Tom's copy")
    assert "\\'" in q
    # One opening and one closing quote around the value: no stray terminator.
    assert q.count("'") - q.count("\\'") == 2


def test_backslashes_are_escaped_before_quotes():
    """Order matters: quotes first would double-escape the new backslashes."""
    assert drive_query("a\\b") == "name contains 'a\\\\b' and trashed = false"


def test_an_empty_query_is_refused(backend_services):
    services, _ = backend_services
    google = GoogleBackend(services, me=ME)
    with pytest.raises(ToolError, match="something to search for"):
        google.find_file("   ")


# --------------------------------------------------------------------------- #
# Parsing People and Drive
# --------------------------------------------------------------------------- #


def test_contacts_are_read_with_the_distinguishing_department():
    parsed = parse_contacts(
        {
            "results": [
                {
                    "person": {
                        "names": [{"displayName": "Rahul Menon"}],
                        "emailAddresses": [{"value": RAHUL}],
                        "organizations": [{"department": "Finance"}],
                    }
                }
            ]
        }
    )
    assert parsed == [{"name": "Rahul Menon", "email": RAHUL, "note": "Finance"}]


def test_a_contact_with_no_department_still_says_something():
    """An empty note makes a 'which Rahul?' card useless."""
    parsed = parse_contacts(
        {"results": [{"person": {"names": [{"displayName": "X"}], "emailAddresses": [{"value": ME}]}}]}
    )
    assert parsed[0]["note"] == "no department listed"


def test_a_contact_with_no_address_is_skipped():
    parsed = parse_contacts({"results": [{"person": {"names": [{"displayName": "No Email"}]}}]})
    assert parsed == []


def test_duplicates_across_contacts_and_directory_collapse():
    """The same person in both sources would be two identical options."""
    rows = [
        {"name": "Rahul Menon", "email": RAHUL, "note": "Finance"},
        {"name": "Rahul Menon", "email": RAHUL.upper(), "note": "Finance"},
        {"name": "Anu", "email": ANU, "note": "Finance"},
    ]
    assert [c["email"] for c in dedupe_contacts(rows)] == [RAHUL, ANU]


def test_files_come_back_newest_first():
    parsed = parse_files(
        {
            "files": [
                {"id": "old", "name": "A", "modifiedTime": "2026-01-01T00:00:00Z", "mimeType": "x"},
                {"id": "new", "name": "B", "modifiedTime": "2026-10-01T00:00:00Z", "mimeType": "x"},
            ]
        }
    )
    assert [f["id"] for f in parsed] == ["new", "old"]


# --------------------------------------------------------------------------- #
# free/busy: an error is not an empty result (LESSON 23)
# --------------------------------------------------------------------------- #


def test_busy_intervals_are_read():
    busy = parse_busy(
        {
            "calendars": {
                RAHUL: {"busy": [{"start": "2026-11-03T09:00:00Z", "end": "2026-11-03T10:00:00Z"}]}
            }
        },
        [RAHUL],
    )
    assert len(busy) == 1
    assert busy[0][0].hour == 9


def test_an_inaccessible_calendar_raises_rather_than_looking_free():
    """The dangerous case: a 200 response carrying a per-calendar error."""
    with pytest.raises(ToolError, match="cannot read"):
        parse_busy(
            {"calendars": {RAHUL: {"errors": [{"reason": "notFound"}], "busy": []}}},
            [RAHUL],
        )


def test_a_missing_calendar_raises_rather_than_looking_free():
    with pytest.raises(ToolError, match="no free/busy information"):
        parse_busy({"calendars": {}}, [RAHUL])


def test_a_genuinely_empty_calendar_is_free():
    """Empty means free; only errors and absence mean 'we do not know'."""
    assert parse_busy({"calendars": {RAHUL: {"busy": []}}}, [RAHUL]) == []


def test_every_requested_person_is_checked():
    """One good calendar must not vouch for a missing one."""
    with pytest.raises(ToolError):
        parse_busy({"calendars": {RAHUL: {"busy": []}}}, [RAHUL, ANU])


# --------------------------------------------------------------------------- #
# Building the message
# --------------------------------------------------------------------------- #


def _decode(payload: dict[str, str]):
    raw = base64.urlsafe_b64decode(payload["raw"])
    return email.message_from_bytes(raw, policy=policy.default)


def test_a_message_carries_its_headers_and_body():
    message = _decode(build_message([RAHUL], "Q3 report", "Hi Rahul", [], sender=ME))
    assert message["To"] == RAHUL
    assert message["Subject"] == "Q3 report"
    assert message["From"] == ME
    assert "Hi Rahul" in message.get_body(("plain",)).get_content()


def test_several_recipients_are_comma_joined():
    message = _decode(build_message([RAHUL, ANU], "s", "b", []))
    assert message["To"] == f"{RAHUL}, {ANU}"


def test_an_attachment_survives_the_round_trip():
    payload = b"%PDF-1.7 pretend report"
    message = _decode(
        build_message([RAHUL], "s", "b", [("Q3 report.pdf", "application/pdf", payload)])
    )
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "Q3 report.pdf"
    assert attachments[0].get_content() == payload


def test_the_raw_payload_is_url_safe_base64():
    """Standard base64's + and / break Gmail's JSON transport."""
    raw = build_message([RAHUL], "s" * 200, "b" * 500, [("f.bin", "application/octet-stream", bytes(range(256)))])["raw"]
    assert "+" not in raw and "/" not in raw


def test_an_oversized_attachment_is_refused_before_sending():
    with pytest.raises(ToolError, match="the limit is"):
        build_message([RAHUL], "s", "b", [("huge.bin", "application/octet-stream", b"x" * (16 * 1024 * 1024))])


# --------------------------------------------------------------------------- #
# Google-native files and filenames
# --------------------------------------------------------------------------- #


def test_a_google_doc_is_exported_as_pdf():
    assert export_format("application/vnd.google-apps.document", "Q3 report") == (
        "application/pdf",
        "Q3 report.pdf",
    )


def test_an_existing_extension_is_not_doubled():
    _, filename = export_format("application/vnd.google-apps.document", "Q3 report.pdf")
    assert filename == "Q3 report.pdf"


def test_an_ordinary_file_is_not_exported():
    assert export_format("application/pdf", "Q3 report.pdf") is None


def test_a_missing_extension_is_guessed_from_the_type():
    assert attachment_filename("Q3 report", "application/pdf") == "Q3 report.pdf"
    assert attachment_filename("Q3 report.pdf", "application/pdf") == "Q3 report.pdf"


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def test_sent_is_decided_by_the_label():
    assert was_sent({"labelIds": ["SENT", "INBOX"]})
    assert not was_sent({"labelIds": ["DRAFT"]})
    assert not was_sent({})


def test_a_cancelled_event_is_not_on_the_calendar():
    """It still answers with a 200 and a body, so 'no exception' is not enough."""
    assert event_is_live({"id": "e1", "status": "confirmed"})
    assert not event_is_live({"id": "e1", "status": "cancelled"})
    assert not event_is_live({})


def test_a_naive_datetime_is_refused():
    with pytest.raises(ValueError):
        rfc3339(dt.datetime(2026, 11, 2, 9, 0))


# --------------------------------------------------------------------------- #
# The class, driven with doubles
# --------------------------------------------------------------------------- #


@pytest.fixture
def backend_services():
    return fake_services(
        people__people__searchContacts={
            "results": [
                {
                    "person": {
                        "names": [{"displayName": "Rahul Menon"}],
                        "emailAddresses": [{"value": RAHUL}],
                        "organizations": [{"department": "Finance"}],
                    }
                }
            ]
        },
        people__people__searchDirectoryPeople={"people": []},
        drive__files__list={
            "files": [
                {
                    "id": "f_q3",
                    "name": "Q3 report FINAL.pdf",
                    "modifiedTime": "2026-10-20T11:04:00Z",
                    "mimeType": "application/pdf",
                }
            ]
        },
        drive__files__get={"name": "Q3 report FINAL.pdf", "mimeType": "application/pdf"},
        drive__files__get_media=b"%PDF-1.7 pretend",
        gmail__users__drafts__create={"id": "draft_1"},
        gmail__users__drafts__send={"id": "msg_1", "threadId": "thread_1"},
        gmail__users__messages__get={"labelIds": ["SENT"]},
        calendar__freebusy__query={
            RAHUL: None,  # replaced below
        },
        calendar__events__insert={"id": "evt_1", "htmlLink": "https://cal/evt_1"},
        calendar__events__get={"id": "evt_1", "status": "confirmed"},
    )


@pytest.fixture
def google(backend_services):
    services, _ = backend_services
    return GoogleBackend(services, me=ME, timezone="Asia/Kolkata")


def test_find_contact_merges_both_sources(google, backend_services):
    _, calls = backend_services
    assert google.find_contact("Rahul")[0]["email"] == RAHUL
    asked = [path for path, _ in calls]
    assert "people.searchContacts" in asked
    assert "people.searchDirectoryPeople" in asked


def test_a_personal_account_without_a_directory_still_works():
    """searchDirectoryPeople 404s on a personal Gmail; contacts must still stand."""
    services, _ = fake_services(
        people__people__searchContacts={
            "results": [{"person": {"names": [{"displayName": "A"}], "emailAddresses": [{"value": ANU}]}}]
        },
        people__people__searchDirectoryPeople=FakeHttpError(404, "no directory"),
    )
    assert GoogleBackend(services, me=ME).find_contact("A")[0]["email"] == ANU


def test_find_file_sends_the_escaped_query(google, backend_services):
    _, calls = backend_services
    google.find_file("Tom's report")
    query = dict(next(kw for path, kw in calls if path == "files.list"))["q"]
    assert "\\'" in query and "trashed = false" in query


def test_an_api_failure_becomes_a_tool_error():
    services, _ = fake_services(drive__files__list=FakeHttpError(500, "boom"))
    with pytest.raises(ToolError, match="file search failed"):
        GoogleBackend(services, me=ME).find_file("Q3")


def test_free_slots_asks_for_the_whole_window(google, backend_services):
    services, calls = backend_services
    services["calendar"]._responses["freebusy.query"] = {
        "calendars": {ME: {"busy": []}, RAHUL: {"busy": []}}
    }
    slots = google.find_free_slots([ME, RAHUL], 30, MON, FRI)

    body = dict(next(kw for path, kw in calls if path == "freebusy.query"))["body"]
    assert body["timeMin"].startswith("2026-11-02")
    # The day after the last day, so Friday is fully covered.
    assert body["timeMax"].startswith("2026-11-07")
    assert [item["id"] for item in body["items"]] == [ME, RAHUL]
    # Five empty weekdays. 09:00-18:00 in 30-minute steps gives 18 start times
    # a day: 09:00 through 17:30 inclusive.
    assert len(slots) == 5 * 18


def test_busy_time_removes_slots(google, backend_services):
    services, _ = backend_services
    services["calendar"]._responses["freebusy.query"] = {
        "calendars": {
            ME: {"busy": [{"start": "2026-11-02T03:30:00Z", "end": "2026-11-02T12:30:00Z"}]},
            RAHUL: {"busy": []},
        }
    }
    slots = google.find_free_slots([ME, RAHUL], 30, MON, MON)
    assert slots == []  # 09:00-18:00 IST is exactly that UTC block


def test_draft_attaches_the_downloaded_file(google, backend_services):
    _, calls = backend_services
    draft = google.draft_email([RAHUL], "Q3 report", "Hi", ["f_q3"])

    assert draft["draft_id"] == "draft_1"
    assert google.get_draft("draft_1") == draft
    body = dict(next(kw for path, kw in calls if path == "users.drafts.create"))["body"]
    message = _decode(body["message"])
    assert [a.get_filename() for a in message.iter_attachments()] == ["Q3 report FINAL.pdf"]


def test_a_google_doc_attachment_is_exported_not_downloaded():
    services, calls = fake_services(
        drive__files__get={"name": "Q3 report", "mimeType": "application/vnd.google-apps.document"},
        drive__files__export=b"%PDF-1.7 exported",
        gmail__users__drafts__create={"id": "draft_2"},
    )
    GoogleBackend(services, me=ME).draft_email([RAHUL], "s", "b", ["f_doc"])
    asked = [path for path, _ in calls]
    assert "files.export" in asked and "files.get_media" not in asked


def test_send_returns_the_message_ids(google):
    assert google.send_email("draft_1") == {"message_id": "msg_1", "thread_id": "thread_1"}


def test_send_is_verified_by_reading_the_label_back(google, backend_services):
    services, _ = backend_services
    assert google.message_in_sent("msg_1") is True
    services["gmail"]._responses["users.messages.get"] = {"labelIds": ["DRAFT"]}
    assert google.message_in_sent("msg_1") is False


def test_verification_failure_is_false_not_an_exception(google, backend_services):
    """A failed check means 'not verified', which the loop turns into BLOCKED."""
    services, _ = backend_services
    services["gmail"]._responses["users.messages.get"] = FakeHttpError(404, "gone")
    assert google.message_in_sent("msg_1") is False


def test_create_event_invites_everyone_and_sends_updates(google, backend_services):
    _, calls = backend_services
    slot = {"start": "2026-11-03T12:30:00+05:30", "end": "2026-11-03T13:00:00+05:30"}
    receipt = google.create_event(slot, [ME, RAHUL], "Q3 review")

    assert receipt["event_id"] == "evt_1"
    kwargs = dict(next(kw for path, kw in calls if path == "events.insert"))
    assert kwargs["sendUpdates"] == "all"
    assert [a["email"] for a in kwargs["body"]["attendees"]] == [ME, RAHUL]
    assert kwargs["body"]["start"]["timeZone"] == "Asia/Kolkata"


def test_create_event_refuses_an_empty_attendee_list(google):
    with pytest.raises(ToolError):
        google.create_event({"start": "x", "end": "y"}, [], "t")


def test_the_protocol_is_satisfied(google):
    """Same eight methods as the mock, so the loop cannot tell them apart."""
    from relay.tools import MockBackend

    for name in (
        "find_contact", "find_file", "find_free_slots", "draft_email", "get_draft",
        "send_email", "create_event", "message_in_sent", "event_on_calendar",
    ):
        assert callable(getattr(google, name)), name
        assert callable(getattr(MockBackend(), name)), name
