// Peak Hours tab — single container with auto-detected timezone.
import { useMemo } from 'react';
import { Container, BarChart } from '@cloudscape-design/components';
import { useApi, fmt } from '../api.js';
import { ChartLoading, SectionHeader, CHART_I18N } from '../components/Common.jsx';

export default function PeakTab({ filters, onInfo }) {
  const heat = useApi('/hourly-heatmap', filters, [JSON.stringify(filters)]);

  // Convert UTC hour → local hour using browser's offset.
  const tzOffset = useMemo(() => -new Date().getTimezoneOffset() / 60, []);
  const tzAbbr = useMemo(() => {
    try {
      const parts = new Date().toLocaleTimeString(undefined, { timeZoneName: 'short' }).split(' ');
      return parts[parts.length - 1] || 'local';
    } catch { return 'local'; }
  }, []);

  // /hourly-heatmap now returns { rows, coverage }; tolerate the old bare array
  // so a stale cached response cannot blank the chart.
  const rows = useMemo(
    () => (Array.isArray(heat.data) ? heat.data : (heat.data?.rows || [])),
    [heat.data]);
  const coverage = heat.data?.coverage || null;

  const series = useMemo(() => {
    if (!rows.length) return [];
    // Re-bucket by local hour; the API returns UTC hour buckets.
    const buckets = Array.from({ length: 24 }, () => ({ requests: 0, throttled: 0 }));
    for (const r of rows) {
      const lh = ((Number(r.hour) + tzOffset + 24) % 24) | 0;
      buckets[lh].requests += Number(r.total_requests || 0);
      buckets[lh].throttled += Number(r.throttled || 0);
    }
    return [
      { title: 'Requests',  type: 'bar', data: buckets.map((b, h) => ({ x: h, y: b.requests })) },
      { title: 'Throttled', type: 'bar', data: buckets.map((b, h) => ({ x: h, y: b.throttled })) },
    ];
  }, [rows, tzOffset]);

  return (
    <Container header={
      <SectionHeader
        title={`Requests by hour of day (${tzAbbr}) - ${
          // State the window actually covered. Hourly data comes from the
          // ingester's rolling lookback (14 days by default), so a 30/60/90-day
          // selection is not the span behind these bars (finding 17).
          coverage && coverage.days_covered > 0 && coverage.days_covered < coverage.days_requested
            ? `${coverage.days_covered} day${coverage.days_covered === 1 ? '' : 's'} of hourly data available (${coverage.min_date} to ${coverage.max_date}) of the ${coverage.days_requested} selected`
            : `last ${filters.days} day${filters.days === 1 ? '' : 's'}`
        }`}
        sectionId="peak-hours"
        onInfo={onInfo}
      />
    }>
      {heat.loading ? <ChartLoading height={300} /> :
        <BarChart
          series={series}
          xScaleType="categorical"
          stackedBars
          hideFilter
          ariaLabel="Hour-of-day request volume"
          i18nStrings={{ ...CHART_I18N, yTickFormatter: fmt }}
          height={300}
          xTitle={`Hour (${tzAbbr})`}
          yTitle="Requests"
        />
      }
    </Container>
  );
}
