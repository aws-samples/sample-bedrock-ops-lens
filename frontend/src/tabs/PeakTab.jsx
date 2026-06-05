// Peak Hours tab — single container with auto-detected timezone.
import { useMemo } from 'react';
import { Container, BarChart } from '@cloudscape-design/components';
import { useApi } from '../api.js';
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

  const series = useMemo(() => {
    if (!heat.data) return [];
    // Re-bucket by local hour; the API returns UTC hour buckets.
    const buckets = Array.from({ length: 24 }, () => ({ requests: 0, throttled: 0 }));
    for (const r of heat.data) {
      const lh = ((Number(r.hour) + tzOffset + 24) % 24) | 0;
      buckets[lh].requests += Number(r.total_requests || 0);
      buckets[lh].throttled += Number(r.throttled || 0);
    }
    return [
      { title: 'Requests',  type: 'bar', data: buckets.map((b, h) => ({ x: h, y: b.requests })) },
      { title: 'Throttled', type: 'bar', data: buckets.map((b, h) => ({ x: h, y: b.throttled })) },
    ];
  }, [heat.data, tzOffset]);

  return (
    <Container header={
      <SectionHeader
        title={`Requests by hour of day (${tzAbbr}) — last ${filters.days} day${filters.days === 1 ? '' : 's'}`}
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
          i18nStrings={CHART_I18N}
          height={300}
          xTitle={`Hour (${tzAbbr})`}
          yTitle="Requests"
        />
      }
    </Container>
  );
}
