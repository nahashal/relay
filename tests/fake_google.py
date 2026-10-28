"""Service doubles shaped like googleapiclient's resource objects.

The real client builds chains - ``gmail.users().drafts().send(...).execute()`` -
so a double has to be callable at every link and return something with
``.execute()``. That shape is the only thing being imitated: these record what
was asked for and replay canned responses.

Deliberately NOT a mock of GoogleBackend. The point is to exercise the real
class: its query building, its response parsing, its error mapping.
"""

from __future__ import annotations

from typing import Any


class FakeRequest:
    """What a resource method returns: something with ``.execute()``."""

    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class Recorder:
    """A node in the resource chain that records calls and returns canned data.

    ``responses`` maps a dotted path - "users.drafts.send" - to either a value
    to return or an Exception to raise. ``calls`` collects (path, kwargs) so a
    test can assert what was actually asked of Google.
    """

    def __init__(
        self, responses: dict[str, Any], calls: list[tuple[str, dict]], path: str = ""
    ) -> None:
        self._responses = responses
        self.calls = calls
        self._path = path

    def __getattr__(self, name: str) -> "Recorder":
        if name.startswith("_"):
            raise AttributeError(name)
        return Recorder(self._responses, self.calls, f"{self._path}.{name}".lstrip("."))

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append((self._path, kwargs))
        if self._path in self._responses:
            return FakeRequest(self._responses[self._path])
        # A link in the chain with no canned response, e.g. users() before
        # drafts(): keep walking.
        return Recorder(self._responses, self.calls, self._path)


def fake_services(**responses: Any) -> tuple[dict[str, Any], list[tuple[str, dict]]]:
    """Build the four clients from a flat map of ``service__dotted__path``.

    ``drive__files__list=...`` becomes drive's "files.list" response. Double
    underscores because a Python keyword argument cannot contain a dot.
    """
    calls: list[tuple[str, dict]] = []
    per_service: dict[str, dict[str, Any]] = {
        "gmail": {},
        "calendar": {},
        "drive": {},
        "people": {},
    }
    for key, value in responses.items():
        service, _, rest = key.partition("__")
        if service not in per_service:
            raise KeyError(f"unknown service {service!r}")
        per_service[service][rest.replace("__", ".")] = value

    services = {
        name: Recorder(paths, calls) for name, paths in per_service.items()
    }
    return services, calls


class FakeHttpError(Exception):
    """Stands in for googleapiclient.errors.HttpError.

    GoogleBackend._call catches HttpError specifically and everything else
    generically, so this takes the generic path - which is the one that must
    also produce a clean ToolError.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.resp = type("Resp", (), {"status": status})()
