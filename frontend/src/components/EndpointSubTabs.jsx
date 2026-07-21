// EndpointSubTabs — wraps a tab body with a `bedrock-runtime` vs
// `bedrock-mantle` segmented switcher. The active endpoint is passed
// down so the tab's data fetches scope correctly.
//
// Coverage badge: each tab declares its Mantle coverage so users
// understand provenance immediately.
//
//   'full'         — same telemetry as runtime; CW publishes everything
//   'metric'       — CW publishes the core metrics but with gaps
//                    (4xx-only, no 5xx, etc.)
//   'log-derived'  — Mantle has no CW signal here; data comes from
//                    Model Invocation Logs if the customer enabled them
//   'defaults'     — only static defaults available (e.g., Service
//                    Quotas doesn't expose Mantle quotas; use the
//                    published defaults from AWS docs)
//   'shared'       — the data source is endpoint-agnostic (Cost Explorer,
//                    model lifecycle metadata). Both tabs show identical
//                    numbers. Banner makes that explicit so users don't
//                    think the switcher is broken when nothing changes.
//   'none'         — Mantle does not publish this signal at all; render
//                    an explicit "not available" empty state, NEVER zero
//
// Design rule: never show a blank/zero chart for Mantle when the source
// has no data — that's indistinguishable from "healthy" and would mislead
// operators. Where Mantle genuinely has no signal, hide the Mantle
// sub-tab entirely rather than render an empty state.

import {
  SegmentedControl, Box, StatusIndicator, SpaceBetween, Alert,
} from '@cloudscape-design/components';

const COVERAGE_LABEL = {
  full:         { label: 'Live metric',            type: 'success' },
  metric:       { label: 'Live metric (partial)',  type: 'success' },
  'log-derived':{ label: 'Log-derived',            type: 'pending' },
  defaults:     { label: 'Defaults only',          type: 'info' },
  shared:       { label: 'Endpoint-agnostic',      type: 'info' },
  none:         { label: 'Not available',          type: 'warning' },
};

export default function EndpointSubTabs({
  selected,                // 'runtime' | 'mantle'
  onChange,                // (next) => void
  runtimeCoverage = 'full',
  mantleCoverage  = 'metric',
  // When false, the bedrock-mantle segment is HIDDEN entirely — no
  // switcher, no coverage badge, just the runtime body. Use this for
  // tabs whose signal Mantle genuinely doesn't publish (and can't be
  // derived by any means), e.g. Latency when no invocation-log-derived
  // Mantle rows exist. Rule: show Mantle wherever data is obtainable by
  // ANY means (CW metric or invocation logs); hide only when there's
  // truly nothing — never render a blank Mantle view.
  mantleAvailable = true,
  children,                // function: ({ endpoint, coverage }) => ReactNode
}) {
  // If Mantle isn't available for this tab, force runtime and render the
  // body with no switcher chrome.
  if (!mantleAvailable) {
    return children({ endpoint: 'runtime', coverage: runtimeCoverage });
  }

  const coverageMap = { runtime: runtimeCoverage, mantle: mantleCoverage };
  const activeCov = coverageMap[selected] || 'none';
  const meta = COVERAGE_LABEL[activeCov];

  // When BOTH endpoints share the same data source (Cost Explorer,
  // model lifecycle), surface a banner so users don't wonder why the
  // numbers don't change when they toggle.
  const isShared = runtimeCoverage === 'shared' && mantleCoverage === 'shared';

  return (
    <SpaceBetween size="s">
      <Box>
        <SpaceBetween size="m" direction="horizontal">
          <SegmentedControl
            selectedId={selected}
            onChange={({ detail }) => onChange(detail.selectedId)}
            options={[
              { id: 'runtime', text: 'bedrock-runtime' },
              { id: 'mantle',  text: 'bedrock-mantle' },
            ]}
          />
          <Box variant="span" color="text-body-secondary">
            <StatusIndicator type={meta.type}>{meta.label}</StatusIndicator>
          </Box>
        </SpaceBetween>
      </Box>
      {isShared && (
        <Alert type="info" header="Same numbers on both endpoints by design">
          The data on this tab comes from a source that doesn't split by
          Bedrock endpoint (Cost Explorer is consolidated; model
          lifecycle is a property of the model, not the API path). The
          switcher above is here for consistency with the rest of the
          dashboard — toggling it won't change what you see here.
        </Alert>
      )}
      {children({ endpoint: selected, coverage: activeCov })}
    </SpaceBetween>
  );
}

// Convenience: a standardised empty state for the 'none' coverage case.
// Use this inside a tab when the active endpoint genuinely has no data
// path. It's deliberately distinct from "no rows in window" — a real
// product gap, not a transient.
export function EndpointNotAvailable({ message }) {
  return (
    <Box textAlign="center" padding="xxl" color="text-body-secondary">
      <SpaceBetween size="xs">
        <StatusIndicator type="warning">Not published</StatusIndicator>
        <Box variant="p" color="text-body-secondary">
          {message || (
            'This metric is not yet published for the bedrock-mantle ' +
            'endpoint. Switch to bedrock-runtime above, or enable Model ' +
            'Invocation Logging for per-request data.'
          )}
        </Box>
      </SpaceBetween>
    </Box>
  );
}
