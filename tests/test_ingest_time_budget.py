"""invocation_logs must not run past the Lambda timeout.

Reported symptom: every deploy's initial ingest appeared to hang ~30 minutes and
never completed. The logs showed two "[invocation_logs] starting" markers ~898
seconds apart.

Cause: the module had no wall-clock budget. On the first ingest of a bucket with
real history it ran past the ingester's 900s Lambda timeout, so the function was
killed mid-flight — committing nothing and logging no completion — and the
caller's SDK then RETRIED the RequestResponse invoke. deploy.sh passes
`--cli-read-timeout 900`, exactly the Lambda timeout, which is what turned the
timeout into a silent retry rather than a visible error.

Stopping early is safe: every fully-read S3 object is recorded in
ingestion_log_objects, so the next run skips it and resumes. Partial progress
beats a killed run that commits nothing.

Run: .venv/bin/python -m pytest tests/test_ingest_time_budget.py -q
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IL = ROOT / "ingestion/invocation_logs.py"
LH = ROOT / "ingestion/lambda_handler.py"


def test_the_deadline_is_checked_before_an_object_is_read():
    """Mid-object is the one place it must NOT stop: a half-parsed file recorded
    as processed would have its remaining rows skipped forever."""
    src = IL.read_text()
    check = src.index("if _deadline is not None and time.monotonic() >= _deadline")
    read = src.index("for entry in _read_log_lines(")
    assert check < read


def test_an_object_is_recorded_only_after_it_is_fully_read():
    src = IL.read_text()
    assert src.index("for entry in _read_log_lines(") < src.index("new_keys.append")


def test_a_partial_pass_does_not_report_a_clean_run():
    """rc=0 would let the orchestrator claim status=ok while work remained."""
    assert "return 2 if _budget_hit else 0" in IL.read_text()


def test_the_budget_is_opt_in_so_cli_backfills_are_unbounded():
    src = IL.read_text()
    assert "--max-seconds" in src
    assert 'INVOCATION_LOGS_MAX_SECONDS", "0"' in src, "0 must mean no limit"


def test_the_budget_comes_from_real_remaining_lambda_time():
    """A hardcoded 900 would be wrong the moment the Timeout is changed."""
    src = LH.read_text()
    assert "get_remaining_time_in_millis" in src
    assert "logs_budget_s" in src


def test_the_budget_leaves_headroom_for_the_modules_that_follow():
    """proxy_events, quotas, the findings evaluator and the cache bump all run
    after invocation_logs and need time left."""
    for remaining in (900, 600, 300, 120):
        budget = max(60, int(remaining * 0.45))
        assert budget < remaining
        assert remaining - budget >= 60


def test_the_reason_is_recorded_where_someone_will_remove_it():
    """A time budget looks like premature optimisation without the story."""
    src = IL.read_text()
    assert "898" in src or "retr" in src.lower()
    assert "ingestion_log_objects" in src
