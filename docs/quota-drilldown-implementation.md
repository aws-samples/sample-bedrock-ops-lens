# Quota drill-down: implementation guide

A focused diagnostic view that answers "is this specific workload near
its quota?" An oncall picks one (account, model, region) tuple, and
the page shows hourly peak Tokens-per-Minute and Requests-per-Minute
over the last 14 days, with the AWS Service Quotas applied limit
plotted as a red horizontal line.

This doc explains how every piece works in enough detail that another
engineer (or another agent) can rebuild it from scratch, port it to a
different stack, or extend it with new metrics.

---

## 1. The product question

> "I just got paged about throttle errors on the Claude Opus 4.7
>  workload in account X, region us-east-1. Are we near the quota?"

Fleet-wide views average that signal away. The drill-down narrows to
one tuple so the answer is yes/no:

- If util ≥ 100% → request a quota increase or shift to CRIS.
- If util 70-99% → no growth runway; plan an increase.
- If util < 30% but throttle errors are real → cap is somewhere else
  (per-key throttling, regional incident, etc.). Look at the Errors
  tab.

The reference design is the AWS-internal CRIS dashboard chart that a
colleague shared (peak TPM and peak RPM, each plotted against a
dashed quota line, with a KPI strip showing Limit / Peak / Avg / Util %).

---

## 2. Data sources

Two existing tables. Nothing new added to the schema for this feature.

### 2.1 `f_hourly_peak`

Hourly CloudWatch counters for every (date, hour, account, modelId,
region) tuple, populated by the CW Metrics ingester (`ingestion/cw_metrics.py`).

```sql
CREATE TABLE f_hourly_peak (
    event_date         DATE NOT NULL,
    hour               SMALLINT NOT NULL,
    accountId          TEXT NOT NULL,
    modelId            TEXT NOT NULL,    -- 'us.anthropic.claude-opus-4-7-v1:0'
    region             TEXT NOT NULL,
    total_requests     BIGINT NOT NULL,
    total_input_tokens BIGINT,
    total_output_tokens BIGINT,
    status_429_count   BIGINT,
    PRIMARY KEY (event_date, hour, accountId, modelId, region)
);
```

CloudWatch ingester pulls these at `Period=3600` (one bucket per
hour) for the last 14 days. That hourly granularity is the limiting
factor: every "per-minute" rate the chart shows is derived (see §4).

### 2.2 `f_quotas`

Service Quotas snapshot keyed by (account, region, quota_code).

```sql
CREATE TABLE f_quotas (
    accountId       TEXT NOT NULL,
    region          TEXT NOT NULL,
    quota_code      TEXT NOT NULL,    -- e.g. L-5DB28B7B
    quota_name      TEXT NOT NULL,    -- 'Cross-region model inference tokens per minute for Anthropic Claude Opus 4.7'
    model_name      TEXT NOT NULL,    -- 'Anthropic Claude Opus 4.7'
    traffic_type    TEXT NOT NULL,    -- 'On-demand' | 'Cross-region' | 'Global cross-region'
    metric          TEXT NOT NULL,    -- 'TPM' | 'RPM'
    default_value   DOUBLE PRECISION,
    applied_value   DOUBLE PRECISION, -- post quota-increase, falls back to default
    PRIMARY KEY (accountId, region, quota_code)
);
```

Populated by `ingestion/quotas.py`, which calls
`service-quotas:ListServiceQuotas` and `ListAWSDefaultServiceQuotas`,
parses the `QuotaName` strings with this regex:

```
(On-demand|Cross-region|Global cross-region) model inference (requests|tokens) per minute for (.+)
```

…and breaks each row into `(traffic_type, metric, model_name)`.

### 2.3 The naming gap

`f_hourly_peak` keys on technical model IDs:
`us.anthropic.claude-opus-4-7-v1:0`. `f_quotas` keys on display
names: `Anthropic Claude Opus 4.7`. They never match exactly. The
backend has to fuzz-match (see §3.3).

---

## 3. Backend: `/api/quota-drilldown`

