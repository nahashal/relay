"""Falling back to another model - on the failures another model can fix.

Free-tier models are overloaded often; three runs in a row died on "experiencing
high demand" during testing. A second model usually answers at once.

What makes this safe rather than sloppy: reading a goal has no side effect, so
Lesson 26's rule (never retry a send) does not apply. And a rejected API key
fails identically on every model, so falling back there would turn one clear
error into five slow ones.
"""

from __future__ import annotations

import pytest

from relay.planner import (
    GeminiIntentReader,
    _env_fallbacks,
    is_transient_model_error,
)


class ApiError(Exception):
    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message or str(code))
        self.code = code


# --------------------------------------------------------------------------- #
# Which failures justify trying another model
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exc",
    [
        ApiError(503, "UNAVAILABLE high demand"),
        ApiError(429, "RESOURCE_EXHAUSTED"),
        ApiError(429, "RESOURCE_EXHAUSTED limit: 0"),  # paid-only: another may be free
        ApiError(404, "NOT_FOUND"),
    ],
)
def test_these_are_worth_another_model(exc):
    assert is_transient_model_error(exc)


@pytest.mark.parametrize(
    "exc",
    [
        ApiError(401, "API key not valid"),
        ApiError(403, "PERMISSION_DENIED"),
    ],
)
def test_a_key_problem_is_not(exc):
    """The key is wrong for every model; five more attempts help nobody."""
    assert not is_transient_model_error(exc)


def test_an_unknown_error_does_not_trigger_a_fallback():
    """Unrecognised means unknown, and an unknown failure is not evidence that
    a different model would do better."""
    assert not is_transient_model_error(RuntimeError("something odd"))


# --------------------------------------------------------------------------- #
# Reading the configuration
# --------------------------------------------------------------------------- #


def test_fallbacks_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "a, b ,c")
    assert _env_fallbacks() == ["a", "b", "c"]


def test_the_older_singular_name_still_works(monkeypatch):
    """`.env.example` shipped GEMINI_FALLBACK_MODEL long before this existed."""
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "only-one")
    assert _env_fallbacks() == ["only-one"]


def test_no_configuration_means_no_fallbacks(monkeypatch):
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    assert _env_fallbacks() == []


# --------------------------------------------------------------------------- #
# The order the reader will try
# --------------------------------------------------------------------------- #


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return lambda model, fallbacks: GeminiIntentReader(
        model=model, api_key="test-key", fallbacks=fallbacks
    )


def test_the_primary_comes_first(reader):
    assert reader("primary", ["second", "third"]).models == ["primary", "second", "third"]


def test_a_duplicate_fallback_is_dropped(reader):
    """Naming the primary again would make a failure take twice as long."""
    assert reader("primary", ["primary", "second"]).models == ["primary", "second"]


def test_no_fallbacks_means_one_model(reader):
    assert reader("primary", []).models == ["primary"]


def test_nothing_has_answered_yet(reader):
    assert reader("primary", ["second"]).last_model_used is None


def test_a_missing_model_is_still_refused(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_MODEL"):
        GeminiIntentReader(api_key="k", fallbacks=["something"])
