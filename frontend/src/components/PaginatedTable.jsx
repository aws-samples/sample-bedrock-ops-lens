// Generic Cloudscape Table wrapper with TextFilter and pagination.
//
// Sorting deliberately disabled here. Cloudscape's `useCollection.sorting`
// option crashes whenever its internal sortingColumn state ends up pointing
// at a column without `sortingField` or `sortingComparator` — which is most
// of our columns, and the first time it bites is on initial data arrival.
// Server-side responses are already pre-sorted by the relevant metric
// (DESC by request count, throttle %, etc.) so client-side sort would
// only re-sort already-sorted data. Cheap to give up, expensive to keep.
//
// Row-detail expand: when `trackBy` and `renderRowDetail` are both supplied,
// the first column gets a chevron button. Toggling re-renders the page with
// a synthetic detail row inserted after the expanded item. The expanded
// state lives in a ref so column refs stay stable across renders.
import { useMemo, useRef, useState } from 'react';
import {
  Table, Box, TextFilter, Pagination, CollectionPreferences, Button,
  SpaceBetween,
} from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';

// --- CSV export helpers ----------------------------------------------------
// Turn the table's column set + row items into a CSV and trigger a browser
// download. Exports RAW row values (not rendered React cells) using each
// column's `exportValue` if provided, else a best-effort read off the item
// by column id (and its lowercased form, since Postgres returns lowercased
// keys). Keeps downloads meaningful without every caller wiring it up.
function csvEscape(v) {
  if (v === null || v === undefined) return '';
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// Recursively pull the visible text out of whatever a column's `cell(item)`
// returns — string/number, or a React element tree (Box, StatusIndicator,
// <code>, nested children). This is the safety net so a column with a
// React-rendering `cell` and no explicit `exportValue` still exports its
// on-screen text instead of a blank. Skips SVG/icon nodes (no useful text).
function nodeToText(node) {
  if (node === null || node === undefined || node === false || node === true) return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(nodeToText).join(' ');
  if (typeof node === 'object' && node.props) {
    // Cloudscape StatusIndicator/Box/etc. carry their label in children.
    return nodeToText(node.props.children);
  }
  return '';
}

function cellExportValue(col, item) {
  // 1. Explicit exportValue always wins.
  if (typeof col.exportValue === 'function') return col.exportValue(item);
  // 2. Direct field match on the column id (exact or lowercased — Postgres
  //    returns lowercased keys).
  if (col.id && item[col.id] !== undefined) return item[col.id];
  const lc = col.id && col.id.toLowerCase ? col.id.toLowerCase() : null;
  if (lc && item[lc] !== undefined) return item[lc];
  // 3. Fallback: render the cell and extract its visible text. Covers columns
  //    whose id doesn't match a field and that render React elements — without
  //    this they exported blank. Clean the '—' placeholder to empty.
  if (typeof col.cell === 'function') {
    try {
      const txt = nodeToText(col.cell(item)).replace(/\s+/g, ' ').trim();
      return txt === '—' ? '' : txt;
    } catch { /* fall through */ }
  }
  return '';
}

function downloadCsv(filename, columns, rows) {
  const header = columns.map(c => csvEscape(c.header ?? c.id)).join(',');
  const body = rows
    .filter(r => !r.__detail__)
    .map(r => columns.map(c => csvEscape(cellExportValue(c, r))).join(','))
    .join('\n');
  const blob = new Blob([`${header}\n${body}`], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export default function PaginatedTable({
  items = [],
  columnDefinitions = [],
  header,
  footer,
  pageSize: initialPageSize = 10,
  trackBy,
  renderRowDetail,
  variant = 'embedded',
  // Accepted for API compat with reference; ignored — sorting is always off.
  // eslint-disable-next-line no-unused-vars
  sortingDisabled,
  empty = 'No data',
  searchPlaceholder = 'Search…',
  // CSV download. Enabled by default; pass `downloadable={false}` to hide.
  // `downloadFileName` names the file (defaults to bedrock-ops-lens-export.csv).
  downloadable = true,
  downloadFileName = 'bedrock-ops-lens-export.csv',
}) {
  const [pageSize, setPageSize] = useState(initialPageSize);
  const [visible, setVisible] = useState(columnDefinitions.map(c => c.id));
  const [expandedTick, setExpandedTick] = useState(0);
  const expandedRef = useRef(new Set());

  const toggle = (id) => {
    if (expandedRef.current.has(id)) expandedRef.current.delete(id);
    else expandedRef.current.add(id);
    setExpandedTick(t => t + 1); // force re-render; column refs don't change
  };

  // Build the column array once per (columnDefinitions, renderRowDetail) — NOT
  // on every expanded toggle. Cells read the live `expandedRef` directly.
  const cols = useMemo(() => {
    if (!renderRowDetail || !trackBy) return columnDefinitions;
    const [first, ...rest] = columnDefinitions;
    const wrappedFirst = {
      ...first,
      cell: (item) => {
        if (item.__detail__) return null;
        const open = expandedRef.current.has(item[trackBy]);
        return (
          <Box>
            <Button
              variant="inline-icon"
              ariaLabel={open ? 'Collapse row' : 'Expand row'}
              iconName={open ? 'caret-down-filled' : 'caret-right-filled'}
              onClick={() => toggle(item[trackBy])}
            />
            <Box variant="span" margin={{ left: 'xs' }}>{first.cell(item)}</Box>
          </Box>
        );
      },
    };
    return [wrappedFirst, ...rest];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [columnDefinitions, renderRowDetail, trackBy]);

  // Synthetic detail rows are inserted post-pagination so each expanded
  // parent has its detail directly underneath, and the chevron toggle
  // doesn't push the parent off the page.
  const colsForDetail = useMemo(() => {
    if (!renderRowDetail) return cols;
    return cols.map((c, idx) => ({
      ...c,
      cell: (item) => {
        if (item.__detail__) {
          // Render the detail content in the first column only; other cells
          // collapse to nothing. This works in practice because Cloudscape
          // renders cells side-by-side in the same row.
          return idx === 0 ? <Box padding="s">{renderRowDetail(item)}</Box> : null;
        }
        return c.cell(item);
      },
    }));
  }, [cols, renderRowDetail]);

  // Sorting intentionally NOT in opts — see file header comment.
  const { items: pagedItems, collectionProps, filterProps, paginationProps, filteredItemsCount } =
    useCollection(items, {
      filtering: {
        empty: <Box textAlign="center" color="inherit"><b>{empty}</b></Box>,
        noMatch: <Box textAlign="center" color="inherit"><b>No matches</b></Box>,
      },
      pagination: { pageSize },
    });

  // Interleave detail rows (post-pagination so we don't blow the page count).
  const displayItems = useMemo(() => {
    if (!renderRowDetail || !trackBy) return pagedItems;
    const out = [];
    for (const item of pagedItems) {
      out.push(item);
      if (expandedRef.current.has(item[trackBy])) {
        out.push({ ...item, __detail__: true, __key__: `__detail__${item[trackBy]}` });
      }
    }
    return out;
    // expandedTick included so the list re-renders when toggle() fires.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pagedItems, renderRowDetail, trackBy, expandedTick]);

  const visibleCols = colsForDetail.filter(c => visible.includes(c.id));

  // trackBy MUST return a unique non-undefined value for every row, otherwise
  // React/Cloudscape collapses rows with duplicate keys. We only pass a
  // custom trackBy when we have something to anchor to (the trackBy prop or
  // a synthetic __key__ on detail rows). Otherwise we omit the prop entirely
  // so the table uses its built-in index-based keying.
  const tableTrackBy = (renderRowDetail || trackBy)
    ? ((item) => item.__key__ || (trackBy ? item[trackBy] : JSON.stringify(item)))
    : undefined;

  return (
    <Table
      {...collectionProps}
      variant={variant}
      header={header}
      columnDefinitions={visibleCols}
      items={displayItems}
      sortingDisabled
      {...(tableTrackBy ? { trackBy: tableTrackBy } : {})}
      filter={
        <SpaceBetween direction="horizontal" size="xs">
          <div style={{ minWidth: 220, flexGrow: 1 }}>
            <TextFilter
              {...filterProps}
              countText={`${filteredItemsCount} matches`}
              filteringPlaceholder={searchPlaceholder}
            />
          </div>
          {downloadable && (
            <Button
              variant="icon"
              iconName="download"
              ariaLabel="Download table as CSV"
              disabled={!items.length}
              onClick={() => downloadCsv(downloadFileName, visibleCols, items)}
            />
          )}
        </SpaceBetween>
      }
      pagination={<Pagination {...paginationProps} />}
      preferences={
        <CollectionPreferences
          title="Preferences"
          confirmLabel="Confirm"
          cancelLabel="Cancel"
          preferences={{ pageSize, visibleContent: visible }}
          onConfirm={({ detail }) => {
            setPageSize(detail.pageSize || initialPageSize);
            if (detail.visibleContent) setVisible([...detail.visibleContent]);
          }}
          pageSizePreference={{
            title: 'Page size',
            options: [
              { value: 10,  label: '10 rows' },
              { value: 25,  label: '25 rows' },
              { value: 50,  label: '50 rows' },
              { value: 100, label: '100 rows' },
            ],
          }}
          visibleContentPreference={{
            title: 'Columns',
            options: [{
              label: 'Columns',
              options: columnDefinitions.map(c => ({ id: c.id, label: c.header })),
            }],
          }}
        />
      }
      empty={<Box textAlign="center" color="inherit"><b>{empty}</b></Box>}
      footer={footer}
    />
  );
}
