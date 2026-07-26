// Findings panel - the in-dashboard delivery surface for the alerting
// pipeline (009). A bell in the TopNavigation shows the active count; this
// modal lists findings newest/most-severe first with the Phase-2 "prepared
// action" attached to each: a plain-language recommendation, a copyable CLI
// command, and a console deep link. Ack is per-finding and shared (stored on
// the finding row, not per-user) - it means "a human has seen this".
import { useMemo, useState } from 'react';
import {
  Modal, Box, SpaceBetween, Button, StatusIndicator, ExpandableSection,
  Badge, SegmentedControl, Popover,
} from '@cloudscape-design/components';
import { useApi, apiSend, fmtAccount, useAccountNames } from '../api.js';

const SEV_TYPE = { critical: 'error', warning: 'warning', info: 'info' };
const TYPE_LABEL = {
  quota_utilization: 'Quota',
  throttle_rate: 'Throttling',
  cost_jump: 'Cost',
  model_eol: 'Lifecycle',
};

function CopyCli({ cli }) {
  if (!cli) return null;
  return (
    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
      <Box variant="code" fontSize="body-s"
           display="inline-block"
           padding={{ horizontal: 'xs' }}>{cli}</Box>
      <Popover dismissButton={false} position="top" size="small"
               content={<StatusIndicator type="success">Copied</StatusIndicator>}>
        <Button iconName="copy" variant="inline-icon" ariaLabel="Copy CLI command"
          onClick={() => navigator.clipboard?.writeText(cli)} />
      </Popover>
    </SpaceBetween>
  );
}

function FindingCard({ f, onAck }) {
  const act = f.recommended_action || {};
  const acct = f.accountid || f.accountId;
  return (
    <Box padding={{ vertical: 'xs' }}>
      <SpaceBetween size="xxs">
        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
          <StatusIndicator key="sev" type={SEV_TYPE[f.severity] || 'info'}>
            {f.severity}
          </StatusIndicator>
          <Badge key="type" color="grey">{TYPE_LABEL[f.type] || f.type}</Badge>
          {f.state === 'resolved'
            ? <Badge key="res" color="green">resolved</Badge> : null}
          {f.acked && f.state === 'active'
            ? <Badge key="ack" color="blue">acknowledged</Badge> : null}
        </SpaceBetween>
        <Box variant="strong">{f.title}</Box>
        <Box variant="small" color="text-body-secondary">
          {f.detail}
          {acct ? ` · ${fmtAccount(acct)}` : ''}
        </Box>
        {(act.summary || act.cli || act.console_url) ? (
          <ExpandableSection key="action" headerText="Recommended action" variant="footer">
            <SpaceBetween size="xxs">
              {act.summary ? <Box key="sum" variant="small">{act.summary}</Box> : null}
              <CopyCli cli={act.cli} />
              {act.console_url ? (
                <Button key="console" href={act.console_url} target="_blank" iconAlign="right"
                        iconName="external" variant="inline-link">
                  Open in AWS console
                </Button>
              ) : null}
              <Box variant="small" color="text-body-secondary">
                Actions are prepared, never auto-executed - review and run them
                with your own credentials.
              </Box>
            </SpaceBetween>
          </ExpandableSection>
        ) : null}
        {f.state === 'active' ? (
          <Button key="ackbtn" variant="inline-link" onClick={() => onAck(f)}>
            {f.acked ? 'Un-acknowledge' : 'Acknowledge'}
          </Button>
        ) : null}
      </SpaceBetween>
    </Box>
  );
}

export default function FindingsPanel({ visible, onDismiss }) {
  useAccountNames();
  const [scope, setScope] = useState('active');
  const [version, setVersion] = useState(0);
  // `_v` busts api()'s module-level GET cache - ack/un-ack must re-render
  // with the server's truth immediately, not after CACHE_MS expires.
  const feed = useApi('/notifications/findings', { state: scope, _v: version },
                      [scope, version, visible]);

  const findings = feed.data?.findings || [];
  const counts = feed.data?.counts || {};

  const ack = async (f) => {
    try {
      await apiSend('/notifications/findings/ack', {
        method: 'POST',
        body: { finding_id: f.finding_id, acked: !f.acked },
      });
      setVersion(v => v + 1);
    } catch { /* transient - the list refetch will show truth */ }
  };

  const groups = useMemo(() => {
    const active = findings.filter(f => f.state === 'active');
    const resolved = findings.filter(f => f.state !== 'active');
    return { active, resolved };
  }, [findings]);

  return (
    <Modal visible={visible} onDismiss={onDismiss} size="large"
      header={
        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
          <span key="t">Findings</span>
          {counts.critical > 0
            ? <Badge key="c" color="red">{counts.critical} critical</Badge> : null}
          {counts.active > 0
            ? <Badge key="a" color="grey">{counts.active} active</Badge> : null}
        </SpaceBetween>
      }
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>Close</Button>
        </Box>
      }>
      <SpaceBetween size="m">
        <SpaceBetween direction="horizontal" size="m" alignItems="center">
          <SegmentedControl
            selectedId={scope}
            onChange={({ detail }) => setScope(detail.selectedId)}
            options={[
              { id: 'active', text: `Active${counts.active ? ` (${counts.active})` : ''}` },
              { id: 'all', text: 'All (incl. resolved)' },
            ]}
          />
          <Box variant="small" color="text-body-secondary">
            Evaluated after every ingest run. Configure thresholds and delivery
            in Settings → Notifications.
          </Box>
        </SpaceBetween>

        {feed.loading ? <StatusIndicator key="load" type="loading">Loading…</StatusIndicator> : null}

        {!feed.loading && findings.length === 0 ? (
          <Box key="empty" textAlign="center" padding="l" color="text-body-secondary">
            <b>No {scope === 'active' ? 'active ' : ''}findings.</b>
            <Box variant="small" display="block" padding={{ top: 'xs' }}>
              Findings appear when quota utilization, throttle rate, cost, or
              model-lifecycle thresholds are crossed.
            </Box>
          </Box>
        ) : null}

        <div key="list">
          {groups.active.map(f => (
            <FindingCard key={f.finding_id} f={f} onAck={ack} />
          ))}
        </div>
        {scope === 'all' && groups.resolved.length > 0 ? (
          <ExpandableSection headerText={`Resolved (${groups.resolved.length})`}>
            <div>
              {groups.resolved.map(f => (
                <FindingCard key={f.finding_id} f={f} onAck={ack} />
              ))}
            </div>
          </ExpandableSection>
        ) : null}
      </SpaceBetween>
    </Modal>
  );
}
