// Workloads tab (Task A) — per-workload usage attributed from proxy events.
//
// A GenAI proxy fronting Bedrock signs everything with one IAM role, so
// caller identity can't tell workloads apart. The proxy instead emits one
// metadata-only event per request (workload, model, tokens, status, latency)
// to S3, which we ingest cross-account into f_proxy_usage_hourly. This tab
// renders that: usage / throttle / latency BY WORKLOAD, endpoint-agnostic
// (runtime + mantle), with per-workload model drill-down.
import { useMemo, useState, useEffect } from 'react';
import {
  Container, SpaceBetween, Grid, BarChart, Box, SegmentedControl,
  Alert, Header, ExpandableSection, Select, Multiselect, StatusIndicator,
} from '@cloudscape-design/components';
import { useApi, fmt, fmtPct } from '../api.js';
import { ChartLoading, KpiCard, SectionHeader, CHART_I18N } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';

// Onboarding panel shown at the top of the Workloads tab when it's enabled but
// no proxy telemetry has arrived yet — so the tab is never a confusing blank.
// A trimmed version of the README "Workloads: per-workload attribution" section.
function WorkloadsSetupPanel() {
  return (
    <Alert type="info" header="Set up per-workload attribution">
      <SpaceBetween size="s">
        <Box variant="p">
          This tab answers <em>“which of my use-cases is driving Bedrock usage,
          throttling, and latency”</em> — attribution AWS-native metrics can’t
          provide, because CloudWatch is keyed by <strong>model</strong>, not by
          your application’s use-case. It works only if you front Bedrock with a
          shared GenAI proxy / gateway (LiteLLM, a Bedrock gateway, an internal
          SDK wrapper, etc.) that tags each call with a <strong>dimensions</strong>
          map. Tag whatever you slice by — <code>workload</code>, <code>env</code>,
          <code>business_unit</code>, <code>cost_center</code>, … — and pivot the
          whole tab by any key using the picker above. Apps calling Bedrock
          directly with no common layer won’t appear here — the rest of the
          dashboard is unaffected.
        </Box>
        <Box variant="p" color="text-body-secondary">
          <strong>Privacy:</strong> your proxy drops one <em>metadata-only</em>
          event per request into an S3 bucket that this dashboard reads
          read-only. It never sits in your request path, and no prompt or
          response text ever leaves your proxy.
        </Box>
        <ExpandableSection headerText="How it works (event shape)">
          <SpaceBetween size="xs">
            <Box variant="p">
              After each Bedrock call, your proxy appends one NDJSON line to S3
              (<code>.jsonl</code> or <code>.jsonl.gz</code>) with metadata only:
            </Box>
            <Box variant="code">
              {'{"ts":"2026-07-04T12:00:00Z","workload":"search-service",'}
              {'"model":"anthropic.claude-sonnet-4-6","endpoint":"runtime",'}
              {'"input_tokens":1200,"output_tokens":340,"status":200,'}
              {'"latency_ms":880}'}
            </Box>
            <Box variant="p" color="text-body-secondary">
              Point the ingester at that bucket (<code>PROXY_EVENTS_BUCKET</code>
              in <code>config.yaml</code>, then re-run <code>./deploy.sh</code>).
              <strong> Using LiteLLM?</strong> A ready-made callback ships under
              <code> tools/client-telemetry/</code> — no custom code needed.
              Full field reference and a ready-to-copy proxy example are in the
              project README under “Workloads: per-workload attribution” (also
              shipped in the deployment package under <code>tools/reference-proxy/</code>).
            </Box>
          </SpaceBetween>
        </ExpandableSection>
      </SpaceBetween>
    </Alert>
  );
}

function fmtMs(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  return Number.isNaN(n) ? '—' : `${Math.round(n)} ms`;
}

