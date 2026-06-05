#!/usr/bin/env python3
"""Smoke-test every API endpoint against a running backend."""
import json
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = "http://127.0.0.1:8001/api"

PATHS = [
    "/health",
    "/ingestion-status",
    "/summary?days=7",
    "/daily-trend?days=7",
    "/daily-breakdown?days=7&group_by=model",
    "/daily-breakdown?days=7&group_by=provider",
    "/daily-breakdown?days=7&group_by=traffic",
    "/daily-breakdown?days=7&group_by=region",
    "/requests-by-model?days=7",
    "/traffic-types?days=7",
    "/operations?days=7",
    "/regions?days=7",
    "/errors-by-model?days=7",
    "/errors-by-account?days=7",
    "/errors-daily-trend?days=7",
    "/latency-by-model?days=7",
    "/latency-cris-vs-od?days=7",
    "/operation-latency?days=7",
    "/hourly-heatmap?days=7",
    "/ops-cris-adoption?days=7",
    "/ops-cris-by-account?days=7",
    "/ops-throttle-rate?days=7",
    "/ops-peak-rpm?days=7",
    "/ops-burndown-risk?days=7",
    "/ops-caching?days=7",
    "/ops-context-length?days=7",
    "/ops-request-shape?days=7",
    "/ops-service-tier?days=7",
    "/ops-inference-profile?days=7",
    "/tags",
    "/tags/team/values",
    "/tags/team/values?q=plat",
    "/preferences",
    "/ops-review?days=7",
]


def hit(path):
    url = BASE + path
    try:
        with urlopen(url, timeout=10) as r:
            body = r.read()
            data = json.loads(body)
            shape = (
                f"{len(data)} rows" if isinstance(data, list)
                else f"obj keys={sorted(data.keys())[:6]}" if isinstance(data, dict)
                else type(data).__name__
            )
            return r.getcode(), shape, len(body), None
    except HTTPError as e:
        return e.code, None, 0, e.read().decode("utf-8", "replace")[:300]
    except URLError as e:
        return 0, None, 0, str(e)
    except Exception as e:
        return 0, None, 0, f"{type(e).__name__}: {e}"


def main():
    failed = 0
    for p in PATHS:
        code, shape, size, err = hit(p)
        ok = code == 200
        marker = "✓" if ok else "✗"
        if ok:
            print(f"  {marker}  {code}  {p:<50s}  {shape}  ({size:,} B)")
        else:
            failed += 1
            print(f"  {marker}  {code}  {p:<50s}  ERROR")
            if err:
                print(f"        {err}")
    print()
    if failed:
        print(f"FAILED: {failed}/{len(PATHS)} endpoints")
        return 1
    print(f"PASSED: all {len(PATHS)} endpoints")
    return 0


if __name__ == "__main__":
    sys.exit(main())
