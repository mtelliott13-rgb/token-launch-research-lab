import logging

from radar.faillog import FailureLog


def test_repeated_failure_is_logged_once_then_with_a_count(caplog):
    clock = [0.0]
    failures = FailureLog(logging.getLogger("t"), every_s=600, clock=lambda: clock[0])
    with caplog.at_level(logging.INFO, logger="t"):
        for _ in range(300):               # every 10 seconds for 50 minutes
            failures.failed("rugcheck", "rugcheck: request failed (HTTP 503)")
            clock[0] += 10
        failures.ok("rugcheck")
        failures.ok("rugcheck")
        failures.failed("rugcheck", "rugcheck: request failed (HTTP 503)")
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1 + 4 + 1 + 1
    assert messages[0] == "rugcheck: request failed (HTTP 503)"
    assert messages[1] == "rugcheck: request failed (HTTP 503) (failed 61 times in a row)"
    assert messages[5] == "rugcheck: working again after 300 failures in a row"
    assert messages[6] == messages[0]


def test_keys_are_counted_separately_and_one_failure_recovers_quietly(caplog):
    failures = FailureLog(logging.getLogger("t"), clock=lambda: 0.0)
    with caplog.at_level(logging.INFO, logger="t"):
        failures.failed("a", "a failed")
        failures.failed("b", "b failed")
        failures.ok("a")
    assert [r.getMessage() for r in caplog.records] == ["a failed", "b failed"]
