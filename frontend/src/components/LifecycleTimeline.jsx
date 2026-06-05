// Hand-rolled horizontal lifecycle timeline. Pure CSS + absolutely-positioned
// markers, so it prints/exports cleanly. CSS class names match index.css.
//
// One row per model with a legacy date and (optional) eol date. A vertical
// red line marks today. Coloured bands span legacy → eol. Tick labels along
// the bottom axis at 2-month intervals.

import { useMemo } from 'react';

function _toDate(s) {
  if (!s) return null;
  const d = new Date(s + 'T00:00:00Z');
  return Number.isNaN(d.getTime()) ? null : d;
}

const SEV_COLOR = {
  critical: '#d91515',
  warning:  '#b35900',
  info:     '#0972d3',
};

export default function LifecycleTimeline({ alerts = [] }) {
  // Collect all the dates across alerts to determine the axis range.
  const dates = useMemo(() => {
    const all = [];
    for (const a of alerts) {
      [a.legacy_date, a.eol_date, a.extended_access_date].forEach(s => {
        const d = _toDate(s);
        if (d) all.push(d);
      });
    }
    all.push(new Date());
    return all;
  }, [alerts]);

  if (dates.length < 2) return null;

  const minMs = Math.min(...dates.map(d => d.getTime()));
  const maxMs = Math.max(...dates.map(d => d.getTime()));
  // Pad range by 5% on either side so dots aren't flush at the edges.
  const span = Math.max(1, maxMs - minMs);
  const start = minMs - span * 0.05;
  const end = maxMs + span * 0.05;
  const range = end - start;
  const pct = (d) => ((d.getTime() - start) / range) * 100;

  const today = new Date();
  const todayPct = pct(today);

  // Axis ticks at 2-month boundaries.
  const ticks = useMemo(() => {
    const out = [];
    const cursor = new Date(start);
    cursor.setUTCDate(1);
    while (cursor.getTime() <= end) {
      out.push(new Date(cursor));
      cursor.setUTCMonth(cursor.getUTCMonth() + 2);
    }
    return out;
  }, [start, end]);

  return (
    <div className="ops-timeline">
      {alerts.map((a) => {
        const legacy = _toDate(a.legacy_date);
        const eol = _toDate(a.eol_date);
        const ext = _toDate(a.extended_access_date);
        const color = SEV_COLOR[a.severity] || '#5f6b7a';
        const bandLeft = legacy ? pct(legacy) : null;
        const bandRight = eol ? pct(eol) : null;
        return (
          <div className="ops-timeline-row" key={a.modelId}>
            <div className="ops-timeline-label">
              <div className="ops-timeline-modelname">{a.modelId}</div>
              <div className="ops-timeline-modelsev" style={{ color }}>{a.severity}</div>
            </div>
            <div className="ops-timeline-track">
              {(bandLeft !== null && bandRight !== null) ? (
                <div
                  className="ops-timeline-band"
                  style={{
                    left: `${bandLeft}%`,
                    width: `${Math.max(0, bandRight - bandLeft)}%`,
                    background: color,
                  }}
                />
              ) : null}
              {legacy ? (
                <>
                  <div className="ops-timeline-milestone above" style={{ left: `${pct(legacy)}%` }}>
                    <div className="ops-timeline-mlabel">Legacy</div>
                    <div className="ops-timeline-mdate">{a.legacy_date}</div>
                  </div>
                  <div className="ops-timeline-dot" style={{
                    background: color, position: 'absolute',
                    left: `${pct(legacy)}%`, top: 'calc(50% - 4px)', transform: 'translateX(-50%)',
                  }} />
                </>
              ) : null}
              {ext ? (
                <>
                  <div className="ops-timeline-milestone below" style={{ left: `${pct(ext)}%` }}>
                    <div className="ops-timeline-mlabel">Ext. access</div>
                    <div className="ops-timeline-mdate">{a.extended_access_date}</div>
                  </div>
                  <div className="ops-timeline-dot" style={{
                    background: '#5f6b7a', position: 'absolute',
                    left: `${pct(ext)}%`, top: 'calc(50% - 4px)', transform: 'translateX(-50%)',
                  }} />
                </>
              ) : null}
              {eol ? (
                <>
                  <div className="ops-timeline-milestone above" style={{ left: `${pct(eol)}%` }}>
                    <div className="ops-timeline-mlabel">EOL</div>
                    <div className="ops-timeline-mdate">{a.eol_date}</div>
                  </div>
                  <div className="ops-timeline-dot" style={{
                    background: '#d91515', position: 'absolute',
                    left: `${pct(eol)}%`, top: 'calc(50% - 4px)', transform: 'translateX(-50%)',
                  }} />
                </>
              ) : null}
              <div className="ops-timeline-today-line" style={{ left: `${todayPct}%` }} />
              <div className="ops-timeline-today-flag" style={{ left: `${todayPct}%` }}>Today</div>
            </div>
          </div>
        );
      })}
      <div className="ops-timeline-row ops-timeline-axis">
        <div className="ops-timeline-label" />
        <div className="ops-timeline-track">
          {ticks.map((t, i) => (
            <div className="ops-timeline-tick" key={i} style={{ left: `${pct(t)}%` }}>
              {t.toLocaleString(undefined, { month: 'short', year: '2-digit', timeZone: 'UTC' })}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