File: `backend/app/routers/quota_drilldown.py`. Registered in
`backend/app/main.py` alongside the other routers.

Two endpoints.

### 3.1 `GET /api/quota-drilldown/options`

Populates the UI's "pick a tuple" dropdown.

```python
@router.get("/quota-drilldown/options")
async def drilldown_options(days: int = Query(14, ge=1, le=90)):
    rows = await db.fetch(
        """
        SELECT accountId, modelId, region,
               SUM(total_requests)::BIGINT AS total_requests
        FROM f_hourly_peak
        WHERE event_date >= current_date - $1::int
        GROUP BY accountId, modelId, region
        HAVING SUM(total_requests) > 0
        ORDER BY total_requests DESC
        LIMIT 500
        """,
        days,
    )
    out = [
        {
            "accountId": r["accountid"] if "accountid" in r else r["accountId"],
            "modelId":   r["modelid"]   if "modelid"   in r else r["modelId"],
            "region":    r["region"],
            "total_requests": int(r["total_requests"] or 0),
            "label": f"{acct} · {mid} · {r['region']}",
        }
        for r in rows
    ]
    return {"options": out}
```

Three things to note:

- Ordered by volume so the busiest workload is the auto-selected
  default — when an oncall opens the tab cold during an incident,
  the most likely target is already shown.
- `LIMIT 500` is there to keep payload size sane on huge multi-account
  fleets. Not a real concern today.
- `accountid`/`accountId` casing: asyncpg returns lower-cased column
  names if Postgres treats the column as case-insensitive identifier.
  Coalesce both.

### 3.2 `GET /api/quota-drilldown`

The main endpoint. Takes `account_id`, `model_id`, `region`, `days`.
Returns a series + the matched limits + KPIs.

```python
@router.get("/quota-drilldown")
async def quota_drilldown(
    account_id: str = Query(..., min_length=12, max_length=12),
    model_id:   str = Query(..., min_length=1,  max_length=200),
    region:     str = Query(..., min_length=1,  max_length=40),
    days:       int = Query(14,  ge=1, le=90),
):
    if not account_id.isdigit():
        raise HTTPException(400, "account_id must be 12 digits")
    ...
```

It runs three steps in sequence: time-series query, quota match,
KPI rollup.

#### Step 1: Time series

Hourly buckets normalised to per-minute by dividing by 60. Both input
and output tokens contribute to TPM (matches AWS Bedrock's quota
definition).

```sql
SELECT
  (event_date::timestamp + (hour || ' hours')::interval) AS ts,
  total_requests::float / 60.0                            AS rpm,
  (COALESCE(total_input_tokens,  0)
   + COALESCE(total_output_tokens, 0))::float / 60.0      AS tpm,
  COALESCE(total_input_tokens,  0)::float / 60.0          AS input_tpm,
  COALESCE(total_output_tokens, 0)::float / 60.0          AS output_tpm,
  COALESCE(status_429_count, 0)::float    / 60.0          AS error_rpm
FROM f_hourly_peak
WHERE accountId = $1 AND modelId = $2 AND region = $3
  AND event_date >= current_date - $4::int
ORDER BY ts
```

The "per minute" labelling is faithful to what AWS shows in their own
CRIS dashboard for the same data: hourly peak ÷ 60 = average rate
during that hour. Real minute-level granularity is not available
without rebuilding the ingester at `Period=60`, which 60x's payload
size and hits CloudWatch's 14-day retention cliff at 1-min resolution
(vs. 455 days at 1-hour).

#### Step 2: Quota match

Fetch every quota row for `(accountId, region)`, then fuzzy-match
each `model_name` against the requested `model_id`.

```python
def _strip_seps(s: str) -> str:
    """Strip every separator AWS uses in model names or IDs so '4.7'
    matches '4-7' after canonicalization."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _matches(model_name: str, model_id: str) -> bool:
    if not model_name or not model_id:
        return False
    mid_canon = _strip_seps(model_id)
    name_lower = model_name.lower()
    if _strip_seps(name_lower) and _strip_seps(name_lower) in mid_canon:
        return True
    noise = {"anthropic","amazon","meta","mistral","ai21","cohere",
             "stability","deepseek","openai","ai","labs","the","for",
             "model","version"}
    tokens = [t for t in name_lower.replace(",", " ").replace("-", " ").split()
              if t and t not in noise]
    if not tokens:
        return False
    return all(_strip_seps(t) in mid_canon for t in tokens)
```

