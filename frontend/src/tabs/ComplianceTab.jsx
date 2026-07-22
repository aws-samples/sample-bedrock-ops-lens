// Compliance tab — Guardrails intervention metrics (G3 path A).
import { useMemo } from 'react';
import {
  Container, SpaceBetween, BarChart, Box, ColumnLayout,
} from '@cloudscape-design/components';
import { useApi, fmt } from '../api.js';
import { ChartLoading, SectionHeader, CHART_I18N } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';

const POLICY_LABELS = {
  ContentPolicy: 'Content filters',
  TopicPolicy: 'Denied topics',
  WordPolicy: 'Word filters',
  SensitiveInformationPolicy: 'PII / Sensitive info',
  ContextualGroundingPolicy: 'Grounding (hallucination)',
};

function Kpi({ label, value }) {
  return (
    <div>
      <Box variant="awsui-key-label">{label}</Box>
      <Box fontSize="display-l" fontWeight="bold">{value}</Box>
    </div>
  );
}

export default function ComplianceTab({ filters, onInfo }) {
  const totals = useApi('/compliance/totals', filters, [JSON.stringify(filters)]);
  const byPolicy = useApi('/compliance/summary', filters, [JSON.stringify(filters)]);
  const byGuardrail = useApi('/compliance/by-guardrail', filters, [JSON.stringify(filters)]);

  const t = totals.data || {};
  const interventionRate = t.invocations > 0
    ? ((100 * (t.intervened || 0)) / t.invocations).toFixed(1) + '%'
    : '—';

  const chartSeries = useMemo(() => ([{
    title: 'Interventions', type: 'bar',
    data: (byPolicy.data || []).map(r => ({
      x: POLICY_LABELS[r.policy_type] || r.policy_type,
      y: Number(r.intervened || 0),
    })),
  }]), [byPolicy.data]);

  const empty = !totals.loading && !(t.invocations > 0);

  return (
    <SpaceBetween size="l">
      <Container header={<SectionHeader title="Guardrails activity" sectionId="compliance-kpi" onInfo={onInfo} />}>
        {totals.loading ? <ChartLoading height={80} /> : empty ? (
          <Box textAlign="center" color="text-status-inactive" padding="xl">
            No guardrail activity in this window. Attach guardrails to
            invocations (guardrailConfig) or enforce them org-wide with
            Organizational Safeguards to populate this view.
          </Box>
        ) : (
          <ColumnLayout columns={4} variant="text-grid">
            <Kpi label="Guardrail invocations" value={fmt(t.invocations)} />
            <Kpi label="Interventions" value={fmt(t.intervened)} />
            <Kpi label="Intervention rate" value={interventionRate} />
            <Kpi label="Text units consumed" value={fmt(t.text_units)} />
          </ColumnLayout>
        )}
      </Container>

      <Container header={<SectionHeader title="Interventions by policy type" sectionId="compliance-policy" onInfo={onInfo} />}>
        {byPolicy.loading ? <ChartLoading height={280} /> :
          (byPolicy.data || []).length === 0 ? (
            <Box textAlign="center" color="text-status-inactive" padding="l">No interventions recorded</Box>
          ) : (
            <BarChart
              series={chartSeries}
              xScaleType="categorical"
              hideFilter
              ariaLabel="Interventions by policy type"
              i18nStrings={CHART_I18N}
              height={280}
              xTitle="Policy" yTitle="Interventions"
            />
          )}
      </Container>

      <Container header={<SectionHeader title="Guardrails" sectionId="compliance-guardrails" onInfo={onInfo} />}>
        {byGuardrail.loading ? <ChartLoading /> :
          <PaginatedTable
            items={byGuardrail.data || []}
            columnDefinitions={[
              { id: 'arn', header: 'Guardrail',    cell: r => (r.guardrail_arn || '').split('/').pop() },
              { id: 'ver', header: 'Version',      cell: r => r.guardrail_version || 'DRAFT' },
              { id: 'inv', header: 'Invocations',  cell: r => fmt(r.invocations) },
              { id: 'int', header: 'Interventions', cell: r => fmt(r.intervened) },
            ]}
            empty="No guardrails observed"
            sortingDisabled
          />
        }
      </Container>
    </SpaceBetween>
  );
}
