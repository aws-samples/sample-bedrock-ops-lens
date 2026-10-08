import assert from 'node:assert/strict';
import test from 'node:test';
import { liveSeries, seriesAverage, utilPct, lineSegments, drilldownParams, liveQuotaRoutingUnknown } from '../src/livePull.js';

const window = { start: '2026-10-06T12:00:00+00:00', end: '2026-10-06T12:05:00+00:00', hours: 1 };
const minute = (mm, extra) => ({
  minute: `2026-10-06T12:0${mm}:00+00:00`, requests: 10, quota_tpm: 1000,
  input_tokens: 400, output_tokens: 120, quota_source: 'aws_estimate', ...extra,
});

test('every minute of [start, end) is plotted and idle minutes are zero', () => {
  const s = liveSeries({ status: 'complete', window, minutes: [minute(1), minute(3, { requests: 25 })] }, 'requests');
  assert.deepEqual(s.map(p => p.x.toISOString().slice(11, 16)),
    ['12:00', '12:01', '12:02', '12:03', '12:04']);
  assert.deepEqual(s.map(p => p.y), [0, 10, 0, 25, 0]);
});

test('an active unknown minute breaks the line and withholds the average', () => {
  const s = liveSeries({ status: 'complete', window, minutes: [minute(1), minute(2, { quota_tpm: null })] }, 'quota_tpm');
  assert.deepEqual(s.map(p => p.y), [0, 1000, null, 0, 0]);
  assert.deepEqual(lineSegments(s).map(segment => segment.map(p => p.y)), [[0, 1000], [0, 0]]);
  assert.equal(seriesAverage(s), null);
});

test('failed queries cannot fabricate idle minutes or a zero average', () => {
  const s = liveSeries({ status: 'partial', window, minutes: [] }, 'requests');
  assert.deepEqual(s.map(p => p.y), [null, null, null, null, null]);
  assert.equal(seriesAverage(s), null);
  assert.deepEqual(lineSegments(s), []);
});

test('a partial pull preserves observed points and gaps between them', () => {
  const s = liveSeries({ status: 'partial', window, minutes: [minute(1), minute(3)] }, 'requests');
  assert.deepEqual(s.map(p => p.y), [null, 10, null, 10, null]);
  assert.equal(lineSegments(s).length, 2);
});

test('hourly requests preserve the selected endpoint for the same account/model/region', () => {
  const selection = { accountId: '111111111111', modelId: 'amazon.nova-pro-v1:0', region: 'us-east-1' };
  for (const endpoint of ['runtime', 'mantle']) {
    assert.deepEqual(drilldownParams({ ...selection, endpoint }), {
      account_id: selection.accountId, model_id: selection.modelId, region: selection.region,
      endpoint, days: 14,
    });
  }
});

test('an observed zero stays a point', () => {
  const s = liveSeries({ window, minutes: [minute(0, { requests: 0 })] }, 'requests');
  assert.equal(s[0].y, 0);
  assert.equal(s.length, 5);
});

test('a missing, inverted or oversized window yields no series', () => {
  assert.deepEqual(liveSeries(null, 'requests'), []);
  assert.deepEqual(liveSeries({ window, minutes: null }, 'requests'), []);
  assert.deepEqual(liveSeries({ window: { start: window.end, end: window.start }, minutes: [] }, 'requests'), []);
  assert.deepEqual(liveSeries({ window: { start: '2026-10-04T00:00:00Z', end: '2026-10-06T00:00:00Z' },
    minutes: [] }, 'requests'), []);
});

test('a full 24-hour window has 1,440 points', () => {
  const s = liveSeries({ window: { start: '2026-10-05T12:00:00Z', end: '2026-10-06T12:00:00Z' },
    minutes: [] }, 'requests');
  assert.equal(s.length, 1440);
});

test('average and utilization', () => {
  assert.equal(seriesAverage([]), null);
  assert.equal(seriesAverage([{ y: 0 }, { y: 30 }]), 15);
  assert.equal(seriesAverage([{ y: 0 }, { y: 30 }], false), null);
  assert.equal(utilPct(50, 200), 25);
  assert.equal(utilPct(null, 200), null);
  assert.equal(utilPct(50, null), null);
  assert.equal(utilPct(50, 0), null);
  assert.equal(utilPct(0, 200), 0);
});

test('a failed profile series cannot make a mixed pull look like on-demand alone', () => {
  const result = { model_id: 'amazon.nova-pro-v1:0',
    identifiers_with_data: ['amazon.nova-pro-v1:0'], has_application_profile: true };
  assert.equal(liveQuotaRoutingUnknown({ ...result, status: 'partial' }), true);
  assert.equal(liveQuotaRoutingUnknown({ ...result, status: 'complete' }), false);
  assert.equal(liveQuotaRoutingUnknown({ ...result, status: 'complete',
    identifiers_with_data: [...result.identifiers_with_data, 'abcdef123456'] }), true);
});
