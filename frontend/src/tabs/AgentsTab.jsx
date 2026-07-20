// Agents & MCP tab — AgentCore observability (G2 phase 1: metrics).
import {
  Container, SpaceBetween, Box,
} from '@cloudscape-design/components';
import { useApi, fmt, fmtMs } from '../api.js';
import { ChartLoading, SectionHeader } from '../components/Common.jsx';
import PaginatedTable from '../components/PaginatedTable.jsx';

export default function AgentsTab({ filters, onInfo }) {
  const summary = useApi('/agents/summary', filters, [JSON.stringify(filters)]);
  const tools = useApi('/agents/gateway-tools', filters, [JSON.stringify(filters)]);
  const realCost = useApi('/agents/real-cost', filters, [JSON.stringify(filters)]);

  const emptyAgents = !summary.loading && (summary.data || []).length === 0;
  const emptyTools = !tools.loading && (tools.data || []).length === 0;

  function usageTypeShort(u) {
    // "USE1-Runtime:Consumption-based:vCPU" → "Runtime vCPU"
    return (u || '').replace(/^[A-Z0-9]+-/, '').replace(/:Consumption-based/, '').replace(/:/g, ' ');
  }

  return (
    <SpaceBetween size="l">
      <Container header={<SectionHeader title="Agents (AgentCore Runtime)" sectionId="agents-runtime" onInfo={onInfo} />}>
        {summary.loading ? <ChartLoading /> : emptyAgents ? (
          <Box textAlign="center" color="text-status-inactive" padding="xl">
            No AgentCore activity detected in this window. Agents running
            outside AgentCore appear in the By User tab as IAM principals;
            standardize agents on AgentCore Runtime / Gateway to light up
            this view (sessions, latency, errors, tool calls) automatically.
          </Box>
        ) : (
          <PaginatedTable
            items={summary.data || []}
            columnDefinitions={[
              { id: 'id',   header: 'Resource',      cell: r => r.resource_id },
              { id: 'type', header: 'Type',          cell: r => r.resource_type },
              { id: 'inv',  header: 'Invocations',   cell: r => fmt(r.invocations) },
              { id: 'ses',  header: 'Sessions',      cell: r => fmt(r.sessions) },
              { id: 'err',  header: 'Errors',        cell: r => fmt(r.errors) },
              { id: 'thr',  header: 'Throttles',     cell: r => fmt(r.throttles) },
              { id: 'p99',  header: 'p99 latency',   cell: r => fmtMs(r.p99_latency_ms) },
            ]}
            empty="No agent activity"
            sortingDisabled
          />
        )}
      </Container>

      <Container header={<SectionHeader title="Billed cost (Cost Explorer, real)" sectionId="agents-cost" onInfo={onInfo} />}>
        {realCost.loading ? <ChartLoading /> : (realCost.data || []).length === 0 ? (
          <Box textAlign="center" color="text-status-inactive" padding="l">
            No billed AgentCore cost in this window (Cost Explorer has a
            24-48h lag).
          </Box>
        ) : (
          <>
            <PaginatedTable
              items={realCost.data || []}
              columnDefinitions={[
                { id: 'ut',  header: 'Billed line item', cell: r => usageTypeShort(r.usage_type) },
                { id: 'c',   header: 'Cost (USD)',       cell: r => '$' + Number(r.total_cost || 0).toFixed(2) },
                { id: 'q',   header: 'Usage qty',        cell: r => Number(r.usage_qty || 0).toFixed(2) },
              ]}
              empty="No billed cost"
              sortingDisabled
            />
            <Box color="text-status-inactive" fontSize="body-s" padding={{ top: 's' }}>
              Real billed dollars from Cost Explorer, at account level. A
              per-agent split of real dollars requires cost allocation tags
              activated at the management (payer) account.
            </Box>
          </>
        )}
      </Container>

      <Container header={<SectionHeader title="MCP tools (AgentCore Gateway)" sectionId="agents-tools" onInfo={onInfo} />}>
        {tools.loading ? <ChartLoading /> : emptyTools ? (
          <Box textAlign="center" color="text-status-inactive" padding="l">
            No Gateway tool activity. MCP servers fronted by AgentCore
            Gateway report per-tool call counts and latency here.
          </Box>
        ) : (
          <PaginatedTable
            items={tools.data || []}
            columnDefinitions={[
              { id: 'id',   header: 'Tool / target', cell: r => r.resource_id },
              { id: 'type', header: 'Type',          cell: r => r.resource_type },
              { id: 'm',    header: 'Metric',        cell: r => r.metric_name },
              { id: 'tot',  header: 'Total',         cell: r => fmt(r.total) },
              { id: 'p99',  header: 'p99',           cell: r => r.p99 != null ? fmtMs(r.p99) : '—' },
            ]}
            empty="No tool activity"
            sortingDisabled
          />
        )}
      </Container>
    </SpaceBetween>
  );
}
