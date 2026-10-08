// Keep each peak, limit and provenance together. Independent maxima can pair
// one account's usage with another account's quota or an hourly fallback.
export function quotaUtilization(measuredMinute, hourlyAverage, limit, complete) {
  const observed = [measuredMinute, hourlyAverage].filter(
    v => v != null && Number.isFinite(v) && v >= 0);
  // A measured maximum below a known hourly average cannot cover that window.
  const exact = complete === true && measuredMinute != null
    && Number.isFinite(measuredMinute) && measuredMinute >= 0
    && (hourlyAverage == null || measuredMinute >= hourlyAverage);
  const peak = observed.length ? Math.max(...observed) : null;
  return {
    percentage: peak != null && Number.isFinite(limit) && limit > 0
      ? peak / limit * 100 : null,
    lowerBound: !exact,
  };
}

export function summarizeQuotaRows(rows, scope) {
  const groups = new Map();
  for (const row of rows) {
    const key = scope === 'per-account'
      ? row.accountId : `${row.modelId}|${row.region}|${row.endpoint}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }

  // Rank by utilization only when the group's utilization is comparable. With
  // unknown profile routing in the group, its utilization is suppressed, and a
  // known-limit row would otherwise win on a tiny percentage and display its own
  // usage beside the profile traffic's unknown limit. Rank by usage instead.
  function choose(items, metric, byUtil) {
    const score = row => [
      byUtil ? (row[`${metric}_util_pct`] ?? -1) : 0,
      row[`minute_${metric}`] ?? -1,
      row[`hourly_avg_${metric}`] ?? -1,
    ];
    return items.reduce((best, row) => {
      if (!best) return row;
      const a = score(row), b = score(best);
      for (let i = 0; i < a.length; i++) {
        if (a[i] !== b[i]) return a[i] > b[i] ? row : best;
      }
      return best;
    }, null);
  }

  return [...groups.entries()].map(([key, items]) => {
    const routingUnknown = items.some(r => r.routing_unknown);
    const tpm = choose(items, 'tpm', !routingUnknown);
    const rpm = choose(items, 'rpm', !routingUnknown);
    const incomplete = items.some(r => !r.minute_coverage_complete);
    // The group shows the most utilized observation's OWN percentage, paired
    // with its own peak, limit and provenance. It is a lower bound when that
    // percentage is one, or when any other observation in the group is a lower
    // bound or has no percentage at all — that observation could be higher.
    const tpmKnown = !routingUnknown && tpm.tpm_util_pct != null;
    const rpmKnown = !routingUnknown && rpm.rpm_util_pct != null;
    const tpmLowerBound = tpmKnown
      && items.some(r => r.tpm_util_pct == null || r.tpm_util_lower_bound);
    const rpmLowerBound = rpmKnown
      && items.some(r => r.rpm_util_pct == null || r.rpm_util_lower_bound);
    return {
      key, accountId: tpm.accountId, modelId: tpm.modelId, region: tpm.region,
      endpoint: tpm.endpoint,
      tpm_observation: tpm, rpm_observation: rpm,
      measured_minute: tpm.minute_tpm != null,
      minute_tpm: tpm.minute_tpm, peak_tpm: tpm.minute_tpm,
      peak_rpm: rpm.minute_rpm,
      hourly_avg_tpm: tpm.hourly_avg_tpm,
      burstiness_x: tpm.burstiness_x, quota_src: tpm.quota_src,
      minute_days: tpm.minute_days,
      incomplete, routing_unknown: routingUnknown,
      tpm_lim: routingUnknown ? null : tpm.tpm_limit,
      rpm_lim: routingUnknown ? null : rpm.rpm_limit,
      tpm_util: tpmKnown ? tpm.tpm_util_pct : null,
      rpm_util: rpmKnown ? rpm.rpm_util_pct : null,
      tpm_util_lower_bound: tpmLowerBound,
      rpm_util_lower_bound: rpmLowerBound,
    };
  }).sort((a, b) => (b.tpm_util ?? -1) - (a.tpm_util ?? -1));
}
