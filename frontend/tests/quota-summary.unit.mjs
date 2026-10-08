import assert from 'node:assert/strict';
import test from 'node:test';
import { summarizeQuotaRows, quotaUtilization } from '../src/quotaSummary.js';

test('partial minute coverage never understates an observed hourly lower bound', () => {
  assert.deepEqual(quotaUtilization(100, 1000, 2000, false),
    { percentage: 50, lowerBound: true });
  // Even a stale complete flag cannot make a contradictory peak exact.
  assert.deepEqual(quotaUtilization(100, 1000, 2000, true),
    { percentage: 50, lowerBound: true });
});

test('complete minute coverage uses the matching limit once', () => {
  assert.deepEqual(quotaUtilization(1000, 100, 2000, true),
    { percentage: 50, lowerBound: false });
  assert.deepEqual(quotaUtilization(1000, 100, 10000, true),
    { percentage: 10, lowerBound: false });
});

test('missing measurements and missing or invalid limits remain unknown', () => {
  assert.equal(quotaUtilization(null, null, 1000, true).percentage, null);
  for (const limit of [null, undefined, 0, -1, NaN, Infinity]) {
    assert.equal(quotaUtilization(100, 50, limit, true).percentage, null);
  }
});

test('explicit idle measurements remain zero and hourly-only rates are lower bounds', () => {
  assert.deepEqual(quotaUtilization(0, 0, 1000, true),
    { percentage: 0, lowerBound: false });
  assert.deepEqual(quotaUtilization(null, 200, 1000, false),
    { percentage: 20, lowerBound: true });
});

const base = {
  accountId: '111111111111', modelId: 'amazon.nova-lite-v1:0', region: 'us-east-1',
  endpoint: 'runtime', minute_tpm: 100, minute_rpm: 10,
  hourly_avg_tpm: 50, hourly_avg_rpm: 1, minute_coverage_complete: true,
  tpm_limit: 1000, rpm_limit: 100, tpm_util_pct: 10, rpm_util_pct: 10,
  quota_src: 'aws_estimate', minute_days: 7, burstiness_x: 2,
};

test('an hourly fallback never changes the measured numerator or percentage', () => {
  const other = { ...base, accountId: '222222222222',
    minute_tpm: null, minute_rpm: null, minute_coverage_complete: false,
    hourly_avg_tpm: 500, tpm_util_pct: null, rpm_util_pct: null };
  const [row] = summarizeQuotaRows([base, other], 'per-model');
  assert.equal(row.minute_tpm, 100);
  assert.equal(row.hourly_avg_tpm, 50);
  assert.equal(row.tpm_lim, 1000);
  // The measured 10% is kept, paired with its own numerator and limit, and
  // flagged as a lower bound because the other observation has no percentage.
  // Never the previous mismatched 50% from another observation's hourly value.
  assert.equal(row.tpm_util, 10);
  assert.equal(row.tpm_util_lower_bound, true);
  assert.equal(row.incomplete, true);
});

test('a lower-bound observation is paired with its own numerator and limit', () => {
  const lowerBound = { ...base, accountId: '222222222222',
    minute_tpm: null, minute_rpm: null, minute_coverage_complete: false,
    hourly_avg_tpm: 600, tpm_limit: 1000, tpm_util_pct: 60, tpm_util_lower_bound: true };
  const [row] = summarizeQuotaRows([base, lowerBound], 'per-model');
  assert.equal(row.tpm_observation.accountId, '222222222222');
  assert.equal(row.tpm_util, 60);
  assert.equal(row.tpm_util_lower_bound, true);
  assert.equal(row.tpm_lim, 1000);
  assert.equal(row.hourly_avg_tpm, 600);
  assert.equal(row.minute_tpm, null);
});

test('a group of exact observations is reported as exact', () => {
  const other = { ...base, accountId: '222222222222', minute_tpm: 400, tpm_util_pct: 40 };
  const [row] = summarizeQuotaRows([base, other], 'per-model');
  assert.equal(row.tpm_util, 40);
  assert.equal(row.tpm_util_lower_bound, false);
});

test('usage, quota and source come from the most utilized observation together', () => {
  const higherCount = { ...base, accountId: '222222222222', minute_tpm: 1000,
    tpm_limit: 20000, tpm_util_pct: 5, quota_src: 'reconstructed', hourly_avg_tpm: 200 };
  const [row] = summarizeQuotaRows([higherCount, base], 'per-model');
  assert.equal(row.minute_tpm, 100);
  assert.equal(row.tpm_lim, 1000);
  assert.equal(row.tpm_util, row.minute_tpm / row.tpm_lim * 100);
  assert.equal(row.quota_src, 'aws_estimate');
  assert.equal(row.tpm_observation.accountId, base.accountId);
  assert.equal(row.hourly_avg_tpm, 50);
});

test('RPM can have a different limiting observation without borrowing its quota', () => {
  const other = { ...base, accountId: '222222222222',
    minute_rpm: 15, rpm_limit: 20, rpm_util_pct: 75 };
  const [row] = summarizeQuotaRows([base, other], 'per-model');
  assert.equal(row.peak_rpm, 15);
  assert.equal(row.rpm_lim, 20);
  assert.equal(row.rpm_util, 75);
  assert.equal(row.rpm_observation.accountId, other.accountId);
});

test('unknown profile routing still suppresses group limits', () => {
  const [row] = summarizeQuotaRows([base, { ...base, routing_unknown: true,
    modelId: 'abcdef123456', tpm_limit: null, rpm_limit: null,
    tpm_util_pct: null, rpm_util_pct: null }], 'per-account');
  assert.equal(row.tpm_util, null);
  assert.equal(row.rpm_util, null);
  assert.equal(row.tpm_lim, null);
  assert.equal(row.rpm_lim, null);
  assert.equal(row.tpm_util_lower_bound, false);
});

test('zero observed usage remains a real zero; missing minute data stays null', () => {
  const [zero] = summarizeQuotaRows([{ ...base, minute_tpm: 0, tpm_util_pct: 0 }], 'per-account');
  assert.equal(zero.minute_tpm, 0);
  assert.equal(zero.measured_minute, true);
  assert.equal(zero.tpm_util, 0);
  const [missing] = summarizeQuotaRows([{ ...base, minute_tpm: null, minute_rpm: null,
    tpm_util_pct: null, rpm_util_pct: null, minute_coverage_complete: false }], 'per-account');
  assert.equal(missing.peak_rpm, null);
  assert.equal(missing.measured_minute, false);
  assert.equal(missing.tpm_util, null);
});

test('with unknown routing, the group shows its busiest usage, not a known-limit row', () => {
  const nova = { ...base, accountId: '333333333333', minute_tpm: null,
    minute_coverage_complete: false, hourly_avg_tpm: 67,
    tpm_limit: 10000, tpm_util_pct: 0.67, tpm_util_lower_bound: true };
  const profile = { ...base, accountId: '333333333333', modelId: 'abcdef123456',
    routing_unknown: true, minute_tpm: null, minute_coverage_complete: false,
    hourly_avg_tpm: 100, tpm_limit: null, tpm_util_pct: null };
  const [row] = summarizeQuotaRows([nova, profile], 'per-account');
  assert.equal(row.hourly_avg_tpm, 100);
  assert.equal(row.tpm_observation.modelId, 'abcdef123456');
  assert.equal(row.tpm_lim, null);
  assert.equal(row.tpm_util, null);
});
