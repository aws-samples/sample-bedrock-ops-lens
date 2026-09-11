/**
 * One date convention for the whole app: UTC.
 *
 * Every fact the API returns is bucketed by a UTC calendar day (`event_date`,
 * or a `year`/`month`/`day` triple) or a UTC hour (`ts`), because that is how
 * CloudWatch and Bedrock invocation logs bucket them. The browser's time zone
 * is irrelevant to what the numbers mean.
 *
 * Audit finding 17: the app mixed two conventions, and both were wrong in a
 * negative-offset zone.
 *
 *   - Request series built x values with `new Date(y, m - 1, d)` - LOCAL
 *     midnight - while cost series used `new Date('2026-09-08')`, which is UTC
 *     midnight. On the Overview tab those two land on different x positions for
 *     the same day, so in America/Los_Angeles the cost line was drawn and
 *     labelled one day earlier than the request line above it.
 *   - Hourly labels ran `new Date(ts)` on a naive timestamp (parsed as local)
 *     and then read `getHours()`, while the label said "(hour, UTC)". West of
 *     Greenwich that named the wrong hour as the busiest one.
 *
 * Use these helpers for anything derived from an API date. Never call
 * `toLocaleDateString` on API data without `timeZone: 'UTC'`.
 */

/** UTC midnight for a year/month/day triple (month is 1-based, as the API sends). */
export function utcDay(year, month, day) {
  return new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
}

/** UTC midnight for a row carrying year/month/day. */
export function utcDayOf(row) {
  return utcDay(row.year, row.month, row.day);
}

/**
 * UTC midnight for a 'YYYY-MM-DD' string. Bare date strings already parse as
 * UTC, but going through the parts is explicit and also tolerates a full
 * timestamp by keeping only its date part.
 */
export function utcDayFromString(s) {
  if (s instanceof Date) return s;
  const [y, m, d] = String(s).slice(0, 10).split('-');
  return utcDay(y, m, d);
}

/** "Sep 8" - the UTC day, regardless of where the browser is. */
export function fmtDayUTC(d, opts = { month: 'short', day: 'numeric' }) {
  return new Date(d).toLocaleDateString(undefined, { ...opts, timeZone: 'UTC' });
}

/** "Sep 8, 2026" in UTC. */
export function fmtDayYearUTC(d) {
  return fmtDayUTC(d, { month: 'numeric', day: 'numeric', year: 'numeric' });
}

/** "Sep 8, 14:00" - the UTC hour. */
export function fmtHourUTC(ts) {
  const d = new Date(ts);
  return `${fmtDayUTC(d)}, ${String(d.getUTCHours()).padStart(2, '0')}:00`;
}

/** Two-digit UTC hour, for labels that add their own ":00 UTC" suffix. */
export function utcHour(ts) {
  return String(new Date(ts).getUTCHours()).padStart(2, '0');
}
