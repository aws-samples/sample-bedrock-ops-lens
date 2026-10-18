import assert from 'node:assert/strict';
import test from 'node:test';
import { accountSplitSlices, MAX_ACCOUNT_SLICES } from '../src/accountSplit.js';

const account = (i, requests) =>
  ({ account_type: `${100000000000 + i}`, unique_accounts: 1, total_requests: requests });

test('twelve single accounts fold into six slices plus Other holding the whole remainder', () => {
  const rows = Array.from({ length: 12 }, (_, i) => account(i, (i + 1) * 100));   // 100 … 1200
  const slices = accountSplitSlices(rows);
  assert.equal(slices.length, MAX_ACCOUNT_SLICES + 1);
  assert.deepEqual(slices.slice(0, 6).map(s => s.value), [1200, 1100, 1000, 900, 800, 700]);
  assert.deepEqual(slices.at(-1), { title: 'Other (6 accts)', value: 100 + 200 + 300 + 400 + 500 + 600 });
  assert.equal(slices.reduce((n, s) => n + s.value, 0), rows.reduce((n, r) => n + r.total_requests, 0));
  assert.ok(slices.every(s => !/\(1 accts\)/.test(s.title)));
});

test('seven or fewer groups are shown in full, largest first', () => {
  const rows = Array.from({ length: 7 }, (_, i) => account(i, 10 * (i + 1)));
  const slices = accountSplitSlices(rows);
  assert.equal(slices.length, 7);
  assert.deepEqual(slices.map(s => s.value), [70, 60, 50, 40, 30, 20, 10]);
  assert.ok(slices.every(s => !s.title.startsWith('Other')));
});

test('real account groups keep their account count, formatted by the caller', () => {
  const slices = accountSplitSlices([
    { account_type: 'internal', unique_accounts: 8, total_requests: 500 },
    { account_type: 'external', unique_accounts: 1200, total_requests: 900 },
  ], n => n.toLocaleString('en-US'));
  assert.deepEqual(slices, [
    { title: 'external (1,200 accts)', value: 900 },
    { title: 'internal (8 accts)', value: 500 },
  ]);
});

test('missing data yields no slices', () => {
  assert.deepEqual(accountSplitSlices(null), []);
  assert.deepEqual(accountSplitSlices([]), []);
});
