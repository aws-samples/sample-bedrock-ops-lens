// Chart helpers for the Quota drill-down's "Pull live data" panel. Kept free of
// React so the per-minute semantics can be unit-tested directly.

const MINUTE_MS = 60_000;
// The API allows at most 24 hours; anything wider is not a live-pull window.
const MAX_WINDOW_MS = 24 * 60 * MINUTE_MS;

// One point per minute of [start, end). Missing data from an incomplete pull
// and unestablished active measurements stay null. Charts must break at nulls.
export function liveSeries(result, field) {
  if (!result?.window || !Array.isArray(result.minutes)) return [];
  const start = Date.parse(result.window.start);
  const end = Date.parse(result.window.end);
  if (!Number.isFinite(start) || !Number.isFinite(end)
      || end <= start || end - start > MAX_WINDOW_MS) return [];
  const byMinute = new Map(result.minutes.map(m => [Date.parse(m.minute), m]));
  const out = [];
  for (let t = start; t < end; t += MINUTE_MS) {
    const m = byMinute.get(t);
    const y = m ? m[field] : result.status === 'complete' ? 0 : null;
    out.push({ x: new Date(t), y: Number.isFinite(y) ? y : null });
  }
  return out;
}

export function seriesAverage(series, complete = true) {
  if (!complete || !series.length || series.some(p => !Number.isFinite(p.y))) return null;
  return series.reduce((sum, p) => sum + p.y, 0) / series.length;
}

export function liveQuotaRoutingUnknown(result) {
  return (result.identifiers_with_data || []).some(i => i !== result.model_id)
    || (result.status !== 'complete' && result.has_application_profile === true);
}

// Cloudscape connects missing x values and treats a null y as zero. Split the
// data ourselves so an unknown interval is a visible gap, never an idle line.
export function lineSegments(series) {
  const segments = [];
  let current = [];
  for (const point of series) {
    if (!Number.isFinite(point.y)) {
      if (current.length) segments.push(current);
      current = [];
    } else current.push(point);
  }
  if (current.length) segments.push(current);
  return segments;
}

export function drilldownParams(selection, days = 14) {
  return {
    account_id: selection.accountId,
    model_id: selection.modelId,
    region: selection.region,
    endpoint: selection.endpoint,
    days,
  };
}

export function utilPct(value, limit) {
  return value != null && limit ? (value / limit) * 100 : null;
}
