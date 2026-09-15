"""Model-lifecycle policy regimes and EOL retention.

Bedrock now has TWO lifecycle policies and the dashboard only understood one:

    legacy policy   (launched before 2026-09-07) — >=12 months on Bedrock,
                    >=6 months in Legacy, plus a public-extended-access phase
                    (provider-set price rises) for EOL dates after 2026-02-01.
    current policy  (launched on or after 2026-09-07) — no 12-month floor, NO
                    extended-access phase at all, and a per-model Legacy period
                    of either 6 months OR 45 days.

    https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle-legacy.html
    https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html

Two bugs follow from that, and these tests pin both.

(1) Extended access was applied to every model. Under the current policy that
    phase does not exist, so scoring a current-policy model against it invents
    a grace period AWS never promised. Worse in the other direction too: a
    current-policy model can go from Legacy to dead in 45 days with no
    extended-access milestone to escalate on, so without an EOL-proximity rule
    it sat at "warning" until the day requests started failing.

(2) Past-EOL models vanish. Verified against the live API: once a model passes
    EOL, ListFoundationModels stops returning it and GetFoundationModel raises
    ResourceNotFoundException. The ingester used to DELETE the table and
    re-INSERT, destroying the row on the next run — so "past EOL" was
    unreportable, and a customer still calling a dead model saw an unexplained
    4xx spike with no model attribution.

No fixtures and no seeded rows here: these are pure functions over dates, which
is exactly the part that was wrong. The UPSERT itself is SQL and is covered by
deploying it, not by mocking asyncpg.

Run: .venv/bin/python -m pytest tests/test_model_lifecycle_policy.py -q
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ingestion.model_lifecycle import (  # noqa: E402
    POLICY_CURRENT,
    POLICY_CUTOFF,
    POLICY_LEGACY,
    classify_policy,
    notice_period_days,
)
from backend.app.routers.model_lifecycle import (  # noqa: E402
    EOL_IMMINENT_DAYS,
    _notice_period_label,
    _severity,
)


def _utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# classify_policy — the regime discriminator
# --------------------------------------------------------------------------- #
class TestClassifyPolicy:
    def test_cutoff_is_2026_09_07_utc_midnight(self):
        # AWS states the cutoff as a date, so midnight UTC is the boundary.
        assert POLICY_CUTOFF == _utc(2026, 9, 7)

    def test_launched_before_cutoff_is_legacy_policy(self):
        assert classify_policy(_utc(2024, 3, 7)) == POLICY_LEGACY

    def test_launched_on_cutoff_is_current_policy(self):
        # "on or after September 7, 2026" — the cutoff day itself is current.
        assert classify_policy(_utc(2026, 9, 7)) == POLICY_CURRENT

    def test_day_before_cutoff_is_legacy_policy(self):
        # One second earlier flips the regime; this is the boundary that decides
        # whether extended-access scoring applies at all.
        assert classify_policy(_utc(2026, 9, 7) - timedelta(seconds=1)) == POLICY_LEGACY

    def test_launched_after_cutoff_is_current_policy(self):
        assert classify_policy(_utc(2027, 1, 15)) == POLICY_CURRENT

    def test_missing_start_of_life_is_unknown_not_guessed(self):
        # The API omits startOfLifeTime for some models. Returning None keeps
        # "we don't know" distinguishable from "we determined it's legacy" —
        # the UI shows Unknown rather than promising the wrong notice period.
        assert classify_policy(None) is None


# --------------------------------------------------------------------------- #
# notice_period_days — how much warning the model actually gave
# --------------------------------------------------------------------------- #
class TestNoticePeriodDays:
    def test_six_month_legacy_period(self):
        # The legacy policy's ">=6 months in Legacy" lands near 184 days, not
        # exactly 180 — months are not 30 days.
        assert notice_period_days(_utc(2026, 3, 1), _utc(2026, 9, 1)) == 184

    def test_forty_five_day_legacy_period(self):
        # The current policy's short option, verbatim from the docs.
        assert notice_period_days(_utc(2027, 1, 1), _utc(2027, 2, 15)) == 45

    def test_null_until_the_model_enters_legacy(self):
        # An ACTIVE model has no legacyTime and no endOfLifeTime, and the
        # current policy's declared Legacy period is model-card-only, so there
        # is genuinely nothing to compute.
        assert notice_period_days(None, _utc(2027, 2, 15)) is None
        assert notice_period_days(_utc(2027, 1, 1), None) is None
        assert notice_period_days(None, None) is None

    def test_truncates_to_whole_days(self):
        legacy = _utc(2027, 1, 1)
        assert notice_period_days(legacy, legacy + timedelta(days=45, hours=23)) == 45


# --------------------------------------------------------------------------- #
# _notice_period_label — buckets, because observed gaps are never exact
# --------------------------------------------------------------------------- #
class TestNoticePeriodLabel:
    def test_45_day_bucket(self):
        assert _notice_period_label(45) == "45 days"

    def test_six_month_bucket_accepts_real_calendar_variance(self):
        # Real 6-month spans measure 181-184 days depending on which months
        # they cross. Comparing to a single number would mislabel most of them.
        for days in (181, 182, 183, 184, 185):
            assert _notice_period_label(days) == "6 months", days

    def test_unrecognised_gap_reports_the_raw_count(self):
        # Don't force a value into a bucket it doesn't belong to — a 300-day
        # notice period is real data, and "6 months" would be a lie.
        assert _notice_period_label(300) == "300 days"
        assert _notice_period_label(90) == "90 days"

    def test_none_stays_none(self):
        assert _notice_period_label(None) is None


# --------------------------------------------------------------------------- #
# _severity — the part that was wrong for current-policy models
# --------------------------------------------------------------------------- #
class TestSeverity:
    TODAY = date(2027, 6, 1)

    def test_past_eol_is_critical(self):
        # This is the case that used to be unreachable: the row was deleted
        # before the API could report it.
        assert _severity(self.TODAY, date(2026, 1, 1), None,
                         date(2027, 5, 1), POLICY_LEGACY) == "critical"

    def test_eol_today_is_critical(self):
        assert _severity(self.TODAY, date(2026, 1, 1), None,
                         self.TODAY, POLICY_CURRENT) == "critical"

    def test_imminent_eol_is_critical_under_current_policy(self):
        # The whole point of EOL_IMMINENT_DAYS: a current-policy model has no
        # extended-access milestone, so this is the only escalation available
        # before requests start failing.
        eol = self.TODAY + timedelta(days=EOL_IMMINENT_DAYS - 1)
        assert _severity(self.TODAY, date(2027, 4, 20), None,
                         eol, POLICY_CURRENT) == "critical"

    def test_eol_just_beyond_the_imminent_window_is_only_warning(self):
        eol = self.TODAY + timedelta(days=EOL_IMMINENT_DAYS + 1)
        assert _severity(self.TODAY, date(2027, 4, 20), None,
                         eol, POLICY_CURRENT) == "warning"

    def test_extended_access_ignored_for_current_policy(self):
        # THE bug. Current-policy models have no extended-access phase, so even
        # a past extended-access date (which shouldn't be populated at all) must
        # not manufacture a critical. Legacy start is in the past → warning.
        assert _severity(self.TODAY, date(2027, 1, 1), date(2027, 3, 1),
                         None, POLICY_CURRENT) == "warning"

    def test_extended_access_still_escalates_legacy_policy(self):
        # Same inputs, legacy regime: past extended access means active users
        # are now paying provider-set premium pricing. Still critical.
        assert _severity(self.TODAY, date(2027, 1, 1), date(2027, 3, 1),
                         None, POLICY_LEGACY) == "critical"

    def test_unknown_policy_treated_as_legacy(self):
        # Conservative default: with no startOfLifeTime we can't rule out an
        # extended-access phase, and over-warning is recoverable where
        # under-warning is not.
        assert _severity(self.TODAY, date(2027, 1, 1), date(2027, 3, 1),
                         None, None) == "critical"

    def test_in_legacy_is_warning(self):
        assert _severity(self.TODAY, date(2027, 5, 1), None, None,
                         POLICY_CURRENT) == "warning"

    def test_legacy_within_90_days_is_info(self):
        assert _severity(self.TODAY, self.TODAY + timedelta(days=60), None,
                         None, POLICY_LEGACY) == "info"

    def test_distant_legacy_date_is_active_and_hidden(self):
        # 'active' is the router's signal to drop the row entirely — the user
        # asked for legacy/EOL only, not a catalogue of healthy models.
        assert _severity(self.TODAY, self.TODAY + timedelta(days=200), None,
                         None, POLICY_LEGACY) == "active"

    def test_no_dates_at_all_is_active(self):
        assert _severity(self.TODAY, None, None, None, POLICY_LEGACY) == "active"

    def test_policy_defaults_to_legacy_when_omitted(self):
        # Backwards compatibility: callers that predate the regime work keep
        # their old behaviour rather than silently losing the extended-access
        # rule.
        assert _severity(self.TODAY, date(2027, 1, 1),
                         date(2027, 3, 1), None) == "critical"


# --------------------------------------------------------------------------- #
# End-to-end regime scenarios, with the dates AWS actually published
# --------------------------------------------------------------------------- #
class TestRealisticScenarios:
    def test_legacy_policy_model_with_extended_access(self):
        """A pre-cutoff model: 6-month Legacy period, extended access before EOL."""
        start = _utc(2024, 3, 7)
        legacy = _utc(2026, 3, 1)
        eol = _utc(2026, 9, 1)
        assert classify_policy(start) == POLICY_LEGACY
        assert _notice_period_label(notice_period_days(legacy, eol)) == "6 months"

    def test_current_policy_model_on_the_45_day_track(self):
        """A post-cutoff model given the minimum notice: 45 days, no extended access.

        This is the combination the old code handled worst — it would have
        looked for an extended-access date that will never exist, and reported
        only "warning" for the entire 45 days.
        """
        start = _utc(2026, 10, 1)
        legacy = _utc(2027, 5, 20)
        eol = _utc(2027, 7, 4)
        assert classify_policy(start) == POLICY_CURRENT
        days = notice_period_days(legacy, eol)
        assert days == 45
        assert _notice_period_label(days) == "45 days"
        # 30 days out, with no extended-access date, it must still be critical.
        assert _severity(date(2027, 6, 10), legacy.date(), None, eol.date(),
                         POLICY_CURRENT) == "critical"

    def test_retained_past_eol_model_stays_critical_forever(self):
        """A model AWS deleted from the API keeps reporting critical.

        The retained row's dates never change, so severity does not decay as
        the EOL recedes into the past. That is intended: traffic against a dead
        model is 100 % failures no matter how long ago it died.
        """
        eol = date(2027, 1, 1)
        for today in (date(2027, 1, 2), date(2027, 6, 1), date(2028, 1, 1)):
            assert _severity(today, date(2026, 7, 1), None, eol,
                             POLICY_LEGACY) == "critical", today
