// Engagement Signals (= Ops Insights) tab — 12 containers.
import { useMemo, useState } from 'react';
import {
  Container, Header, SpaceBetween, ColumnLayout, PieChart, LineChart,
  Box, Badge, Spinner, StatusIndicator,
} from '@cloudscape-design/components';
import { useApi, fmt, fmtPct, accountName, useAccountNames } from '../api.js';
import { ChartLoading, SectionHeader, CHART_I18N } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';
import EndpointSubTabs from '../components/EndpointSubTabs.jsx';

function modelShort(id) {
  return (id || '').replace(/^us\./, '').replace(/^eu\./, '').replace(/^global\./, '')
    .replace(/^anthropic\./, '').replace(/^amazon\./, '').replace(/^meta\./, '')
    .split(':')[0];
}

function severityType(p) {
  return p > 5 ? 'error' : p > 1 ? 'warning' : 'success';
}

export default function OpsInsightsTab({ filters, onInfo }) {
  useAccountNames();   // resolve account names for the Account name cells
  // CRIS adoption is a bedrock-runtime concept (Mantle is in-region only,
  // no CRIS aggregation). Keep coverage='full' for runtime; for Mantle
  // we still render the body but several charts will simply be empty
  // (no CRIS rows). Coverage='metric' communicates partial fit.
  const distinct = useApi('/distinct-filters', {}, []).data || {};
  const mantleAvailable = !!distinct.mantle_available?.volumetric;
  // Initialise to the value the sub-tab control actually SHOWS as selected.
  // Defaulting to 'all' while the control highlighted "bedrock-runtime"
  // meant the header read Runtime while the data was Combined: the browser
  // showed 23,204,626 requests where real Runtime is 22,995,921 (finding 11).
  const [endpoint, setEndpoint] = useState(
    filters.endpoint && filters.endpoint !== 'all' ? filters.endpoint : 'runtime');
  const filtersWithEp = useMemo(() => ({ ...filters, endpoint }), [filters, endpoint]);
  return (
    <EndpointSubTabs
      selected={endpoint}
      onChange={setEndpoint}
      runtimeCoverage="full"
      mantleCoverage="metric"
      mantleAvailable={mantleAvailable}
    >
      {({ endpoint: ep }) => <OpsInsightsBody filters={filtersWithEp} onInfo={onInfo} endpoint={ep} />}
    </EndpointSubTabs>
  );
}