export default function WorkloadsTab({ filters, onInfo }) {
  // Endpoint slice — proxy telemetry is endpoint-agnostic. We ALWAYS fetch the
  // full ('all') set so the KPIs/charts never go empty, and derive from it
  // whether a runtime/mantle split is even meaningful (a proxy usually only
  // tags one endpoint). The switcher is a client-side filter over that full
  // set — never a query that can return nothing — and the mantle option only
  // appears when mantle events actually exist, matching the app-wide
  // "hide the sub-tab where the signal doesn't exist" rule.
  const [ep, setEp] = useState('all');

  // Attribution source config — abstracts over invocation-log tags vs proxy.
  // `capabilities` tells us which panels the active source can populate
  // (invocation logs = volume only; proxy = throttle/latency/quota too).
  const cfg = useApi('/attribution/config', {}, []);
  const caps = cfg.data?.capabilities || {};
  const effectiveSource = cfg.data?.effective_source || 'off';

  // Which custom attribute to slice by (workload / env / business_unit / …).
  // Sourced from whichever attribution source the admin enabled — this picker
  // pivots the whole tab by any key that source carries.
  const dimsMeta = useApi('/attribution/dimensions', {}, []);
  const dimKeys = useMemo(
    () => (dimsMeta.data?.dimensions || []).map(d => d.key), [dimsMeta.data]);
  // Resolve the default key from real data: backend's default_key if it's a key
  // that actually exists, else the first available key. NEVER hardcode
  // 'workload' — the account may tag by business_unit/env/etc. and a bogus
  // 'workload' key returns 0 rows, blanking the whole tab (the empty-state bug).
  const defaultKey = useMemo(() => {
    const dk = dimsMeta.data?.default_key;
    if (dk && dimKeys.includes(dk)) return dk;
    return dimKeys[0] || null;
  }, [dimsMeta.data, dimKeys]);
  const [dimKey, setDimKey] = useState(null);
  // Adopt the resolved default key once dims load (unless user already picked).
  useEffect(() => {
    if (dimKey === null && defaultKey) setDimKey(defaultKey);
  }, [defaultKey, dimKey]);
  // activeKey is null until dims resolve — the usage fetch is gated on it below
  // so we never fire a query with a non-existent key.
  const activeKey = dimKey || defaultKey;

  // Selected values to filter by (empty = all). Reset when the key changes.
  const [selectedValues, setSelectedValues] = useState([]);
  useEffect(() => { setSelectedValues([]); }, [activeKey]);

  // Distinct values for the active key (for the value multiselect). Skip until
  // activeKey resolves — /attribution/values requires dim_key (422 without it).
  const valuesMeta = useApi(activeKey ? '/attribution/values' : null,
    { dim_key: activeKey }, [activeKey]);
  const valueOptions = (valuesMeta.data || []).map(v => ({
    value: v.value, label: v.value,
    description: `${fmt(v.total_requests_30d)} req`,
  }));

  // The endpoint switcher RE-QUERIES with the endpoint pinned, so every
  // number on the page re-slices (a client-side row filter only removed
  // users who never touched the endpoint — with multi-endpoint users the
  // totals never changed, which read as "the switcher does nothing").
  // We ALSO always fetch the 'all' set: it drives which endpoint options
  // exist and keeps the value picker/empty-state stable.
  const paramsAll = useMemo(
    () => ({ days: filters.days, endpoint: 'all', dim_key: activeKey,
             dim_value: selectedValues.length ? selectedValues : undefined }),
    [filters.days, activeKey, selectedValues]);
  // Skip until activeKey resolves so we never query with a null/absent dim_key.
  const usageAll = useApi(activeKey ? '/attribution/usage' : null, paramsAll,
    [JSON.stringify(paramsAll)]);
  const allRows = usageAll.data || [];

  const paramsEp = useMemo(
    () => ({ ...paramsAll, endpoint: ep }), [paramsAll, ep]);
  const usageEp = useApi(activeKey && ep !== 'all' ? '/attribution/usage' : null,
    paramsEp, [JSON.stringify(paramsEp)]);

  // Per-value quota utilization (tokens→TPM ÷ applied limit). Proxy-derived
  // estimate — the third metric of the common enterprise ask alongside tokens + throttles.
  // (Quota query groups all values for the key; client-side filter applies below.)
  const quotaParams = useMemo(
    () => ({ days: filters.days, endpoint: 'all', dim_key: activeKey }),
    [filters.days, activeKey]);
  const quota = useApi('/attribution/quota', quotaParams, [JSON.stringify(quotaParams)]);
  const quotaRowsAll = quota.data?.rows || [];
  const quotaRows = useMemo(() => (
    selectedValues.length
      ? quotaRowsAll.filter(r => selectedValues.includes(r.workload))
      : quotaRowsAll
  ), [quotaRowsAll, selectedValues]);

  // Which endpoints does the proxy data actually contain? (from the 'all'
  // set, so switching a slice never changes which options are offered)
  const endpointsPresent = useMemo(() => {
    const s = new Set();
    for (const r of allRows) for (const e of (r.endpoints || [])) s.add(e);
    return s;
  }, [allRows]);
  // The switcher renders when there's more than one endpoint to switch among.
  const canSplit = endpointsPresent.size > 1;

  // If the selected slice isn't available, fall back to 'all' so the view
  // never renders an empty state just because of a stale toggle selection.
  const effectiveEp = (ep !== 'all' && !endpointsPresent.has(ep)) ? 'all' : ep;

  // Rows come from the endpoint-pinned query: KPIs, charts, and tables all
  // genuinely re-slice when the switcher changes.
  // While an endpoint slice is still loading, keep showing the previous rows
  // (soft update) — a full-page spinner on every switcher click would make
  // the control feel broken.
  const usage = usageAll;
  const rows = (effectiveEp === 'all' ? usageAll.data : usageEp.data)
    || usageAll.data || [];

  // Identity usage (G) — per-IAM-principal attribution from invocation logs.
  // Complements tag-based workloads; empty when logging is off.
  const identity = useApi('/identity-usage', paramsAll, [JSON.stringify(paramsAll)]);
  const identityRows = identity.data || [];

  // Mantle per-project usage (B) — chargeback by the Mantle Project dimension.
  // Provider × path rollup (client telemetry only; [] from other sources).
  const byProvider = useApi('/attribution/by-provider', { days: filters.days },
    [filters.days]);
  const providerRows = byProvider.data || [];

  const mantleProjects = useApi('/mantle-projects', { days: filters.days, endpoint: 'mantle' },
    [filters.days]);
  const projectRows = mantleProjects.data || [];

  // Human label for the active dimension key, e.g. "business_unit" → "Business
  // unit". Falls back to a neutral "Attribute" (not "Workload") while dims are
  // still resolving, so a not-yet-loaded label never implies a phantom key.
  const dimLabel = useMemo(() => {
    const k = activeKey || 'attribute';
    return k.replace(/[_-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }, [activeKey]);

  const kpis = useMemo(() => {
    let reqs = 0, thr = 0, tok = 0, err = 0, wl = rows.length;
    for (const r of rows) {
      reqs += Number(r.total_requests || 0);
      thr += Number(r.throttled || 0);
      tok += Number(r.input_tokens || 0) + Number(r.output_tokens || 0);
      err += Number(r.errors || 0);
    }
    return {
      workloads: wl,
      requests: reqs,
      tokens: tok,
      errors: err,
      throttlePct: reqs ? (thr * 100 / reqs) : 0,
      topWorkload: rows[0]?.workload || '—',
    };
  }, [rows]);

  // TRUE distinct count for the KPI. `rows` is capped at the API's top-500,
  // so rows.length under-counts a big fleet (1,000s of users at enterprise
  // scale); dim_proxy_dimensions carries every value seen in 30 days.
  const totalValues = valueOptions.length || kpis.workloads;

  // Peak quota utilization across values (worst offender) for the KPI ribbon.
  const peakUtil = useMemo(() => {
    let m = null;
    for (const r of quotaRows) {
      if (r.utilization_pct != null && (m === null || r.utilization_pct > m.utilization_pct)) m = r;
    }
    return m;
  }, [quotaRows]);

  const tokenSeries = useMemo(() => {
    const top = rows.slice(0, 12);
    // valueFormatter drives the hover popover — raw counts (64000000) read
    // as noise; fmt renders "64.0M" (matches the Overview charts).
    return [
      { title: 'Input tokens',  type: 'bar', valueFormatter: fmt, data: top.map(r => ({ x: r.workload, y: Number(r.input_tokens || 0) })) },
      { title: 'Output tokens', type: 'bar', valueFormatter: fmt, data: top.map(r => ({ x: r.workload, y: Number(r.output_tokens || 0) })) },
    ];
  }, [rows]);

  const throttleSeries = useMemo(() => {
    // Only values that actually throttled — a zero-throttle value in a
    // "worst offenders" chart is noise (renders as a labeled empty slot).
    const top = rows
      .filter(r => Number(r.throttle_pct || 0) > 0)
      .sort((a, b) => Number(b.throttle_pct || 0) - Number(a.throttle_pct || 0))
      .slice(0, 12);
    return [{ title: 'Throttle %', type: 'bar', color: '#ef4444',
      valueFormatter: v => `${Number(v).toFixed(2)}%`,
      data: top.map(r => ({ x: r.workload, y: Number(r.throttle_pct || 0) })) }];
  }, [rows]);
  const throttledCount = throttleSeries[0].data.length;

  // No attribution data AT ALL (fresh account, no proxy/tags): dims resolve
  // to an empty list, activeKey stays null, and the usage fetch never fires —
  // usage.loading would be true FOREVER (eternal spinner instead of the
  // setup panel; found on a real-data stack). Fall through to the setup
  // panel once dims have resolved empty.
  const dimsResolvedEmpty = !dimsMeta.loading && dimKeys.length === 0;
  if (usage.loading && !dimsResolvedEmpty) {
    return <ChartLoading height={320} label="Loading per-workload usage…" />;
  }

  // Enabled-but-empty: the tab was surfaced (admin toggle or expecting data)
  // but no proxy telemetry exists. Show the onboarding panel instead of a
  // grid of zeros so a customer knows exactly what to do to populate it.
  const noData = allRows.length === 0;

  const dimOptions = dimKeys.map(k => ({
    value: k, label: k.replace(/[_-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) }));
  const lc = dimLabel.toLowerCase();

  return (
    <SpaceBetween size="l">
      {noData && <WorkloadsSetupPanel />}

      {/* Controls row: pick WHICH custom attribute to break usage down by
          (workload / env / business_unit / … — whatever the proxy emitted),
          filter to specific values, and slice by endpoint. All driven by the
          attributes actually ingested — nothing hardcoded. */}
      {!noData && (
        <Box>
          <SpaceBetween direction="horizontal" size="m">
            {dimOptions.length > 0 && (
              <Select
                selectedOption={dimOptions.find(o => o.value === activeKey) || dimOptions[0] || null}
                onChange={({ detail }) => setDimKey(detail.selectedOption.value)}
                options={dimOptions}
                ariaLabel="Break down by attribute"
              />
            )}
            <Multiselect
              selectedOptions={selectedValues.map(v => ({ value: v, label: v }))}
              onChange={({ detail }) => setSelectedValues(detail.selectedOptions.map(o => o.value))}
              options={valueOptions}
              placeholder={`All ${dimLabel} values`}
              tokenLimit={3}
              filteringType="auto"
              ariaLabel={`Filter ${dimLabel} values`}
              empty="No values ingested yet"
              disabled={valueOptions.length === 0}
            />
            {canSplit && (
              <SegmentedControl
                selectedId={effectiveEp}
                onChange={({ detail }) => setEp(detail.selectedId)}
                options={[
                  { id: 'all',     text: 'All endpoints' },
                  { id: 'runtime', text: 'bedrock-runtime' },
                  { id: 'mantle',  text: 'bedrock-mantle' },
                  // Direct-API paths exist only in client telemetry — the
                  // options self-gate on data presence (same pattern as the
                  // Mantle sub-tab elsewhere): no data → no button.
                  ...(endpointsPresent.has('anthropic-api')
                    ? [{ id: 'anthropic-api', text: 'anthropic-api' }] : []),
                  ...(endpointsPresent.has('openai-api')
                    ? [{ id: 'openai-api', text: 'openai-api' }] : []),
                ]}
              />
            )}
          </SpaceBetween>
        </Box>
      )}

      {/* Source-provenance note: make it explicit which attribution source
          backs this view, so the (fewer) invocation-log panels aren't read as
          "missing data". */}
      {!noData && effectiveSource === 'invocation_logs' && (
        <Box variant="small" color="text-body-secondary">
          Attributed from Bedrock invocation-log tags (bedrock-runtime). This
          source reports volume and tokens by attribute; throttle rate, latency,
          and quota utilization need the proxy attribution source.
        </Box>
      )}

      <Grid gridDefinition={[{ colspan: 3 }, { colspan: 3 }, { colspan: 3 }, { colspan: 3 }]}>
        <KpiCard title={`${dimLabel}s`} value={fmt(totalValues)} />
        <KpiCard title="Total requests" value={fmt(kpis.requests)} />
        {caps.throttle
          ? <KpiCard title="Fleet throttle rate" value={fmtPct(kpis.throttlePct)} />
          : <KpiCard title="Total tokens" value={fmt(kpis.tokens)} />}
        {caps.quota
          ? <KpiCard title="Peak quota utilization"
                     value={peakUtil?.utilization_pct != null ? `${peakUtil.utilization_pct.toFixed(2)}%` : '—'}
                     invert />
          : <KpiCard title="Failed requests" value={fmt(kpis.errors)} invert />}
      </Grid>

      <Container header={<SectionHeader
          title={`Tokens by ${lc}${totalValues > 12 ? ` — top 12 of ${fmt(totalValues)}` : ''}`}
          sectionId="wl-tokens" onInfo={onInfo} />}>
        {rows.length === 0
          ? <Box textAlign="center" color="text-body-secondary" padding="l">
              No per-{lc} data yet. Point your proxy at an S3 bucket and tag each request with its {lc} — see the deployment guide.
            </Box>
          : <BarChart series={tokenSeries} xScaleType="categorical" height={280}
              hideFilter stackedBars
              i18nStrings={{ ...CHART_I18N, yTickFormatter: fmt }}
              ariaLabel={`Tokens by ${lc}`} />}
      </Container>

      {/* Quota utilization (the third attribution metric). Proxy-derived estimate:
          peak-hour quota-tokens ÷ applied TPM limit, per dimension value.
          Only the proxy source carries the signal for this. */}
      {caps.quota && (
      <Container header={
        <SectionHeader
          title={`Quota utilization by ${lc}`}
          description="Peak TPM as a share of the applicable Service Quotas limit. Estimated from proxy-reported tokens (excludes cache-write, which the proxy doesn't report) — treat as a floor, not the exact CloudWatch EstimatedTPMQuotaUsage."
          sectionId="wl-quota"
          onInfo={onInfo}
        />
      }>
        {quota.loading ? <ChartLoading height={200} /> :
          quotaRows.length === 0
            ? <Box textAlign="center" color="text-body-secondary" padding="l">
                No quota-utilization estimate available — needs proxy token data plus a Service Quotas TPM limit for the models in use.
              </Box>
            : <PaginatedTable
                items={quotaRows}
                downloadFileName={`quota-utilization-by-${activeKey}.csv`}
                trackBy="workload"
                columnDefinitions={[
                  { id: 'value', header: dimLabel, cell: r => r.workload, exportValue: r => r.workload },
                  { id: 'util', header: 'Peak quota utilization', cell: (r) => {
                      const p = r.utilization_pct;
                      if (p == null) return '—';
                      const t = p > 80 ? 'error' : p > 50 ? 'warning' : 'success';
                      return <StatusIndicator type={t}>{p.toFixed(2)}%</StatusIndicator>;
                    }, exportValue: r => r.utilization_pct },
                  { id: 'peak_tpm', header: 'Peak TPM (est.)', cell: r => fmt(r.peak_tpm), exportValue: r => r.peak_tpm },
                  { id: 'tpm_limit', header: 'TPM limit', cell: r => r.tpm_limit != null ? fmt(r.tpm_limit) : '—', exportValue: r => r.tpm_limit },
                  { id: 'model', header: 'Busiest model', cell: r => r.model, exportValue: r => r.model },
                  { id: 'region', header: 'Region', cell: r => r.region, exportValue: r => r.region },
                ]}
                empty="No quota-utilization estimate."
              />
        }
      </Container>
      )}

      {caps.throttle && rows.length > 0 && (
        <Container header={<SectionHeader
          title={`Throttle rate by ${lc}${throttledCount >= 12 ? ' — top 12 offenders' : ''}`}
          description={`Only ${lc} values with throttled requests appear; ${lc} values with zero throttles are omitted.`}
          sectionId="wl-throttle" onInfo={onInfo} />}>
          <BarChart series={throttleSeries} xScaleType="categorical" height={260}
            hideFilter
            i18nStrings={{ ...CHART_I18N, yTickFormatter: v => `${Number(v).toFixed(1)}%` }}
            ariaLabel={`Throttle % by ${lc}`} />
        </Container>
      )}

      <Container header={<SectionHeader title={`Per-${lc} detail`} sectionId="wl-table" onInfo={onInfo} />}>
        <PaginatedTable
          items={rows}
          downloadFileName={`${activeKey}-usage.csv`}
          trackBy="workload"
          columnDefinitions={[
            { id: 'workload',       header: dimLabel,      cell: r => r.workload, exportValue: r => r.workload },
            { id: 'total_requests', header: 'Requests',    cell: r => fmt(r.total_requests), exportValue: r => r.total_requests },
            { id: 'input_tokens',   header: 'Input tokens', cell: r => fmt(r.input_tokens), exportValue: r => r.input_tokens },
            { id: 'output_tokens',  header: 'Output tokens', cell: r => fmt(r.output_tokens), exportValue: r => r.output_tokens },
            // Throttle % + latency only exist on the proxy source; drop the
            // columns entirely for the invocation-log source (would be all —).
            ...(caps.throttle ? [{ id: 'throttle_pct', header: 'Throttle %', cell: r => fmtPct(r.throttle_pct), exportValue: r => r.throttle_pct }] : []),
            { id: 'error_pct',      header: 'Error %',     cell: r => fmtPct(r.error_pct), exportValue: r => r.error_pct },
            ...(caps.latency ? [{ id: 'p99_latency_ms', header: 'p99 latency', cell: r => fmtMs(r.p99_latency_ms), exportValue: r => r.p99_latency_ms }] : []),
            { id: 'endpoints',      header: 'Endpoints',   cell: r => (r.endpoints || []).join(', '), exportValue: r => (r.endpoints || []).join('|') },
          ]}
          empty={`No per-${lc} data.`}
        />
      </Container>

      {/* Usage by provider × path — client telemetry only, so the section
          self-gates on data presence (nothing renders for CW-only stacks).
          Provider = model family across ALL paths, including direct
          anthropic-api / openai-api traffic no AWS-side source can see.
          cost_usd_est is the EMITTERS' estimate — labeled, never billing. */}
      {providerRows.length > 0 && (
        <Container header={
          <SectionHeader
            title="Usage by provider (client-reported)"
            description="Rolled up by model family across every path — bedrock-runtime, bedrock-mantle, and direct anthropic-api / openai-api traffic reported by client telemetry. Cost is the emitters' estimate; reconcile against Cost Explorer for billing truth."
            sectionId="wl-by-provider"
            onInfo={onInfo}
          />
        }>
          <PaginatedTable
            items={providerRows}
            downloadFileName="usage-by-provider.csv"
            trackBy={(r) => `${r.provider}:${r.endpoint}`}
            columnDefinitions={[
              { id: 'prov', header: 'Provider', cell: r => r.provider, exportValue: r => r.provider },
              { id: 'ep',   header: 'Path',     cell: r => r.endpoint, exportValue: r => r.endpoint },
              { id: 'req',  header: 'Requests', cell: r => fmt(r.total_requests), exportValue: r => r.total_requests },
              { id: 'in',   header: 'Input tokens',  cell: r => fmt(r.input_tokens), exportValue: r => r.input_tokens },
              { id: 'out',  header: 'Output tokens', cell: r => fmt(r.output_tokens), exportValue: r => r.output_tokens },
              { id: 'cache', header: 'Cache read',   cell: r => fmt(r.cache_read_tokens), exportValue: r => r.cache_read_tokens },
              { id: 'err',  header: 'Errors',   cell: r => fmt(r.errors), exportValue: r => r.errors },
              { id: 'rty',  header: 'Retried',  cell: r => fmt(r.retried), exportValue: r => r.retried },
              { id: 'p90',  header: 'p90 latency', cell: r => r.p90_latency_ms != null ? fmtMs(r.p90_latency_ms) : '—', exportValue: r => r.p90_latency_ms },
              { id: 'ttft', header: 'p90 TTFT',    cell: r => r.p90_ttft_ms != null ? fmtMs(r.p90_ttft_ms) : '—', exportValue: r => r.p90_ttft_ms },
              { id: 'cost', header: 'Est. cost',   cell: r => r.cost_usd_est ? `$${Number(r.cost_usd_est).toFixed(2)}` : '—', exportValue: r => r.cost_usd_est },
              { id: 'mdl',  header: 'Models',   cell: r => fmt(r.distinct_models), exportValue: r => r.distinct_models },
            ]}
            empty="No client-reported provider data."
          />
        </Container>
      )}

      {/* Top callers by IAM principal (G) — invocation-log-derived, so empty
           when model invocation logging is off. Complements tag-based
           workloads with principal-level attribution. */}
      <Container header={<SectionHeader title="Top callers (by IAM principal)" sectionId="identity-usage" onInfo={onInfo} />}>
        {identity.loading ? <ChartLoading /> :
          identityRows.length === 0
            ? <Box textAlign="center" color="text-body-secondary" padding="l">
                No per-principal data. This view is derived from Bedrock Model Invocation Logs — enable model invocation logging (Bedrock &gt; Settings &gt; Model invocation logging) to populate it.
              </Box>
            : <PaginatedTable
                items={identityRows}
                downloadFileName="identity-usage.csv"
                trackBy="identity_arn"
                columnDefinitions={[
                  { id: 'arn', header: 'Identity ARN',
                    cell: r => <Box variant="span" fontSize="body-s"><span title={r.identity_arn} style={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>{r.identity_arn}</span></Box>,
                    exportValue: r => r.identity_arn },
                  { id: 'req',  header: 'Requests',      cell: r => fmt(r.total_requests), exportValue: r => r.total_requests },
                  { id: 'in',   header: 'Input tokens',  cell: r => fmt(r.input_tokens), exportValue: r => r.input_tokens },
                  { id: 'out',  header: 'Output tokens', cell: r => fmt(r.output_tokens), exportValue: r => r.output_tokens },
                  { id: 'fail', header: 'Failed',        cell: r => fmt(r.failed_requests), exportValue: r => r.failed_requests },
                  { id: 'mdl',  header: 'Models used',   cell: r => fmt(r.models_used), exportValue: r => r.models_used },
                ]}
                empty="No per-principal data."
              />
        }
      </Container>

      {/* Per-project usage / chargeback (B) — Mantle Project dimension. */}
      <Container header={<SectionHeader title="Per-project usage (chargeback)" sectionId="mantle-projects" onInfo={onInfo} />}>
        {mantleProjects.loading ? <ChartLoading /> :
          projectRows.length === 0
            ? <Box textAlign="center" color="text-body-secondary" padding="l">
                The bedrock-mantle Project dimension has no data yet for this window.
              </Box>
            : <PaginatedTable
                items={projectRows}
                downloadFileName="mantle-projects.csv"
                trackBy="project"
                columnDefinitions={[
                  { id: 'p',    header: 'Project',       cell: r => r.project, exportValue: r => r.project },
                  { id: 'req',  header: 'Requests',      cell: r => fmt(r.total_requests), exportValue: r => r.total_requests },
                  { id: 'e4',   header: '4xx errors',    cell: r => fmt(r.client_errors_4xx), exportValue: r => r.client_errors_4xx },
                  { id: 'in',   header: 'Input tokens',  cell: r => fmt(r.input_tokens), exportValue: r => r.input_tokens },
                  { id: 'out',  header: 'Output tokens', cell: r => fmt(r.output_tokens), exportValue: r => r.output_tokens },
                  { id: 'mdl',  header: 'Models',        cell: r => fmt(r.models_used), exportValue: r => r.models_used },
                ]}
                empty="No per-project data."
              />
        }
      </Container>
    </SpaceBetween>
  );
}