The trick is **canonicalising both sides to alnum-only**. Without
that, `"4.7"` (in the quota name) doesn't substring-match `"4-7-"`
(in the modelId), and the chart silently shows "Limit: unknown".

##### Independent TPM / RPM picks

AWS exposes TPM and RPM as separate quota_code rows. Those rows can
have **subtly different `model_name` strings** depending on how AWS
formatted the catalog — one might say "Anthropic Claude" while the
other says just "Claude". Picking a single (model_name, traffic_type)
"family" and reading both metrics from it loses the metric whose
name didn't normalise the same way.

Fix: match TPM and RPM independently, but bias both toward the
traffic_type the modelId belongs to. If the modelId starts with
`global.`, prefer Global cross-region; if it starts with any other
CRIS prefix (`us.` / `eu.` / etc.), prefer Cross-region; otherwise
On-demand.

```python
if model_id.startswith("global."):
    prefer_tt = "Global cross-region"
elif any(model_id.startswith(p + ".") for p in ("us","eu","apac","jp","au","ca","amer")):
    prefer_tt = "Cross-region"
else:
    prefer_tt = "On-demand"

def _pick(metric: str) -> tuple[float | None, str | None]:
    candidates = []
    for q in quota_rows:
        if q["metric"] != metric:
            continue
        if not _matches(q["model_name"], model_id):
            continue
        val = q["applied_value"] if q["applied_value"] is not None else q["default_value"]
        if val is None:
            continue
        candidates.append((q["traffic_type"], float(val), q["model_name"]))
    if not candidates:
        return None, None
    candidates.sort(key=lambda c: (c[0] != prefer_tt, -c[1]))
    return candidates[0][1], candidates[0][0]

tpm_limit, tpm_traffic = _pick("TPM")
rpm_limit, rpm_traffic = _pick("RPM")
```

Sort key `(c[0] != prefer_tt, -c[1])` means: rows whose traffic_type
matches the modelId's CRIS prefix come first, and within the
matching set the highest applied_value wins (a customer-requested
quota increase trumps the default).

##### Derived RPM ceiling

For some models AWS does not publish a per-model RPM quota at all.
Claude Opus 4.7 is one example: it has TPM (30M) but no RPM.

In that case the workload's *effective* request ceiling is still
constrained by TPM — you'd hit 30M TPM at roughly
`30M / avg_tokens_per_request` RPM. That number is computed on the
fly:

```python
rpm_limit_derived: float | None = None
if rpm_limit is None and tpm_limit and peak_rpm > 0:
    avg_tokens_per_req = (sum_tpm / sum_rpm) if sum_rpm > 0 else 0.0
    if avg_tokens_per_req > 0:
        rpm_limit_derived = tpm_limit / avg_tokens_per_req
        util_rpm = (peak_rpm / rpm_limit_derived * 100.0)
```

It's labelled clearly as "Effective ceiling (derived from TPM ÷ avg
tokens/req)" in the UI so users never confuse it with a published
quota.

#### Step 3: KPI rollup

Standard reduce over the series:

- `peak_tpm = max(tpm)` and `peak_tpm_at = ts at argmax`
- `avg_tpm = mean(tpm)` (simple mean, not weighted — peaks are what
  matter for quota analysis)
- `util_pct_tpm = peak_tpm / tpm_limit * 100`
- Same for RPM.

#### Response shape

