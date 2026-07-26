// Settings → Notifications (009, Phase 1+2).
//
// Two audiences in one component, mirroring the Settings page's split:
//   - AdminNotificationSettings: thresholds + the SNS delivery channel +
//     "Send test notification". Stack-wide, admin-only.
//   - UserNotificationSubscribe: "email me the alerts" - any signed-in user
//     subscribes their own address to the admin-configured topic (SNS sends
//     the confirmation mail; we never confirm on their behalf).
//
// UX rules: every control saves explicitly (no save-on-keystroke for ARNs),
// state changes flash success/error via the parent's flashbar callback, and
// the test button exists because an alerting pipeline you can't test is an
// alerting pipeline you don't trust.
import { useEffect, useState } from 'react';
import {
  Container, Header, SpaceBetween, Box, Button, FormField, Input, Select,
  Toggle, ColumnLayout, StatusIndicator, ExpandableSection,
} from '@cloudscape-design/components';
import { useApi, apiSend } from '../api.js';
import { InfoLink } from './Common.jsx';

const THRESHOLDS = [
  { key: 'notify_quota_warn_pct', label: 'Quota utilization - warning (%)',
    hint: 'Peak TPM/RPM as % of the applied limit (7-day window).' },
  { key: 'notify_quota_crit_pct', label: 'Quota utilization - critical (%)',
    hint: 'Escalates the same finding to critical.' },
  { key: 'notify_throttle_warn_pct', label: 'Throttle rate - warning (%)',
    hint: '429s as % of requests, last full day, per account/model/region.' },
  { key: 'notify_cost_jump_pct', label: 'Cost jump - warning (%)',
    hint: 'Yesterday vs the prior 7-day daily average, per account.' },
  { key: 'notify_eol_days', label: 'Model lifecycle - days ahead',
    hint: 'Alert when a model with traffic is within N days of Legacy/EOL.' },
];

const SEV_OPTIONS = [
  { value: 'info', label: 'Info and above (everything)' },
  { value: 'warning', label: 'Warning and above (default)' },
  { value: 'critical', label: 'Critical only' },
];

