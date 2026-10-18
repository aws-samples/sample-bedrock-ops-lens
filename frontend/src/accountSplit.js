// Slices for the Overview "Account split" pie. Kept free of React so the
// fold can be unit-tested directly.
//
// One slice per account type, largest first. Without an account-type mapping
// every account is its own "type" (a bare account ID holding one account), so
// the "(n accts)" suffix is kept for real groups only, and anything past the
// top MAX_ACCOUNT_SLICES folds into "Other" so the pie stays legible. The API
// returns every account group; the fold has to see them all, or "Other" would
// understate both its account count and the chart's total.
export const MAX_ACCOUNT_SLICES = 6;

export function accountSplitSlices(rows, fmt = String) {
  if (!Array.isArray(rows)) return [];
  const sorted = rows
    .map(r => ({ type: String(r.account_type), accounts: Number(r.unique_accounts || 0),
                 value: Number(r.total_requests || 0) }))
    .sort((a, b) => b.value - a.value);
  const slice = r => ({ title: r.accounts > 1 ? `${r.type} (${fmt(r.accounts)} accts)` : r.type,
                        value: r.value });
  if (sorted.length <= MAX_ACCOUNT_SLICES + 1) return sorted.map(slice);
  const tail = sorted.slice(MAX_ACCOUNT_SLICES);
  return [
    ...sorted.slice(0, MAX_ACCOUNT_SLICES).map(slice),
    { title: `Other (${fmt(tail.reduce((n, r) => n + r.accounts, 0))} accts)`,
      value: tail.reduce((n, r) => n + r.value, 0) },
  ];
}
