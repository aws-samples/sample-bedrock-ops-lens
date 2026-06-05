// Reusable presentational components shared across tabs.
import { Container, Box, Header, Spinner, Link, StatusIndicator } from '@cloudscape-design/components';

export function ChartLoading({ height = 220, label = 'Loading…' }) {
  return (
    <Box textAlign="center" color="text-body-secondary" padding="l">
      <div style={{
        height: `${height}px`,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        gap: '8px', width: '100%',
      }}>
        <Spinner /> <span>{label}</span>
      </div>
    </Box>
  );
}

export function KpiCard({ title, value, wow, invert }) {
  return (
    <Container>
      <Box variant="awsui-key-label">{title}</Box>
      <Box variant="h1" fontSize="display-l">{value}</Box>
      {wow ? <WowBadge current={wow[0]} previous={wow[1]} invert={invert} /> : null}
    </Container>
  );
}

export function WowBadge({ current, previous, invert }) {
  if (!previous || Number(previous) === 0) return null;
  const change = ((Number(current) - Number(previous)) / Number(previous)) * 100;
  const positive = invert ? change < 0 : change > 0;
  return (
    <Box color={positive ? 'text-status-success' : 'text-status-error'} display="inline">
      {change >= 0 ? '+' : ''}{change.toFixed(1)}% WoW
    </Box>
  );
}

export function InfoLink({ sectionId, onInfo }) {
  return (
    <Link variant="info" onFollow={(e) => { e?.preventDefault?.(); onInfo(sectionId); }}>
      Info
    </Link>
  );
}

// Container header with the Info link rendered top-right via `actions={...}`
// (the reference does this — `info={...}` would render it next to the title
// which is the wrong side for a help affordance).
export function SectionHeader({ title, sectionId, onInfo, variant = 'h2', actions, description }) {
  const right = actions
    ? actions
    : (sectionId && onInfo ? <InfoLink sectionId={sectionId} onInfo={onInfo} /> : undefined);
  return (
    <Header variant={variant} actions={right} description={description}>{title}</Header>
  );
}

export function SeverityCell({ severity, label }) {
  const t = severity === 'critical' ? 'error'
    : severity === 'warning' ? 'warning'
    : severity === 'success' ? 'success'
    : 'info';
  return <StatusIndicator type={t}>{label || severity}</StatusIndicator>;
}

// Cloudscape's BarChart i18n object — covers the warnings about missing keys.
export const CHART_I18N = {
  filterLabel: 'Filter', filterPlaceholder: 'Filter…',
  filterSelectedAriaLabel: 'selected',
  detailPopoverDismissAriaLabel: 'Dismiss',
  legendAriaLabel: 'Legend',
  chartAriaRoleDescription: 'chart',
};