export function AdminNotificationSettings({ onInfo, flash }) {
  const [version, setVersion] = useState(0);
  const cfg = useApi('/notifications/config', {}, [version]);

  const [thresholds, setThresholds] = useState({});
  const [topicArn, setTopicArn] = useState('');
  const [minSev, setMinSev] = useState('warning');
  const [enabled, setEnabled] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (!cfg.data) return;
    setThresholds(cfg.data.thresholds || {});
    const sns = (cfg.data.channels || []).find(c => c.type === 'sns');
    setTopicArn(sns?.config?.topic_arn || '');
    setMinSev(sns?.min_severity || 'warning');
    setEnabled(sns ? !!sns.enabled : true);
    setDirty(false);
  }, [cfg.data]);

  const sns = (cfg.data?.channels || []).find(c => c.type === 'sns');

  const save = async () => {
    setSaving(true);
    try {
      await apiSend('/notifications/config', {
        method: 'PUT',
        body: {
          thresholds,
          sns: { topic_arn: topicArn.trim(), min_severity: minSev, enabled },
        },
      });
      flash('success', 'Notification settings saved.');
      setVersion(v => v + 1);
    } catch (e) {
      flash('error', `Save failed: ${e.message || e}`);
    } finally {
      setSaving(false);
    }
  };

  const sendTest = async () => {
    setTesting(true);
    try {
      await apiSend('/notifications/test', { method: 'POST', body: {} });
      flash('success', 'Test notification published - check the topic subscribers.');
      setVersion(v => v + 1);
    } catch (e) {
      flash('error', `Test failed: ${e.message || e}`);
    } finally {
      setTesting(false);
    }
  };

  return (
    <Container header={<Header variant="h2"
        info={onInfo ? <InfoLink sectionId="notifications" onInfo={onInfo} /> : undefined}
        description="After every ingest run, findings are evaluated (quota utilization, throttle rate, cost jumps, model lifecycle) and state changes are published to the channel below. Notifications carry a prepared action - CLI command and console link - never an automatic change."
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={sendTest} loading={testing}
                    disabled={!sns || !sns.enabled}>Send test notification</Button>
            <Button variant="primary" onClick={save} loading={saving}
                    disabled={!dirty}>Save</Button>
          </SpaceBetween>
        }>
      Notifications
    </Header>}>
      <SpaceBetween size="l">
        {/* Delivery channel ------------------------------------------------ */}
        <SpaceBetween size="s">
          <Box variant="h4">Delivery channel - Amazon SNS</Box>
          <Box variant="small" color="text-body-secondary">
            Point at an SNS topic in this account. Subscribers get email out of
            the box; the same topic fans out to Slack (via AWS Chatbot),
            PagerDuty, Lambda, or any webhook - message payloads include the
            structured findings JSON for machine consumers. Leave empty to
            disable delivery (findings still appear in the bell menu).
          </Box>
          <FormField label="SNS topic ARN" stretch
            description="arn:aws:sns:<region>:<this-account>:<topic>. Create one: aws sns create-topic --name bedrock-ops-lens-alerts">
            <Input value={topicArn} placeholder="arn:aws:sns:us-east-1:123456789012:bedrock-ops-lens-alerts"
              onChange={({ detail }) => { setTopicArn(detail.value); setDirty(true); }} />
          </FormField>
          <ColumnLayout columns={2}>
            <FormField label="Minimum severity to deliver">
              <Select selectedOption={SEV_OPTIONS.find(o => o.value === minSev)}
                options={SEV_OPTIONS}
                onChange={({ detail }) => { setMinSev(detail.selectedOption.value); setDirty(true); }} />
            </FormField>
            <FormField label="Channel enabled">
              <Toggle checked={enabled}
                onChange={({ detail }) => { setEnabled(detail.checked); setDirty(true); }}>
                {enabled ? 'Enabled' : 'Disabled'}
              </Toggle>
            </FormField>
          </ColumnLayout>
          {sns && (
            <Box variant="small">
              Last delivery:{' '}
              {sns.last_delivery_at
                ? <StatusIndicator type={sns.last_delivery_status?.startsWith('ok') ? 'success' : 'error'}>
                    {new Date(sns.last_delivery_at).toLocaleString()} - {sns.last_delivery_status || 'ok'}
                  </StatusIndicator>
                : <StatusIndicator type="pending">none yet</StatusIndicator>}
            </Box>
          )}
        </SpaceBetween>

        {/* Thresholds ------------------------------------------------------ */}
        <ExpandableSection headerText="Alert thresholds" defaultExpanded={false}
          headerDescription="When a metric crosses its threshold, a finding is created; when it recovers, the finding resolves. Both transitions notify.">
          <ColumnLayout columns={2}>
            {THRESHOLDS.map(t => (
              <FormField key={t.key} label={t.label} description={t.hint}>
                <Input type="number" inputMode="decimal"
                  value={String(thresholds[t.key] ?? '')}
                  onChange={({ detail }) => {
                    setThresholds(prev => ({ ...prev, [t.key]: detail.value }));
                    setDirty(true);
                  }} />
              </FormField>
            ))}
          </ColumnLayout>
        </ExpandableSection>
      </SpaceBetween>
    </Container>
  );
}

export function UserNotificationSubscribe({ userEmail, flash }) {
  const cfg = useApi('/notifications/config', {}, []);
  const [subscribing, setSubscribing] = useState(false);
  const [done, setDone] = useState(false);

  const hasChannel = (cfg.data?.channels || []).some(
    c => c.type === 'sns' && c.enabled &&
         (c.config?.topic_arn || c.config?.configured));

  const subscribe = async () => {
    setSubscribing(true);
    try {
      const r = await apiSend('/notifications/subscribe', { method: 'POST', body: {} });
      setDone(true);
      flash('success', r.note || 'Subscription requested - check your inbox.');
    } catch (e) {
      flash('error', `Subscribe failed: ${e.message || e}`);
    } finally {
      setSubscribing(false);
    }
  };

  return (
    <Container header={<Header variant="h2"
        description="Get findings (quota utilization, throttling, cost jumps, model lifecycle) by email when they open or resolve. Delivery is via the alerts topic configured by an administrator; AWS sends a one-time confirmation link to your address.">
      Email notifications
    </Header>}>
      {!hasChannel ? (
        <Box variant="small" color="text-body-secondary">
          No alerts channel is configured yet. Ask a dashboard administrator to
          set the SNS topic under Settings → Notifications.
        </Box>
      ) : done ? (
        <StatusIndicator type="success">
          Confirmation sent to {userEmail} - click the link in that email to
          finish subscribing.
        </StatusIndicator>
      ) : (
        <SpaceBetween direction="horizontal" size="m" alignItems="center">
          <Button onClick={subscribe} loading={subscribing} iconName="envelope">
            Subscribe {userEmail || 'my email'}
          </Button>
          <Box variant="small" color="text-body-secondary">
            You can unsubscribe any time via the link in every email.
          </Box>
        </SpaceBetween>
      )}
    </Container>
  );
}
