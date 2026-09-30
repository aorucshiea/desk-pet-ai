"""The swallowed-error counters must actually be observable.

Rationale lives in server.py next to _note_failure: the plugin tool table was
empty for a week and the only trace was a log.debug() the INFO file logger
never wrote. A fix nobody can see is a fix that regresses.
"""

from gateway import server as S


def _reset():
    S._silent_failures.clear()


def test_note_failure_counts_and_warns_where_the_file_logger_sees_it(caplog):
    _reset()
    try:
        raise ValueError("boom")
    except ValueError as exc:
        S._note_failure("unit_test_site", exc)

    assert S._silent_failures["unit_test_site"] == 1
    assert "swallowed error at unit_test_site (x1)" in caplog.text
    assert caplog.records[-1].levelname == "WARNING"


def test_logging_is_throttled_so_a_hot_path_cannot_flood_the_log():
    _reset()
    for i in range(45):
        S._note_failure("hot_site", RuntimeError("x"))
    assert S._silent_failures["hot_site"] == 45


def test_counters_are_isolated_per_site():
    _reset()
    S._note_failure("a", RuntimeError("1"))
    S._note_failure("a", RuntimeError("2"))
    S._count_failure("b")
    assert S._silent_failures == {"a": 2, "b": 1}


def test_diagnostics_endpoint_exposes_the_counters():
    """Read the counters over HTTP — the whole point is that a human or the
    settings UI can see a swallowed error without grepping a log file."""
    from fastapi.testclient import TestClient

    app = S.build_app(initial_model=None)
    _reset()
    S._count_failure("context_injection")
    S._count_failure("context_injection")
    with TestClient(app) as client:
        r = client.get("/api/diagnostics")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["swallowed_errors"]["context_injection"] == 2
