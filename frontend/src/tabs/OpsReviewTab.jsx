// Ops Review tab — the deep operational review.
//
// Renders structured findings + Claude-Opus-synthesized markdown narrative.
// Mirrors the internal reference's component structure: header bar, KPI
// ribbon, executive summary (LLM markdown with mermaid), model lifecycle
// alerts (horizontal timeline + table), capacity health, growth signal,
// burndown risk, request shape outliers, detailed breakdown lazy section.
//
// Markdown→HTML pipeline + render-off-screen mermaid hack live in
// ../components/Mermaid.js.

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Container, Header, SpaceBetween, Box, Button, Alert, Spinner,
  ColumnLayout, ExpandableSection, StatusIndicator, Link, Grid,
} from '@cloudscape-design/components';
import { useApi, fmt, fmtPct, api as apiCall, buildUrl } from '../api.js';
import { ChartLoading, SectionHeader } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';
import LifecycleTimeline from '../components/LifecycleTimeline.jsx';
import { renderMarkdownToHtml, renderMermaidIn } from '../components/Mermaid.js';
import DOMPurify from 'dompurify';

// Paint every `.action-orange-btn` button orange via inline style. Inline
// styles bypass the cascade entirely, so we don't have to fight Cloudscape's
// hashed CSS variables. Re-runs on every render of any consumer.
function useOrangeActionButtons(deps = []) {
  useEffect(() => {
    const apply = () => {
      const buttons = document.querySelectorAll('button.action-orange-btn');
      buttons.forEach((b) => {
        b.style.setProperty('background-color', '#ec7211', 'important');
        b.style.setProperty('background',       '#ec7211', 'important');
        b.style.setProperty('border-color',     '#ec7211', 'important');
        b.style.setProperty('color',            '#ffffff', 'important');
      });
    };
    apply();
    // Cloudscape sometimes re-styles on hover/focus; observer keeps us in sync.
    const obs = new MutationObserver(apply);
    obs.observe(document.body, { subtree: true, attributes: true, attributeFilter: ['class', 'style'] });
    return () => obs.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

function severityType(s) {
  return s === 'critical' ? 'error'
    : s === 'warning' ? 'warning'
    : s === 'success' ? 'success'
    : 'info';
}

function trafficLabel(tt) {
  if (tt === 'CROSS_REGION_OD_INFERENCE_REQUEST') return 'CRIS (destination)';
  if (tt === 'SOURCE_REGION_OD_INFERENCE_REQUEST') return 'CRIS (source)';
  if (tt === 'ON_DEMAND_INFERENCE_REQUEST') return 'On-Demand';
  if (tt === 'PROVISIONED_THROUGHPUT_V1') return 'Provisioned';
  return tt || '—';
}

// Briefly add the .ops-flash class to flash-glow a section after a click.
function flashSection(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('ops-flash');
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  // Force reflow before re-adding the class so the animation restarts.
  // eslint-disable-next-line no-unused-expressions
  el.offsetHeight;
  el.classList.add('ops-flash');
}

function KpiRibbon({ counts }) {
  // Severity → Cloudscape text-status color token.
  const colorFor = (total, crit, warn) => {
    if (crit)  return 'text-status-error';
    if (warn)  return 'text-status-warning';
    if (total) return 'text-status-info';
    return 'text-status-success';   // 0 → green ("clean")
  };

  const cards = [
    { label: 'Lifecycle alerts',
      total: counts.lifecycle,
      color: colorFor(counts.lifecycle, counts.lifecycleCrit, counts.lifecycleWarn),
      target: 'ops-section-lifecycle' },
    { label: 'Throttled hotspots',
      total: counts.capacity,
      color: colorFor(counts.capacity, counts.capacityCrit, counts.capacityWarn),
      target: 'ops-section-capacity' },
    { label: 'Growth signals',
      total: counts.growth,
      color: counts.growth ? 'text-status-info' : 'text-status-success',
      target: 'ops-section-growth' },
    { label: 'Burndown risks',
      total: counts.burndown,
      color: counts.burndown ? 'text-status-warning' : 'text-status-success',
      target: 'ops-section-burndown' },
    { label: 'Request shape outliers',
      total: counts.shape,
      color: counts.shape ? 'text-status-info' : 'text-status-success',
      target: 'ops-section-shape' },
  ];

  // Compact inline ribbon — five tight Cloudscape Box+Button cards in a row.
  // Number first, label after, separated by a vertical pipe so the eye
  // reads `5  |  Burndown risks` not a blocky two-line stat tile.
  return (
    <SpaceBetween direction="horizontal" size="m">
      {cards.map((c) => (
        <Box
          key={c.label}
          padding={{ vertical: 'xs', horizontal: 's' }}
          fontSize="heading-s"
        >
          <Box variant="span"
               fontSize="heading-m"
               fontWeight="bold"
               color={c.color}
               margin={{ right: 'xxs' }}>
            {c.total}
          </Box>
          {' '}
          {c.total ? (
            <Link
              variant="primary"
              onFollow={(e) => { e?.preventDefault?.(); flashSection(c.target); }}
            >
              {c.label}
            </Link>
          ) : (
            <Box variant="span" color="text-body-secondary">{c.label}</Box>
          )}
        </Box>
      ))}
    </SpaceBetween>
  );
}

export default function OpsReviewTab({ filters, onInfo }) {
  const findings = useApi('/ops-review', filters, [JSON.stringify(filters)]);
  useOrangeActionButtons([findings.data, findings.loading]);

  // Synthesis state
  const [narrative, setNarrative] = useState('');
  const [renderedNarrative, setRenderedNarrative] = useState('');
  const [synthLoading, setSynthLoading] = useState(false);
  const [synthError, setSynthError] = useState('');
  const [cached, setCached] = useState(false);

  const generate = async (force = false) => {
    setSynthLoading(true);
    setSynthError('');
    try {
      const url = buildUrl('/ops-review/synthesize', { ...filters, force: force ? 1 : 0 });
      const res = await fetch(url, { method: 'POST' });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const d = await res.json();
      setNarrative(d.narrative || '');
      setCached(!!d.cached);
    } catch (e) {
      setSynthError(String(e.message || e));
    } finally {
      setSynthLoading(false);
    }
  };

  // Render markdown → HTML, then post-pass mermaid into stable HTML string.
  // Defense-in-depth: re-sanitize at the innerHTML sink itself with DOMPurify
  // so this dangerous assignment is provably safe regardless of what the
  // upstream renderMarkdownToHtml() pipeline does. The off-screen-div hack is
  // the only way to keep the SVG from being trampled by React's next
  // reconciliation; renderMermaidIn re-sanitizes the SVG it splices in.
  useEffect(() => {
    let cancelled = false;
    if (!narrative) { setRenderedNarrative(''); return; }
    const svgPolicy = {
      ADD_TAGS: ['svg', 'g', 'path', 'rect', 'circle', 'line', 'polygon', 'polyline', 'text', 'tspan', 'defs', 'marker', 'foreignObject'],
      ADD_ATTR: ['transform', 'd', 'viewBox', 'xmlns', 'fill', 'stroke', 'stroke-width', 'x', 'y', 'cx', 'cy', 'r', 'rx', 'ry', 'x1', 'y1', 'x2', 'y2', 'points', 'text-anchor', 'font-size', 'font-family', 'class', 'id', 'style', 'marker-end', 'marker-start', 'orient', 'refX', 'refY', 'markerWidth', 'markerHeight'],
    };
    // Build the off-screen container exclusively via DOM APIs (createElement +
    // appendChild) — never via an innerHTML/outerHTML write — so no
    // user-controllable string ever reaches an HTML-parsing sink. DOMPurify
    // returns a pre-parsed, sanitized DocumentFragment we attach as nodes.
    const tmp = document.createElement('div');
    const frag = DOMPurify.sanitize(
      renderMarkdownToHtml(narrative),
      { ...svgPolicy, RETURN_DOM_FRAGMENT: true }
    );
    tmp.appendChild(frag);
    renderMermaidIn(tmp).then(() => {
      if (!cancelled) {
        // Serialize by walking sanitized DOM nodes (no innerHTML read/write on
        // user-controlled data); the consumer re-sanitizes before injection.
        const serialized = Array.from(tmp.childNodes)
          .map((n) => (n.nodeType === 1 ? n.outerHTML : n.textContent || ''))
          .join('');
        setRenderedNarrative(DOMPurify.sanitize(serialized, svgPolicy));
      }
    });
    return () => { cancelled = true; };
  }, [narrative]);

  // Lazy detailed-breakdown
  const [breakdown, setBreakdown] = useState(null);
  const [breakdownLoading, setBreakdownLoading] = useState(false);
  const [breakdownError, setBreakdownError] = useState('');
  const loadBreakdown = async () => {
    if (breakdown !== null || breakdownLoading) return;
    setBreakdownLoading(true);
    try {
      const accts = (findings.data?.account_ids || []).join(',');
      const data = await apiCall('/account-detail', { account_id: accts, days: filters.days }, { useCache: false });
      setBreakdown(data || []);
    } catch (e) {
      setBreakdownError(String(e.message || e));
    } finally {
      setBreakdownLoading(false);
    }
  };

  if (findings.loading && !findings.data) return <Spinner size="large" />;
  if (findings.error) return <Alert type="error">Failed to load findings: {String(findings.error.message)}</Alert>;
  const f = findings.data || {};
  const cap = f.capacity_health || [];
  const lc = f.lifecycle_alerts || [];
  const growth = f.growth_signal || [];
  const burndown = f.burndown_risk || [];
  const shape = f.request_shape || [];

  const counts = {
    lifecycle: lc.length,
    lifecycleCrit: lc.filter(x => x.severity === 'critical').length,
    lifecycleWarn: lc.filter(x => x.severity === 'warning').length,
    capacity: cap.length,
    capacityCrit: cap.filter(x => x.severity === 'critical').length,
    capacityWarn: cap.filter(x => x.severity === 'warning').length,
    growth: growth.length,
    burndown: burndown.length,
    shape: shape.length,
  };

  const downloadMarkdown = () => {
    const lines = [];
    const win = f.window || {};
    lines.push(`# Bedrock Ops Review`);
    lines.push(`Window: ${win.start} to ${win.end} (${win.days} days)`);
    lines.push(`Accounts covered: ${f.account_count}`);
    lines.push('');
    if (narrative) {
      lines.push(narrative);
    } else {
      // Fall back to a bare structured dump.
      lines.push('## Findings\n```json\n' + JSON.stringify(f, null, 2) + '\n```');
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `bedrock-ops-review-${win.end || 'now'}.md`;
    a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const downloadLifecycleCsv = () => {
    const cols = [
      { label: 'Severity',          get: r => r.severity },
      { label: 'Model ID',          get: r => r.modelId },
      { label: 'Base model',        get: r => r.base_modelId },
      { label: 'Legacy date',       get: r => r.legacy_date },
      { label: 'Extended access',   get: r => r.extended_access_date || '' },
      { label: 'EOL date',          get: r => r.eol_date },
      { label: 'Account count',     get: r => r.account_count },
      { label: 'Total requests',    get: r => r.total_requests },
      { label: 'Regions',           get: r => (r.regions || []).join('|') },
    ];
    const esc = (v) => {
      if (v === null || v === undefined) return '';
      const s = String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const header = cols.map(c => esc(c.label)).join(',');
    const body = lc.map(r => cols.map(c => esc(c.get(r))).join(',')).join('\n');
    const blob = new Blob([header + '\n' + body], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `bedrock-lifecycle-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return (
    <div className="ops-print-root">
      <SpaceBetween size="l">
        {/* 1. Header bar */}
        <Container header={
          <Header
            variant="h1"
            description={f.window ? `Operational review for ${f.window.start} to ${f.window.end}` : ''}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="download" className="no-print" onClick={downloadMarkdown}>Markdown</Button>
                <Button iconName="file" className="no-print" onClick={() => window.print()}>Print / PDF</Button>
              </SpaceBetween>
            }
          >
            Ops Review — {f.account_count || 0} account{f.account_count === 1 ? '' : 's'}
          </Header>
        } />

        {/* 2. KPI ribbon */}
        <Container header={<SectionHeader title="At-a-glance" sectionId="ops-kpi-ribbon" onInfo={onInfo} />}>
          <KpiRibbon counts={counts} />
        </Container>

        {/* 3. Executive summary (LLM narrative) */}
        <div id="ops-section-summary">
          <Container header={
            <SectionHeader
              title="Executive summary"
              sectionId="ops-exec-summary"
              onInfo={onInfo}
              actions={
                narrative ? (
                  <span className="action-orange-wrap">
                    <Button variant="primary" iconName="refresh" loading={synthLoading} className="no-print action-orange-btn"
                            onClick={() => generate(true)}>Regenerate</Button>
                  </span>
                ) : (
                  <span className="action-orange-wrap">
                    <Button variant="primary" iconName="gen-ai" loading={synthLoading} className="no-print action-orange-btn"
                            onClick={() => generate(false)}>Generate AI report</Button>
                  </span>
                )
              }
            />
          }>
            {synthLoading ? (
              <Box>
                <Spinner /> Generating summary…
              </Box>
            ) : synthError ? (
              <Alert type="error" header="AI synthesis failed">
                {synthError}
                <Box margin={{ top: 's' }}>
                  <Link onFollow={(e) => { e.preventDefault(); generate(true); }}>Retry</Link>
                </Box>
              </Alert>
            ) : narrative ? (
              <SpaceBetween size="s">
                <Alert type="info" header="Directional findings">
                  Generated by Claude Opus from the structured findings on this page.
                  Always cross-check the cited numbers against the tables below before sharing.
                </Alert>
                {/* Defense-in-depth: re-sanitize at the injection sink. The upstream
                    pipeline already runs DOMPurify, but we sanitize again here so the
                    dangerous sink itself is provably safe regardless of upstream changes. */}
                <div
                  className="ops-narrative"
                  // eslint-disable-next-line react/no-danger
                  dangerouslySetInnerHTML={{
                    __html: DOMPurify.sanitize(
                      renderedNarrative || renderMarkdownToHtml(narrative),
                      { ADD_TAGS: ['svg', 'g', 'path', 'rect', 'circle', 'line', 'polygon', 'polyline', 'text', 'tspan', 'defs', 'marker', 'foreignObject'], ADD_ATTR: ['transform', 'd', 'viewBox', 'xmlns', 'fill', 'stroke', 'stroke-width', 'x', 'y', 'cx', 'cy', 'r', 'rx', 'ry', 'x1', 'y1', 'x2', 'y2', 'points', 'text-anchor', 'font-size', 'font-family', 'class', 'id', 'style', 'marker-end', 'marker-start', 'orient', 'refX', 'refY', 'markerWidth', 'markerHeight'] }
                    ),
                  }}
                />
                {cached ? <Box variant="small" color="text-body-secondary">(served from cache)</Box> : null}
              </SpaceBetween>
            ) : (
              <Box color="text-body-secondary">
                Click <strong>Generate AI report</strong> above to synthesize the structured findings into a narrative review with recommendations and a Mermaid traffic-flow diagram. Requires Bedrock InvokeModel permission on the runtime credentials.
              </Box>
            )}
          </Container>
        </div>

        {/* 4. Lifecycle alerts */}
        {lc.length > 0 && (
          <div id="ops-section-lifecycle" className="ops-section-break">
            <Container header={
              <SectionHeader title="Model lifecycle alerts" sectionId="ops-lifecycle" onInfo={onInfo}
                actions={<Button iconName="download" className="no-print" onClick={downloadLifecycleCsv}>Download CSV</Button>}
              />
            }>
              <SpaceBetween size="m">
                <LifecycleTimeline alerts={lc} />
                <PaginatedTable
                  items={lc}
                  trackBy="modelId"
                  columnDefinitions={[
                    { id: 'sev', header: 'Severity', cell: r => <StatusIndicator type={severityType(r.severity)}>{r.severity}</StatusIndicator> },
                    { id: 'm',   header: 'Model',    cell: r => r.modelId },
                    { id: 'leg', header: 'Legacy date', cell: r => r.legacy_date || '—' },
                    { id: 'ext', header: 'Extended access', cell: r => r.extended_access_date || '—' },
                    { id: 'eol', header: 'EOL date', cell: r => r.eol_date || '—' },
                    { id: 'a',   header: 'Accounts', cell: r => fmt(r.account_count) },
                    { id: 't',   header: 'Requests', cell: r => fmt(r.total_requests) },
                  ]}
                  empty="No lifecycle alerts"
                />
              </SpaceBetween>
            </Container>
          </div>
        )}

        {/* 5. Capacity health */}
        {cap.length > 0 && (
          <div id="ops-section-capacity">
            <Container header={<SectionHeader title="Capacity health" sectionId="ops-capacity-health" onInfo={onInfo} />}>
              <PaginatedTable
                items={cap}
                pageSize={15}
                columnDefinitions={[
                  { id: 'sev', header: 'Severity', cell: r => <StatusIndicator type={severityType(r.severity)}>{r.severity}</StatusIndicator> },
                  { id: 'a',   header: 'Account',  cell: r => r.accountId },
                  { id: 'm',   header: 'Model',    cell: r => r.modelId },
                  { id: 'r',   header: 'Region',   cell: r => r.region },
                  { id: 't',   header: 'Requests', cell: r => fmt(r.total_requests) },
                  { id: 'p',   header: 'Throttle %', cell: r => fmtPct(r.throttle_pct, 2) },
                  { id: 'tpm', header: 'Peak TPM (obs)', cell: r => fmt(r.peak_tpm_observed) },
                  { id: 'rpm', header: 'Peak RPM (obs)', cell: r => fmt(r.peak_rpm_observed) },
                ]}
                empty="No capacity issues"
              />
            </Container>
          </div>
        )}

        {/* 6. Growth */}
        {growth.length > 0 && (
          <div id="ops-section-growth">
            <Container header={<SectionHeader title="Growth signal" sectionId="ops-growth" onInfo={onInfo} />}>
              <PaginatedTable
                items={growth}
                pageSize={10}
                columnDefinitions={[
                  { id: 'sev', header: 'Trend', cell: r => <StatusIndicator type={severityType(r.severity)}>{r.trend_label}</StatusIndicator> },
                  { id: 'a', header: 'Account', cell: r => r.accountId },
                  { id: 'pct', header: 'WoW change', cell: r => `${r.growth_pct >= 0 ? '+' : ''}${r.growth_pct}%` },
                  { id: 'rec', header: 'Recent avg tokens/day', cell: r => fmt(r.recent_avg_tokens_per_day) },
                  { id: 'old', header: 'Earlier avg tokens/day', cell: r => fmt(r.older_avg_tokens_per_day) },
                ]}
                empty="No growth signals"
              />
            </Container>
          </div>
        )}

        {/* 7. Burndown */}
        {burndown.length > 0 && (
          <div id="ops-section-burndown" className="ops-section-break">
            <Container header={<SectionHeader title="Claude 4+ burndown risk" sectionId="ops-burndown" onInfo={onInfo} />}>
              <PaginatedTable
                items={burndown}
                pageSize={10}
                columnDefinitions={[
                  { id: 'sev', header: 'Severity', cell: r => <StatusIndicator type={severityType(r.severity)}>{r.severity}</StatusIndicator> },
                  { id: 'a',   header: 'Account', cell: r => r.accountId },
                  { id: 'm',   header: 'Model',   cell: r => r.modelId },
                  { id: 'r',   header: 'Region',  cell: r => r.region },
                  { id: 'avg', header: 'Avg output / req', cell: r => fmt(r.avg_output_tokens) },
                  { id: 'p',   header: 'Peak TPM',         cell: r => fmt(r.peak_tpm_observed) },
                  { id: 'eff', header: 'Effective (5×)',   cell: r => <Box color="text-status-error" fontWeight="bold">{fmt(r.effective_peak_tpm_5x)}</Box> },
                  { id: 'oh',  header: 'Overhead %',       cell: r => fmtPct(r.burndown_overhead_pct) },
                ]}
                empty="No burndown risks"
              />
            </Container>
          </div>
        )}

        {/* 8. Request shape */}
        {shape.length > 0 && (
          <div id="ops-section-shape">
            <Container header={<SectionHeader title="Request shape outliers" sectionId="ops-request-shape" onInfo={onInfo} />}>
              <PaginatedTable
                items={shape}
                pageSize={10}
                columnDefinitions={[
                  { id: 'sev', header: 'Severity', cell: r => <StatusIndicator type={severityType(r.severity)}>{r.severity}</StatusIndicator> },
                  { id: 'a', header: 'Account', cell: r => r.accountId },
                  { id: 'm', header: 'Model',   cell: r => r.modelId },
                  { id: 'r', header: 'Region',  cell: r => r.region },
                  { id: 'i', header: 'Avg input',  cell: r => fmt(r.avg_input_tokens) },
                  { id: 'o', header: 'Avg output', cell: r => fmt(r.avg_output_tokens) },
                  { id: 'rt', header: 'Ratio',     cell: r => `${r.ratio.toFixed(1)}:1` },
                  { id: 'n', header: 'Note',       cell: r => r.note },
                ]}
                empty="No outliers"
              />
            </Container>
          </div>
        )}

        {/* 9. Detailed breakdown (lazy) */}
        <ExpandableSection
          variant="container"
          headerText="Detailed breakdown by model, region, and operation"
          headerActions={<SectionHeader title="" sectionId="ops-account-breakdown" onInfo={onInfo} />}
          onChange={({ detail }) => { if (detail.expanded) loadBreakdown(); }}
        >
          {breakdownLoading ? <ChartLoading /> :
            breakdownError ? <Alert type="error">{breakdownError}</Alert> :
            breakdown ? (
              <PaginatedTable
                items={breakdown}
                pageSize={20}
                columnDefinitions={[
                  { id: 'a', header: 'Account', cell: r => r.accountid || r.accountId },
                  { id: 'm', header: 'Model',   cell: r => r.modelid || r.modelId },
                  { id: 'r', header: 'Region',  cell: r => r.region },
                  { id: 'op', header: 'Operation', cell: r => r.operation },
                  { id: 'tt', header: 'Traffic type', cell: r => trafficLabel(r.traffic_type) },
                  { id: 't', header: 'Requests', cell: r => fmt(r.total_requests) },
                  { id: 'f', header: 'Failed',   cell: r => fmt(r.failed_requests) },
                  { id: 'i', header: 'Input tokens',  cell: r => fmt(r.total_input_tokens) },
                  { id: 'o', header: 'Output tokens', cell: r => fmt(r.total_output_tokens) },
                  { id: 'thr', header: 'Throttled', cell: r => fmt(r.throttled) },
                ]}
                empty="No detail rows"
              />
            ) : null
          }
        </ExpandableSection>
      </SpaceBetween>
    </div>
  );
}