```json
{
  "series": [
    {"ts":"...", "tpm":1234.5, "rpm":2.0, "input_tpm":..., "output_tpm":..., "error_rpm":...},
    ...
  ],
  "tpm_limit": 30000000.0,
  "rpm_limit": null,
  "rpm_limit_derived": 19500.0,
  "matched_quota_traffic_type": "Cross-region",
  "kpis": {
    "peak_tpm": 7800.0, "peak_tpm_at": "...", "avg_tpm": 1100.0, "util_pct_tpm": 0.026,
    "peak_rpm": 4.2,    "peak_rpm_at": "...", "avg_rpm": 0.91,   "util_pct_rpm": 0.022
  }
}
```

---

## 4. Frontend: `QuotaDrillDownTab.jsx`

File: `frontend/src/tabs/QuotaDrillDownTab.jsx`. Rendered as a
section inside `QuotasTab.jsx` (see §6 for placement).

### 4.1 Tuple picker

```jsx
const opts = useApi('/quota-drilldown/options', { days: 14 }, []);
const optionList = useMemo(() => (opts.data?.options || []).map(o => ({
  label: o.label,
  value: `${o.accountId}|${o.modelId}|${o.region}`,
  description: `${fmt(o.total_requests)} requests in last 14d`,
  _raw: o,
})), [opts.data]);

const [selected, setSelected] = useState(null);
const effective = selected || optionList[0] || null;   // auto-pick busiest
```

Cloudscape `<Select>` with `filteringType="auto"` for the dropdown.
The auto-selection on first render is critical UX: the page is
already showing real data the moment it loads, so an oncall who
opens it during an incident sees a usable chart immediately and
only switches if they need a different tuple.

### 4.2 Conditional fetch

The standard `useApi()` hook can't be used here because the request
must NOT fire until `effective` is set (otherwise the backend gets
empty params). Manual `useEffect`:

```jsx
const [data, setData] = useState(null);
const [loading, setLoading] = useState(false);
useEffect(() => {
  if (!effective) { setData(null); return; }
  let cancelled = false;
  setLoading(true);
  api('/quota-drilldown', {
    account_id: effective._raw.accountId,
    model_id:   effective._raw.modelId,
    region:     effective._raw.region,
    days: 14,
  })
    .then(d => { if (!cancelled) { setData(d); setLoading(false); } })
    .catch(e => { if (!cancelled) { setError(e); setLoading(false); } });
  return () => { cancelled = true; };
}, [effective]);
```

### 4.3 Two metric cards, side by side

```jsx
<div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, alignItems: 'stretch' }}>
  <MetricCard
    title="Tokens per minute (TPM)"
    series={tpmSeries}
    limit={data?.tpm_limit ?? null}
    peak={k.peak_tpm} peakAt={k.peak_tpm_at}
    avg={k.avg_tpm}   util={k.util_pct_tpm}
    fmtVal={fmt}
    sectionId="quota-drilldown-tpm"
    onInfo={onInfo}
  />
  <MetricCard
    title="Requests per minute (RPM)"
    series={rpmSeries}
    limit={data?.rpm_limit ?? null}
    limitDerived={data?.rpm_limit_derived ?? null}
    peak={k.peak_rpm} peakAt={k.peak_rpm_at}
    avg={k.avg_rpm}   util={k.util_pct_rpm}
    fmtVal={fmt}
    sectionId="quota-drilldown-rpm"
    onInfo={onInfo}
  />
</div>
```

The grid layout pattern is critical. Cloudscape's `<ColumnLayout>`
does NOT set `align-items: stretch`, and the default `<Container>`
is `height: auto`. If the two cards have different content heights
you get visible misalignment. Plain CSS grid + `align-items:
stretch` + `<Container fitHeight>` on each card guarantees identical
heights regardless of content. Same pattern is used across Overview,
Capacity & Adoption, and Model Insights tabs (see commit
`3ba41bf`).

### 4.4 The MetricCard internals

Card layout:

