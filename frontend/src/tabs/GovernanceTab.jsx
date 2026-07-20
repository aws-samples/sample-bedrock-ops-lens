// Governance tab — reconciliation of observed usage vs the declarative
// application registry (db/registry.yaml, AI Act-style referential).
import {
  Container, SpaceBetween, Box, ColumnLayout, StatusIndicator,
} from '@cloudscape-design/components';
import { useApi } from '../api.js';
import { ChartLoading, SectionHeader } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';

const STATUS_META = {
  undeclared:      { type: 'error',   label: 'Non déclaré (shadow AI)' },
  model_drift:     { type: 'warning', label: 'Dérive modèle' },
  declared_unused: { type: 'pending', label: 'Déclaré, non utilisé' },
  compliant:       { type: 'success', label: 'Conforme' },
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
            <Kpi label="Apps déclarées" value={s.declared_apps ?? '—'} />
            <Kpi label="Apps observées" value={s.observed_apps ?? '—'} />
            <Kpi label="Usage non déclaré" value={s.undeclared ?? '—'} />
            <Kpi label="Dérives modèle" value={s.model_drift ?? '—'} />
            <Kpi label="Déclarées inutilisées" value={s.declared_unused ?? '—'} />
          </ColumnLayout>
        )}
      </Container>

      <Container header={<SectionHeader title="Observé vs Déclaré" sectionId="gov-table" onInfo={onInfo} />}>
        {recon.loading ? <ChartLoading /> : (
          <>
            <PaginatedTable
              items={rows}
              columnDefinitions={[
                {
                  id: 'status', header: 'Statut',
                  cell: r => {
                    const m = STATUS_META[r.status] || { type: 'info', label: r.status };
                    return <StatusIndicator type={m.type}>{m.label}</StatusIndicator>;
                  },
                },
                { id: 'app',   header: 'App / identité', cell: r => r.app },
                { id: 'name',  header: 'Déclaration',    cell: r => r.declared_name || '—' },
                { id: 'model', header: 'Modèle observé', cell: r => r.modelId || '—' },
                { id: 'risk',  header: 'Risque IA Act',  cell: r => r.ai_act_risk || '—' },
                { id: 'inv',   header: 'Invocations',    cell: r => r.invocations || 0 },
              ]}
              empty="Aucune donnée de réconciliation"
              sortingDisabled
            />
            <Box color="text-status-inactive" fontSize="body-s" padding={{ top: 's' }}>
              Référentiel: db/registry.yaml (versionné git). Gouvernance
              détective par défaut: on observe et on flagge, aucun blocage
              a priori. Pour les cas classés à risque élevé (IA Act),
              l'enforcement IAM est généré en opt-in depuis la même
              déclaration (GET /api/governance/policy/&#123;app_id&#125;).
            </Box>
          </>
        )}
      </Container>
    </SpaceBetween>
  );
}
