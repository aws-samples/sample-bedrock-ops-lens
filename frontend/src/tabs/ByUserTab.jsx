// By User tab — per IAM caller identity attribution.
// Data comes from Bedrock invocation logs (identity.arn), captured
// automatically on every call: no tagging discipline required.
import { useMemo, useState } from 'react';
import {
  Container, SpaceBetween, BarChart, Box, SegmentedControl,
} from '@cloudscape-design/components';
import { useApi, fmt } from '../api.js';
import { ChartLoading, SectionHeader, CHART_I18N } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';

function modelShort(id) {
  return (id || '').replace(/^us\./, '').replace(/^eu\./, '').replace(/^global\./, '')
    .replace(/^anthropic\./, '').replace(/^amazon\./, '').replace(/^meta\./, '')
    .replace(/^cohere\./, '').replace(/^mistral\./, '')
    .split(':')[0];
}

// "role/session" labels can get long; keep the tail (most specific part).
function labelShort(label, max = 42) {
  const s = label || 'unknown';
  return s.length <= max ? s : '…' + s.slice(-(max - 1));
}

export default function ByUserTab({ filters, onInfo }) {
  // Axis: 'group' = role (app / team / workload), 'user' = session
  // (individual, SSO login), 'principal' = full role/session identity.
  const [axis, setAxis] = useState('group');
  const summaryParams = useMemo(() => ({ ...filters, group_by: axis }), [filters, axis]);
  const summary = useApi('/by-user/summary', summaryParams, [JSON.stringify(summaryParams)]);
  const byModel = useApi('/by-user/by-model', filters, [JSON.stringify(filters)]);

  const chartSeries = useMemo(() => {
    const data = (summary.data || []).slice(0, 10);
    return [
      { title: 'Requests', type: 'bar', data: data.map(r => ({ x: labelShort(r.caller || r.principal_label), y: Number(r.total_requests || 0) })) },
    ];
  }, [summary.data]);

  const axisLabel = axis === 'group' ? 'App / team (role)' : axis === 'user' ? 'User (session)' : 'Principal';

  return (
    <SpaceBetween size="l">
      <Container header={
        <SectionHeader
          title={`Top callers by requests — ${axisLabel}`}
          sectionId="by-user-chart"
          onInfo={onInfo}
          actions={
            <SegmentedControl
              selectedId={axis}
              onChange={({ detail }) => setAxis(detail.selectedId)}
              options={[
                { id: 'group',     text: 'App / Group' },
                { id: 'user',      text: 'User' },
                { id: 'principal', text: 'Principal' },
              ]}
            />
          }
        />
      }>
        {summary.loading ? <ChartLoading height={300} /> :
          (summary.data || []).length === 0 ? (
            <Box textAlign="center" color="text-status-inactive" padding="xl">
              No caller-identity data yet. This view reads Bedrock model
              invocation logs (identity.arn); data appears after the first
              ingest following log delivery.
            </Box>
          ) :
          <BarChart
            series={chartSeries}
            xScaleType="categorical"
            hideFilter
            ariaLabel="Requests by caller identity"
            i18nStrings={CHART_I18N}
            height={300}
            xTitle="Caller (role/session)" yTitle="Requests"
          />
        }
      </Container>

      <Container header={<SectionHeader title="Callers" sectionId="by-user-table" onInfo={onInfo} />}>
        {summary.loading ? <ChartLoading /> :
          <PaginatedTable
            items={summary.data || []}
            columnDefinitions={[
              { id: 'label', header: 'Caller',          cell: r => r.caller || r.principal_label },
              { id: 'req',   header: 'Requests',        cell: r => fmt(r.total_requests) },
              { id: 'fail',  header: 'Failed',          cell: r => fmt(r.failed_requests) },
              { id: 'in',    header: 'Input tokens',    cell: r => fmt(r.input_tokens) },
              { id: 'out',   header: 'Output tokens',   cell: r => fmt(r.output_tokens) },
              { id: 'nm',    header: 'Models',          cell: r => fmt(r.distinct_models) },
              { id: 'np',    header: 'Principals',      cell: r => fmt(r.distinct_principals) },
            ]}
            empty="No caller-identity data"
            sortingDisabled
          />
        }
      </Container>

      <Container header={<SectionHeader title="Caller × model" sectionId="by-user-model" onInfo={onInfo} />}>
        {byModel.loading ? <ChartLoading /> :
          <PaginatedTable
            items={byModel.data || []}
            columnDefinitions={[
              { id: 'label', header: 'Caller',        cell: r => r.principal_label || r.principal_arn },
              { id: 'm',     header: 'Model',         cell: r => modelShort(r.modelid || r.modelId) },
              { id: 'req',   header: 'Requests',      cell: r => fmt(r.total_requests) },
              { id: 'in',    header: 'Input tokens',  cell: r => fmt(r.input_tokens) },
              { id: 'out',   header: 'Output tokens', cell: r => fmt(r.output_tokens) },
            ]}
            empty="No data"
            sortingDisabled
          />
        }
      </Container>
    </SpaceBetween>
  );
}