```
┌─────────────────────────────────────────────────────┐
│ Tokens per minute (TPM)                       Info  │  <-- Header with optional Info link
├─────────────────────────────────────────────────────┤
│ Limit: 30M · Peak: 7.8K @ May 25 20:00 ·            │  <-- KPI strip
│   Avg: 1.1K · Util: ✓ 0.0%                          │
│                                                     │
│ Y-axis is logarithmic so both peak usage and the    │  <-- Note (only when log scale)
│ much higher limit fit on the same chart.            │
│                                                     │
│  30.0M ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ <-- red    │  <-- Limit line
│  10.0M                                              │
│   1.0M                                              │
│ 100.0K                                              │
│  10.0K  ╱╲                                          │
│   1.0K  ╲ ╲╱╲╱╲   ╱╲    ╲╱╲╱╲╱╲╱╲                   │  <-- Peak TPM (blue)
│    100         ╲╱   ╲  ╱       ╲╱╲                  │
│     10            ╲╱             ╲                  │
│         May 23   May 25   May 27   May 29   May 31  │
│ ━ Peak TPM   ━━━ Limit (30.0M)                      │
└─────────────────────────────────────────────────────┘
```

### 4.5 The two non-obvious chart concerns

**Issue 1: red limit line.** Cloudscape's `threshold` series in
`<LineChart>` silently ignores the `color` prop (works on
`<MixedLineBarChart>`). To guarantee an AWS-red horizontal line, use
a regular `line` series with two flat data points:

```jsx
const xMin = safeSeries[0].x;
const xMax = safeSeries[safeSeries.length - 1].x;
out.push({
  title: isDerived ? `Effective ceiling (${fmtVal(limit)})` : `Limit (${fmtVal(limit)})`,
  type: 'line',
  color: '#d13212',   // AWS red
  data: [{ x: xMin, y: limit }, { x: xMax, y: limit }],
  valueFormatter: fmtVal,
});
```

**Issue 2: log y-axis when peak << limit.** A typical Bedrock
workload is far below quota — 7.8K TPM peak vs 30M limit is 3800x
ratio. A linear y-axis stretched to 30M crushes the data line to one
pixel; a y-axis anchored to 7.8K hides the limit line. Switch to log
scale automatically when the ratio exceeds 100x:

```jsx
const useLogScale =
  effectiveLimit !== null && peakValue > 0 && effectiveLimit / peakValue > 100;
```

Two follow-on subtleties:

- Log scale needs a strictly positive floor; substitute zero
  datapoints with a small value so the line keeps drawing through
  idle hours instead of breaking:
  ```jsx
  const yFloor = useLogScale ? Math.max(peakValue * 0.001, 0.1) : 0;
  const safeSeries = useLogScale
    ? series.map(p => ({ x: p.x, y: p.y > 0 ? p.y : yFloor }))
    : series;
  ```
- Show a one-line note above the chart explaining the scale switch.
  Without it, users wonder why values don't grow linearly.

### 4.6 KPI strip

```jsx
function KpiStrip({ limit, isDerived, peak, peakAt, avg, util, fmtVal }) {
  return (
    <Box color="text-body-secondary" fontSize="body-s">
      <SpaceBetween direction="horizontal" size="m">
        <span>
          <b>{isDerived ? 'Effective ceiling:' : 'Limit:'}</b>{' '}
          {limit !== null && limit !== undefined
            ? <>
                {fmtVal(limit)}
                {isDerived && <span style={{ color: '#aaa' }}> (derived from TPM ÷ avg tokens/req)</span>}
              </>
            : <span style={{ color: '#aaa' }}>not published by AWS</span>}
        </span>
        <span>·</span>
        <span><b>Peak:</b> {fmtVal(peak)} <span style={{ color: '#aaa' }}>@ {fmtAt(peakAt)}</span></span>
        <span>·</span>
        <span><b>Avg:</b> {fmtVal(avg)}</span>
        <span>·</span>
        <span><b>Util:</b>{' '}
          <StatusIndicator type={utilSeverity(util)}>
            {util === null || util === undefined ? '—' : fmtPct(util, 1)}
          </StatusIndicator>
        </span>
      </SpaceBetween>
    </Box>
  );
}
```

Util severity is colored by Cloudscape `<StatusIndicator>`:

```jsx
function utilSeverity(pct) {
  if (pct === null || pct === undefined) return 'info';
  if (pct >= 100) return 'error';     // red
  if (pct >= 70)  return 'warning';   // amber
  return 'success';                    // green
}
```

