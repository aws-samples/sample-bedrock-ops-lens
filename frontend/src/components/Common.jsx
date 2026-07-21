// Reusable presentational components shared across tabs.
import { Container, Box, Header, Spinner, Link, StatusIndicator, SegmentedControl } from '@cloudscape-design/components';

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

export function KpiCard({ title, value, wow, invert, split, note, tabs }) {
  // A KPI value is usually a short number ("547", "4.20%") but sometimes a
  // free-text string ("search-service"). Long strings at display-l wrap to two
  // lines and make one tile taller than its row-mates — the KPI ribbon then
  // looks ragged. So: (1) fillHeight so every tile in a Grid row is the same
  // height regardless of content, and (2) scale the value font down for long
  // text values so they stay on one line and never blow up the tile.
  const asText = value == null ? '' : String(value);
  const isNumericish = /^[\d.,%$\sKMBkmb+\-]+$/.test(asText);  // "547", "4.20%", "$71.09"
  // Long non-numeric strings get a smaller size; numbers keep the big display font.
  const valFontSize = isNumericish
    ? 'display-l'
    : asText.length > 16 ? 'heading-m'
    : asText.length > 10 ? 'heading-l'
    : 'display-l';
  return (
    <Container fitHeight>
      <Box variant="awsui-key-label">{title}</Box>
      {/* Optional in-card toggle — e.g. flip the spend tile between an
          endpoint's allocated slice and the combined Total. */}
      {tabs ? (
        <Box padding={{ bottom: 'xxs' }}>
          <SegmentedControl
            selectedId={tabs.selectedId}
            onChange={({ detail }) => tabs.onChange(detail.selectedId)}
            options={tabs.options}
            label={title}
          />
        </Box>
      ) : null}
      <Box
        variant="h1"
        fontSize={valFontSize}
        // Break only between words (not mid-word) and cap at two lines with an
        // ellipsis so an unusually long value can never distort the layout.
        // Full value stays available on hover via the title attribute.
      >
        <span title={asText} style={{
          display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
          overflow: 'hidden', overflowWrap: 'anywhere', wordBreak: 'normal',
        }}>{value}</span>
      </Box>
      {wow ? <WowBadge current={wow[0]} previous={wow[1]} invert={invert} /> : null}
      {/* Optional runtime/mantle composition line. Total stays the headline
          above; this shows how it splits by Bedrock endpoint at a glance.
          Rendered only when there IS mantle usage, so runtime-only fleets
          aren't cluttered with a "· mantle 0" tail. */}
      {split ? (
        <Box color="text-body-secondary" fontSize="body-s">
          runtime {split.runtime}{split.mantle != null ? ` · mantle ${split.mantle}` : ''}
        </Box>
      ) : null}
      {/* Optional caveat line — e.g. a metric that can't be split by the
          active filter (Cost Explorer doesn't break spend out by endpoint). */}
      {note ? (
        <Box color="text-body-secondary" fontSize="body-s"><i>{note}</i></Box>
      ) : null}
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