function OpsInsightsBody({ filters, onInfo, endpoint }) {
  // Thumb rule: on the mantle slice, show only what Mantle CloudWatch
  // actually exposes (Inferences, 4xx, tokens → volume, throttle-ish, peak
  // TPM, request shape). CRIS adoption, traffic-type, service tier, cache,
  // context-length routing, and inference-profile adoption are runtime-only
  // concepts Mantle doesn't publish — hide those panels rather than render
  // them empty.
  const isMantle = endpoint === 'mantle';
  const cris = useApi('/ops-cris-adoption', filters, [JSON.stringify(filters)]);
  const crisGaps = useApi('/ops-cris-by-account', filters, [JSON.stringify(filters)]);
  const profile = useApi('/ops-inference-profile', filters, [JSON.stringify(filters)]);
  const tier = useApi('/ops-service-tier', filters, [JSON.stringify(filters)]);
  const trend = useApi('/daily-trend', filters, [JSON.stringify(filters)]);
  const matrix = useApi('/region-model-matrix', filters, [JSON.stringify(filters)]);
  const throttle = useApi('/ops-throttle-rate', filters, [JSON.stringify(filters)]);
  const shape = useApi('/ops-request-shape', filters, [JSON.stringify(filters)]);
  const peak = useApi('/ops-peak-rpm', { ...filters, days: Math.min(filters.days, 3) }, [JSON.stringify(filters)]);
  const burndown = useApi('/ops-burndown-risk', filters, [JSON.stringify(filters)]);
  const caching = useApi('/ops-caching', filters, [JSON.stringify(filters)]);
  const ctx = useApi('/ops-context-length', filters, [JSON.stringify(filters)]);

  // CRIS pie data
  // Finding 18: requests whose traffic_type is neither CRIS nor plain on-demand
  // (provisioned throughput, batch, or simply not reported in the invocation
  // log) used to be dropped from this pie, so the slices implied a routing mix
  // for traffic we cannot classify. Show that share instead of hiding it.
  const crisPie = useMemo(() => {
    if (!cris.data) return [];
    let crisR = 0, odR = 0, otherR = 0;
    for (const r of cris.data) {
      crisR += Number(r.cris_requests || 0);
      odR += Number(r.od_requests || 0);
      otherR += Number(r.unclassified_requests || 0);
    }
    return [
      { title: `CRIS (${fmt(crisR)})`, value: crisR },
      { title: `On-Demand (${fmt(odR)})`, value: odR },
      { title: `Other / not reported (${fmt(otherR)})`, value: otherR },
    ].filter(d => d.value > 0);
  }, [cris.data]);

  // Requests we could classify, for the gaps panel's honesty note.
  const crisCoverage = useMemo(() => {
    let known = 0, total = 0;
    for (const r of (cris.data || [])) {
      known += Number(r.cris_requests || 0) + Number(r.od_requests || 0);
      total += Number(r.total_requests || 0);
    }
    return { known, total, pct: total > 0 ? (100 * known) / total : null };
  }, [cris.data]);

  const profilePie = useMemo(() => {
    if (!profile.data) return [];
    return profile.data
      .filter(r => (r.inference_profile_prefix || '') !== '__none__')
      .map(r => ({
        title: `${r.inference_profile_prefix} (${fmt(r.unique_accounts)} accts)`,
        value: Number(r.total_requests),
      }));
  }, [profile.data]);

  // Share of prompt tokens served from cache.
  //
  // Finding 14: inputTokens, cacheReadInputTokens and cacheWriteInputTokens are
  // three disjoint counters, so the denominator is all three. Omitting writes
  // overstated the share precisely on workloads that are populating a cache.
  // This is a token share, not a per-request hit rate - CloudWatch publishes no
  // per-request cache dimension, so a request hit rate is not derivable here.
  const cacheTrendSeries = useMemo(() => {
    if (!trend.data) return [];
    return [{
      title: 'Cached prompt tokens %',
      type: 'line',
      data: trend.data.map(r => {
        const read = Number(r.cache_read_tokens || 0);
        const write = Number(r.cache_write_tokens || 0);
        const fresh = Number(r.input_tokens || 0);
        const denom = read + write + fresh;
        return {
          x: `${r.month}/${r.day}`,
          y: denom ? (read * 100 / denom) : 0,
        };
      }),
    }];
  }, [trend.data]);

  // Pivot region-model-matrix client-side: rows=region, cols=top model + Total
  const matrixView = useMemo(() => {
    if (!matrix.data) return null;
    const regions = new Map();
    const modelTotals = new Map();
    for (const r of matrix.data) {
      const region = r.region;
      const m = r.modelid || r.modelId;
      const v = Number(r.total_requests);
      if (!regions.has(region)) regions.set(region, { region, _total: 0 });
      regions.get(region)[m] = v;
      regions.get(region)._total += v;
      modelTotals.set(m, (modelTotals.get(m) || 0) + v);
    }
    const models = [...modelTotals.entries()].sort((a, b) => b[1] - a[1]).map(e => e[0]);
    return { rows: [...regions.values()].sort((a, b) => b._total - a._total), models };
  }, [matrix.data]);

  return (
    <SpaceBetween size="l">
      {/* Runtime-only panels: CRIS adoption, inference-profile, service tier,
          cache — all runtime concepts Mantle CloudWatch does not publish.
          Hidden on the mantle slice (thumb rule: show only what exists). */}
      {!isMantle && <>
      {/* Row 1: 2 columns of pies. fitHeight + grid stretch keeps the two
           cards the same height even when one renders an empty-state Box
           and the other renders a full pie. */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, alignItems: 'stretch' }}>
        <Container fitHeight header={<SectionHeader title="CRIS vs On-Demand" sectionId="cris-adoption" onInfo={onInfo} />}>
          {cris.loading ? <ChartLoading height={220} /> :
           crisPie.length === 0 ? <Box textAlign="center" color="text-body-secondary" padding="l">No CRIS / OD data in window.</Box> :
            <PieChart data={crisPie} size="medium" hideFilter ariaLabel="CRIS adoption" empty="No data" />
          }
        </Container>
        <Container fitHeight header={<SectionHeader title="Inference profile adoption" sectionId="inference-profile" onInfo={onInfo} />}>
          {profile.loading ? <ChartLoading height={220} /> :
           profilePie.length === 0 ? <Box textAlign="center" color="text-body-secondary" padding="l">No inference-profile usage detected — workloads are using bare on-demand model IDs.</Box> :
            <PieChart data={profilePie} size="medium" hideFilter ariaLabel="Inference profile" empty="No data" />
          }
        </Container>
      </div>

      {/* Row 2: tier table on the left, cache trend chart on the right. */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, alignItems: 'stretch' }}>
        <Container fitHeight header={<SectionHeader title="Service tier distribution" sectionId="service-tier" onInfo={onInfo} />}>
          {tier.loading ? <ChartLoading height={200} /> :
            <PaginatedTable
              variant="embedded"
              sortingDisabled
              items={(tier.data || []).map(r => ({
                ...r,
                // The CW ingester writes '__none__' for unknown tier; surface
                // that as the canonical "default" label.
                _tier_label: (r.service_tier && r.service_tier !== '__none__') ? r.service_tier : 'default',
              }))}
              columnDefinitions={[
                { id: 't', header: 'Tier',     cell: r => <Badge color={r._tier_label === 'priority' ? 'green' : r._tier_label === 'flex' ? 'grey' : 'severity-low'}>{r._tier_label}</Badge> },
                { id: 'r', header: 'Requests', cell: r => fmt(r.total_requests) },
                { id: 'a', header: 'Accounts', cell: r => fmt(r.unique_accounts) },
                { id: 'p', header: 'Throttle %', cell: r => <StatusIndicator type={severityType(Number(r.throttle_pct || 0))}>{fmtPct(r.throttle_pct, 3)}</StatusIndicator> },
              ]}
              empty="No data"
            />
          }
        </Container>
        <Container fitHeight header={<SectionHeader title="Fleet-wide cache hit rate" sectionId="cache-trend" onInfo={onInfo} />}>
          {trend.loading ? <ChartLoading height={220} /> :
            <LineChart
              series={cacheTrendSeries}
              xScaleType="categorical"
              hideFilter
              ariaLabel="Cache trend"
              i18nStrings={{ ...CHART_I18N, yTickFormatter: v => `${v.toFixed(1)}%` }}
              height={220}
            />
          }
        </Container>
      </div>
      </>}

      {!isMantle && <Container header={<SectionHeader title="CRIS adoption gaps" sectionId="cris-gaps" onInfo={onInfo} />}>
        {crisGaps.loading ? <ChartLoading height={200} /> :
          <PaginatedTable
            items={(crisGaps.data || []).filter(r => Number(r.od_requests) > 0 && Number(r.cris_requests || 0) === 0)}
            columnDefinitions={[
              { id: 'a', header: 'Account ID', cell: r => r.accountid || r.accountId, exportValue: r => r.accountid || r.accountId },
              { id: 'an', header: 'Account name', cell: r => accountName(r.accountid || r.accountId) || '—', exportValue: r => accountName(r.accountid || r.accountId) },
              { id: 'm', header: 'Model',   cell: r => r.modelid || r.modelId },
              { id: 'o', header: 'OD requests', cell: r => fmt(r.od_requests) },
            ]}
            empty={crisCoverage.pct === null
              ? 'No request data in this window.'
              : crisCoverage.known === 0
                ? 'Routing not reported for any request in this window, so CRIS adoption cannot be determined.'
                : crisCoverage.pct < 99.5
                  ? `No on-demand-only workloads among the ${fmt(crisCoverage.known)} requests whose routing is reported (${crisCoverage.pct.toFixed(1)}% of traffic; the rest is provisioned throughput, batch, or not reported).`
                  : 'Every request with reported routing uses CRIS.'}
          />
        }
      </Container>}

      {matrixView && matrixView.models.length > 0 && (
        <Container header={<SectionHeader title={`Region × Model matrix (top ${matrixView.models.length} models)`} sectionId="region-matrix" onInfo={onInfo} />}>
          <PaginatedTable
            items={matrixView.rows}
            pageSize={15}
            columnDefinitions={[
              { id: 'r', header: 'Region', cell: r => r.region },
              ...matrixView.models.map((m) => ({
                id: 'm-' + m,
                header: modelShort(m),
                cell: (r) => r[m] ? fmt(r[m]) : '—',
              })),
              { id: 'tot', header: 'Total', cell: r => <Box fontWeight="bold">{fmt(r._total)}</Box> },
            ]}
            empty="No data"
          />
        </Container>
      )}

      <Container header={<SectionHeader title={`Throttle rate by account (${(throttle.data || []).length} entries)`} sectionId="throttle-rate-account" onInfo={onInfo} />}>
        {throttle.loading ? <ChartLoading /> :
          <PaginatedTable
            items={throttle.data || []}
            columnDefinitions={[
              { id: 'a', header: 'Account ID', cell: r => r.accountid || r.accountId, exportValue: r => r.accountid || r.accountId },
              { id: 'an', header: 'Account name', cell: r => accountName(r.accountid || r.accountId) || '—', exportValue: r => accountName(r.accountid || r.accountId) },
              { id: 'm', header: 'Model',   cell: r => r.modelid || r.modelId },
              { id: 'r', header: 'Region',  cell: r => r.region },
              { id: 'rate', header: 'Throttle %', cell: r => <StatusIndicator type={severityType(Number(r.throttle_pct || 0))}>{fmtPct(r.throttle_pct, 3)}</StatusIndicator> },
              { id: 'thr', header: 'Throttled', cell: r => fmt(r.throttled) },
              { id: 't', header: 'Total',  cell: r => fmt(r.total_requests) },
            ]}
            empty="No throttling — clean fleet."
          />
        }
      </Container>

      <Container header={<SectionHeader title="Request shape by model" sectionId="request-shape" onInfo={onInfo} />}>
        {shape.loading ? <ChartLoading /> :
          <PaginatedTable
            items={shape.data || []}
            columnDefinitions={[
              { id: 'a', header: 'Account ID', cell: r => r.accountid || r.accountId, exportValue: r => r.accountid || r.accountId },
              { id: 'an', header: 'Account name', cell: r => accountName(r.accountid || r.accountId) || '—', exportValue: r => accountName(r.accountid || r.accountId) },
              { id: 'm', header: 'Model',   cell: r => r.modelid || r.modelId },
              { id: 'r', header: 'Region',  cell: r => r.region },
              { id: 'i', header: 'Avg input',  cell: r => fmt(r.avg_input) },
              { id: 'o', header: 'Avg output', cell: r => fmt(r.avg_output) },
              {
                // Finding 12: this read `ratio` when the API returned
                // output/input, so 74.7:1 input-heavy traffic rendered "0.0:1"
                // and tripped the "output-heavy" warning - the opposite advice,
                // and the opposite of what Ops Review said about the same rows.
                // The thresholds below now match Ops Review exactly:
                // > 50:1 input-heavy (prompt-caching candidate), < 2:1
                // output-heavy (burndown amplifier on Claude 4+).
                id: 'ratio', header: 'Input:output ratio',
                cell: (r) => {
                  const v = r.input_output_ratio ?? r.ratio;
                  if (v === null || v === undefined) {
                    return <StatusIndicator type="info">n/a</StatusIndicator>;
                  }
                  const n = Number(v);
                  const t = n > 50 ? 'info' : n < 2 ? 'warning' : 'success';
                  return <StatusIndicator type={t}>{n.toFixed(1)}:1</StatusIndicator>;
                },
                exportValue: r => r.input_output_ratio ?? r.ratio,
              },
            ]}
            empty="No data"
          />
        }
      </Container>

      <Container header={
        <SectionHeader
          title="Avg RPM & TPM"
          sectionId="avg-rpm-tpm"
          onInfo={onInfo}
        />
      }>
        {peak.loading ? <ChartLoading /> :
          <PaginatedTable
            items={peak.data || []}
            downloadFileName="bedrock-rpm-tpm.csv"
            columnDefinitions={[
              { id: 'a', header: 'Account ID', cell: r => r.accountid || r.accountId, exportValue: r => r.accountid || r.accountId },
              { id: 'an', header: 'Account name', cell: r => accountName(r.accountid || r.accountId) || '—', exportValue: r => accountName(r.accountid || r.accountId) },
              { id: 'm', header: 'Model',   cell: r => r.modelid || r.modelId },
              { id: 'r', header: 'Region',  cell: r => r.region },
              // f_hourly_peak stores CloudWatch Period=3600 SUMS, so these are
              // the busiest hour's HOURLY-AVERAGE per-minute rates (total/60),
              // a lower bound on the true minute peak. The old "Peak RPM
              // (hr × 60)" column rendered the raw hourly total.
              { id: 'rpm', header: 'Busiest hr — avg RPM', cell: r => <Box fontWeight="bold">{fmt(r.busiest_hour_avg_rpm ?? r.peak_rpm)}</Box>,
                exportValue: r => r.busiest_hour_avg_rpm ?? r.peak_rpm },
              { id: 'tpm-in',  header: 'Busiest hr — avg input TPM',  cell: r => <Box fontWeight="bold">{fmt(r.busiest_hour_avg_input_tpm ?? r.peak_input_tpm)}</Box>,
                exportValue: r => r.busiest_hour_avg_input_tpm ?? r.peak_input_tpm },
              { id: 'tpm-out', header: 'Busiest hr — avg output TPM', cell: r => <Box fontWeight="bold">{fmt(r.busiest_hour_avg_output_tpm ?? r.peak_output_tpm)}</Box>,
                exportValue: r => r.busiest_hour_avg_output_tpm ?? r.peak_output_tpm },
              { id: 'rq', header: 'Requests in that hour', cell: r => fmt(r.requests_busiest_hour_total ?? r.peak_requests_hour),
                exportValue: r => r.requests_busiest_hour_total ?? r.peak_requests_hour },
            ]}
            empty="No peak data"
          />
        }
      </Container>

      {(burndown.data || []).length > 0 && (
        <Container header={<SectionHeader title="Claude 4+ burndown risk" sectionId="burndown" onInfo={onInfo} />}>
          <PaginatedTable
            items={burndown.data || []}
            columnDefinitions={[
              { id: 'a', header: 'Account ID', cell: r => r.accountid || r.accountId, exportValue: r => r.accountid || r.accountId },
              { id: 'an', header: 'Account name', cell: r => accountName(r.accountid || r.accountId) || '—', exportValue: r => accountName(r.accountid || r.accountId) },
              { id: 'm', header: 'Model',   cell: r => r.modelid || r.modelId },
              { id: 'r', header: 'Region',  cell: r => r.region },
              { id: 'p', header: 'Busiest hr — avg quota TPM',
                cell: r => fmt(r.busiest_hour_avg_tpm ?? r.peak_tpm),
                exportValue: r => r.busiest_hour_avg_tpm ?? r.peak_tpm },
              { id: 'b', header: 'Burndown', cell: r => r.burndown_rate != null ? `${r.burndown_rate}×` : '—' },
              // The applicable quota depends on traffic family (On-demand /
              // Cross-region / Global CRIS) and f_hourly_peak has no family
              // dimension, so an ambiguous match is labelled instead of
              // silently resolving to the most generous limit.
              { id: 'q', header: 'Quota limit (TPM)',
                cell: r => r.effective_tpm == null
                  ? <StatusIndicator type="info">unknown</StatusIndicator>
                  : <span>{fmt(r.effective_tpm)}{r.quota_ambiguous ? ' *' : ''}</span>,
                exportValue: r => r.effective_tpm ?? '' },
              { id: 'qf', header: 'Quota family',
                cell: r => r.quota_family
                  ? <span>{r.quota_family}{r.quota_ambiguous ? ' (ambiguous)' : ''}</span>
                  : '—',
                exportValue: r => r.quota_family || '' },
              { id: 'o', header: 'Quota util %',
                cell: r => r.utilization_pct == null
                  ? <StatusIndicator type="info">unknown</StatusIndicator>
                  : <Box color={r.utilization_pct >= 80 ? 'text-status-error' : 'text-status-info'} fontWeight="bold">{fmtPct(r.utilization_pct)}</Box>,
                exportValue: r => r.utilization_pct ?? '' },
            ]}
            empty="No burndown risk"
          />
          <Box variant="small" color="text-body-secondary" padding={{ top: 'xs' }}>
            Rates are the busiest hour's <b>hourly average</b> per minute
            (hourly total ÷ 60) — CloudWatch is collected at hourly resolution
            here, so a true minute peak is not derivable and these are lower
            bounds. <b>*</b> marks a quota whose traffic family could not be
            determined (On-demand / Cross-region / Global CRIS publish different
            limits and the hourly table carries no family dimension); the
            On-demand limit is shown where available.
          </Box>
        </Container>
      )}

      {!isMantle && <>
      <Container header={<SectionHeader title="Prompt caching adoption" sectionId="caching" onInfo={onInfo} />}>
        {caching.loading ? <ChartLoading /> :
          <PaginatedTable
            items={caching.data || []}
            columnDefinitions={[
              { id: 'm', header: 'Model', cell: r => r.modelid || r.modelId },
              { id: 'i', header: 'Input tokens', cell: r => fmt(r.total_input_tokens) },
              { id: 'cr', header: 'Cache read',  cell: r => fmt(r.cache_read_tokens) },
              { id: 'cw', header: 'Cache write', cell: r => fmt(r.cache_write_tokens) },
              {
                id: 'h', header: 'Hit rate',
                cell: (r) => {
                  const v = Number(r.hit_rate_pct || 0);
                  const t = v > 30 ? 'success' : v > 5 ? 'warning' : 'error';
                  return <StatusIndicator type={t}>{v.toFixed(2)}%</StatusIndicator>;
                },
              },
            ]}
            empty="No caching data"
          />
        }
      </Container>

      <Container header={<SectionHeader title="Context length routing" sectionId="context-routing" onInfo={onInfo} />}>
        {ctx.loading ? <ChartLoading /> :
          <PaginatedTable
            items={ctx.data || []}
            columnDefinitions={[
              { id: 'rv', header: 'Routed variant', cell: r => r.routed_model_id },
              { id: 'm',  header: 'Model',          cell: r => r.modelid || r.modelId },
              { id: 'r',  header: 'Requests',       cell: r => fmt(r.total_requests) },
              { id: 'i',  header: 'Input tokens',   cell: r => fmt(r.input_tokens) },
            ]}
            empty="No data"
          />
        }
      </Container>
      </>}
    </SpaceBetween>
  );
}