### 4.7 Per-card Info popover

Each MetricCard gets its own `sectionId` + `onInfo` so users can
click "Info" on the card header and the side panel opens with the
metric-specific narrative. SectionInfo entries live in
`frontend/src/components/SectionInfo.jsx`:

- `quota-drilldown` — overall tab context
- `quota-drilldown-tpm` — what TPM is, why it matters, what to do
- `quota-drilldown-rpm` — same for RPM, plus when "derived ceiling"
  is the right answer

---

## 5. Where it lives in the UI

The drill-down is a **section inside the Quotas tab**, not its own
side-nav entry. Sequence on the Quotas tab:

1. KPI ribbon (4 fleet-wide tiles)
2. Quota drill-down chart
3. Per-account / per-model utilization table
4. Throttle hotspots
5. Claude 4+ burndown risk

Putting the drill-down second (between KPI summary and the table)
means an oncall sees the fleet-wide health glance, then drills, then
falls back to the table for breadth — the natural diagnostic flow.

The section is wired into `QuotasTab.jsx` like this:

```jsx
import QuotaDrillDown from './QuotaDrillDownTab.jsx';

// ...inside the component...

return (
  <SpaceBetween size="l">
    <Grid gridDefinition={[{colspan:3},{colspan:3},{colspan:3},{colspan:3}]}>
      {/* KPI tiles */}
    </Grid>

    <QuotaDrillDown onInfo={onInfo} />

    <Container header={...}>
      {/* utilization table */}
    </Container>

    {/* throttle hotspots, burndown */}
  </SpaceBetween>
);
```

No nav entry, no viewBody route. The component is self-contained —
owns its own selector and data fetch — so it drops in anywhere a
host wants it.

---

## 6. Known limitations

| Limitation | Why | Mitigation / future work |
|---|---|---|
| Hourly granularity, not minute | CW `Period=3600` keeps 14d retention; `Period=60` only keeps 14d at 1-min and 60x's payload | Switch the ingester to `Period=60` if anyone hits an actual minute-burst question. Honest labelling: "per-minute rate derived from hourly bucket" |
| No URL deep-links | Picker state lives in component-local React state | Add `react-router` later. Pattern: `/accounts/{id}/models/{modelId}/{region}` |
| Tuple list capped at 500 | `LIMIT 500` in the options query | Bump it for very large fleets, or paginate the dropdown |
| RPM gap for some models | AWS doesn't publish per-model RPM quotas for all SKUs | Derived ceiling = TPM ÷ avg tokens/req. Honest fallback |
| Quota match is fuzzy | AWS uses display names in quotas, technical IDs in metrics | Test cases live in §3.3; add new ones if a future model name doesn't match |

---

## 7. Reproducing this on a different stack

The same shape works for any stack with:

1. A volumetric counter table keyed on (entity, time-bucket) — it
   doesn't have to be Bedrock; could be SQS messages, Lambda
   invocations, or DynamoDB read/write capacity.
2. A quota / limit table keyed on (entity, metric) where metric is
   one of two scalars (analogous to TPM and RPM here).
3. A name disambiguation rule (canonicalisation in §3.3) when those
   two tables key on different naming conventions.

Plug in:

- The volumetric query (your `f_hourly_peak` equivalent) producing
  per-minute rates.
- The quota lookup (your `f_quotas` equivalent) with traffic-type
  bias and a derived-ceiling fallback for missing metrics.
- The two-card grid pattern (`fitHeight + alignItems: stretch`) for
  reliable visual alignment.
- The log-scale guard (`limit / peak > 100`) for the common case
  of usage <<< limit.
- A red flat-line series rendered as `type: 'line'`, NOT
  `type: 'threshold'` (the threshold ignores `color` in Cloudscape's
  `<LineChart>`).

That's it. One backend file, one frontend file, two SectionInfo
entries, one wire-in spot in the host tab. Total: ~700 lines of
code, no schema changes, no ingester changes.
