// Governance tab — reconciliation of observed usage vs the declarative
// application registry (db/registry.yaml, AI Act-style referential).
import {
  Container, SpaceBetween, Box, ColumnLayout, StatusIndicator,
} from '@cloudscape-design/components';
import { useApi } from '../api.js';
import { ChartLoading, SectionHeader } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';

const STATUS_META = {
  undeclared:      { type: 'error',   label: 'Undeclared (shadow AI)' },
  model_drift:     { type: 'warning', label: 'Model drift' },
  declared_unused: { type: 'pending', label: 'Declared, unused' },
  compliant:       { type: 'success', label: 'Compliant' },
};

function Kpi({ label, value }) {
  return (
    <div>
      <Box variant="awsui-key-label">{label}</Box>
      <Box fontSize="display-l" fontWeight="bold">{value}</Box>
    </div>
  );
}

export default function GovernanceTab({ filters, onInfo }) {
  const recon = useApi('/governance/reconciliation', filters, [JSON.stringify(filters)]);
  const s = recon.data?.summary || {};
  const rows = recon.data?.rows || [];

  return (
    <SpaceBetween size="l">
      <Container header={<SectionHeader title="Registry reconciliation" sectionId="gov-kpi" onInfo={onInfo} />}>
        {recon.loading ? <ChartLoading /> : (
          <ColumnLayout columns={5} variant="text-grid">
            <Kpi label="Declared apps" value={s.declared_apps ?? '—'} />
            <Kpi label="Observed apps" value={s.observed_apps ?? '—'} />
            <Kpi label="Undeclared usage" value={s.undeclared ?? '—'} />
            <Kpi label="Model drift" value={s.model_drift ?? '—'} />
            <Kpi label="Declared unused" value={s.declared_unused ?? '—'} />
          </ColumnLayout>
        )}
      </Container>

      <Container header={<SectionHeader title="Observed vs Declared" sectionId="gov-table" onInfo={onInfo} />}>
        {recon.loading ? <ChartLoading /> : (
          <>
            <PaginatedTable
              items={rows}
              columnDefinitions={[
                {
                  id: 'status', header: 'Status',
                  cell: r => {
                    const m = STATUS_META[r.status] || { type: 'info', label: r.status };
                    return <StatusIndicator type={m.type}>{m.label}</StatusIndicator>;
                  },
                },
                { id: 'app',   header: 'App / identity', cell: r => r.app },
                { id: 'name',  header: 'Declaration',    cell: r => r.declared_name || '—' },
                { id: 'model', header: 'Observed model', cell: r => r.modelId || '—' },
                { id: 'risk',  header: 'AI Act risk',  cell: r => r.ai_act_risk || '—' },
                { id: 'inv',   header: 'Invocations',    cell: r => r.invocations || 0 },
              ]}
              empty="No reconciliation data"
              sortingDisabled
            />
            <Box color="text-status-inactive" fontSize="body-s" padding={{ top: 's' }}>
              Referential: db/registry.yaml (git-versioned). Detective
              governance by default: observe and flag, no a-priori
              blocking. For entries classified as high risk (EU AI Act),
              IAM enforcement is rendered opt-in from the same declaration
              (GET /api/governance/policy/&#123;app_id&#125;).
            </Box>
          </>
        )}
      </Container>
    </SpaceBetween>
  );
}
