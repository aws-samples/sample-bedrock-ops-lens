"""Explicit unit conversions for stored aggregates.

the audit audit finding 02. `f_hourly_peak` is ingested with CloudWatch
`Period=3600, Stat=Sum`, so every count/token column in it is an **hourly
total**, not a per-minute rate and not a minute peak. Two consumers disagreed
about that: the burndown-risk widget compared the hourly total directly against
a per-minute quota (60x too high), and the Ops Review multiplied an hourly
request total by 60 to get "RPM" (3,600x the true hourly-average rate).

There is no minute-resolution source in this schema, so a TRUE minute peak is
not derivable. What we can state honestly is the *hourly-average* per-minute
rate: hourly_total / 60. Six thousand calls in one minute and six thousand
spread evenly across an hour share an hourly total of 6,000 but have real peaks
of 6,000 and 100 RPM respectively — so this value is a LOWER BOUND on the true
peak, and every label must say "hourly-average", never "peak".

Helpers here are deliberately tiny; the point is that exactly one definition
exists and both call sites import it.
"""
from __future__ import annotations

MINUTES_PER_HOUR = 60

# Name used in API payloads/labels so the approximation travels with the value.
PER_MINUTE_BASIS = "hourly_average"
PER_MINUTE_BASIS_LABEL = "hourly average (per-minute rate derived from hourly totals)"


def hourly_total_to_per_minute(hourly_total: float | int | None) -> float:
    """Convert an hourly SUM to the hourly-average per-minute rate.

    This is NOT a minute peak. See module docstring.
    """
    if hourly_total is None:
        return 0.0
    return float(hourly_total) / MINUTES_PER_HOUR


def max_hourly_total_to_per_minute(hourly_totals) -> float:
    """Busiest hour's hourly-average per-minute rate.

    Taking the max across hours and then dividing is identical to dividing then
    taking the max, but doing it in one helper keeps callers from inventing a
    different order (or forgetting the division entirely).
    """
    vals = [float(v) for v in hourly_totals if v is not None]
    if not vals:
        return 0.0
    return max(vals) / MINUTES_PER_HOUR


def utilization_pct(per_minute_rate: float | None,
                    per_minute_limit: float | None) -> float | None:
    """Percent of a per-minute quota consumed by a per-minute rate.

    Both arguments must already be per-minute. Returns None when the limit is
    unknown or non-positive — an unknown denominator must surface as unknown,
    never as 0% or as a percentage of some other model's limit.
    """
    if per_minute_rate is None or per_minute_limit is None:
        return None
    try:
        limit = float(per_minute_limit)
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    return 100.0 * float(per_minute_rate) / limit
