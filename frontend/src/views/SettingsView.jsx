// Settings — admin-only, gated on the Cognito `bedrock-lens-admins` group.
// In local dev (AUTH_ENABLED=false) every user is treated as admin.
//
// Layout rule: the top of the page is for things an admin can CHANGE
// (pinned tag keys, per-workload attribution). Read-only diagnostics
// (account identity, ingestion freshness, region/account scope) live in a
// collapsed "System information" section at the bottom — useful for support,
// but not clutter above the actual controls.
//
// Strict rule: every configurable section must work end-to-end today. No
// "coming next" placeholders, no stubs.

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ContentLayout, Header, Container, SpaceBetween, ColumnLayout, Box,
  Button, Alert, Flashbar, Multiselect, FormField, Spinner, Toggle,
  ExpandableSection, RadioGroup, StatusIndicator,
} from '@cloudscape-design/components';
import { useApi, fmt, clearCache } from '../api.js';
import { useUser } from '../components/UserContext.jsx';
import { InfoLink } from '../components/Common.jsx';
import { OPTIONAL_TABS, loadOptionalTabs, saveOptionalTabs } from '../prefs.js';

function K({ label, value, color }) {
  return (
    <Box>
      <Box variant="awsui-key-label">{label}</Box>
      <Box variant="h3" color={color}>{value ?? '—'}</Box>
    </Box>
  );
}

export default function SettingsView({ onInfo }) {
  const { user, isAdmin, authEnabled, loading: userLoading } = useUser();

  const status    = useApi('/ingestion-status', {}, []);
  const prefs     = useApi('/preferences', {}, []);
  const accounts  = useApi('/accounts', {}, []);
  const config    = useApi('/system-config', {}, []);
  const attrCfg   = useApi('/attribution/config', {}, []);
  // Available attribute KEYS for the effective source (proxy dims or tag keys).
  const attrKeys  = useApi('/attribution/keys', {}, []);

  // Surfaced keys per source — independent lists so switching source keeps both.
  const [pinnedTag, setPinnedTag] = useState([]);
  const [pinnedProxy, setPinnedProxy] = useState([]);
  const [savingPrefs, setSavingPrefs] = useState(false);
  // Attribution source: 'off' | 'invocation_logs' | 'proxy' (admin's choice).
  const [attrSource, setAttrSource] = useState(null);
  const [savingSource, setSavingSource] = useState(false);
  // [{ id, type: 'success'|'error', content }] — Cloudscape Flashbar items.
  const [flash, setFlash] = useState([]);
  const flashTimerRef = useRef(null);

  // Optional governance/agent tabs — per-user (this browser), off by default.
  // saveOptionalTabs notifies App.jsx so the sidebar updates immediately.
  const [optTabs, setOptTabs] = useState(loadOptionalTabs);
  const toggleTab = (key, checked) => {
    const next = { ...optTabs, [key]: checked };
    setOptTabs(next);
    saveOptionalTabs(next);
  };
  // "Data detected" hints — cheap availability probes so a user who HAS
  // Guardrails/identity/AgentCore data notices these tabs exist even while
  // they're switched off. Each returns fast; empty array/null = no data.
  const byUserProbe  = useApi('/by-user/summary',    { days: 30 }, []);
  const agentsProbe  = useApi('/agents/summary',     { days: 30 }, []);
  const compProbe    = useApi('/compliance/totals',  { days: 30 }, []);
  const hasData = {
    // Workloads: the attribution config already tells us if a source is live.
    workloads:  !!(attrCfg.data && attrCfg.data.effective_source && attrCfg.data.effective_source !== 'off'),
    byUser:     Array.isArray(byUserProbe.data) && byUserProbe.data.length > 0,
    agents:     Array.isArray(agentsProbe.data) && agentsProbe.data.length > 0,
    compliance: !!(compProbe.data && (compProbe.data.invocations || compProbe.data.intervened)),
    // Governance reconciles a config file (registry.yaml) against usage — the
    // registry ships with demo entries, so a "data detected" hint would always
    // fire and mean nothing. No hint; it's a pure opt-in.
    governance: false,
  };
  const TAB_DESC = {
    workloads:  'Per-workload / custom-attribute usage (workload, environment, business unit, …). Needs an attribution source configured below (invocation-log tags or a GenAI proxy) — the tab only appears when the toggle is on AND a source is active.',
    byUser:     'Per-caller attribution from invocation-log identity (by role/team, session, or full principal). Needs model invocation logging enabled.',
    agents:     'AgentCore runtime + MCP gateway observability: invocations, sessions, errors, latency, real billed cost. Needs Bedrock AgentCore in use.',
    compliance: 'Guardrails interventions by policy type, guardrail, and daily trend. Needs Bedrock Guardrails configured.',
    governance: 'Declared AI-app registry (db/registry.yaml) reconciled against observed usage: compliant / drift / undeclared shadow AI.',
  };

  useMemo(() => {
    if (prefs.data?.pinned_tag_keys) {
      setPinnedTag(prefs.data.pinned_tag_keys.map(k => ({ value: k, label: k })));
    }
    if (prefs.data?.pinned_proxy_keys) {
      setPinnedProxy(prefs.data.pinned_proxy_keys.map(k => ({ value: k, label: k })));
    }
  }, [prefs.data]);

  useMemo(() => {
    if (attrCfg.data && attrSource === null) setAttrSource(attrCfg.data.source || 'off');
  }, [attrCfg.data, attrSource]);

  // Auto-dismiss success flashes after 4s. Errors stay until dismissed.
  useEffect(() => () => clearTimeout(flashTimerRef.current), []);

  const showFlash = (type, content) => {
    const id = `flash-${type}`;
    const item = { id, type, content, dismissible: true, onDismiss: () => setFlash([]) };
    setFlash([item]);
    clearTimeout(flashTimerRef.current);
    if (type === 'success') {
      flashTimerRef.current = setTimeout(() => setFlash([]), 4000);
    }
  };

  // Save the surfaced-keys list for the active source (tag or proxy). The PUT
  // is partial, so only the relevant field is sent — the other source's keys
  // are untouched.
  const saveSurfacedKeys = async () => {
    setSavingPrefs(true);
    const isProxy = effectiveSource === 'proxy';
    const sel = isProxy ? pinnedProxy : pinnedTag;
    const field = isProxy ? 'pinned_proxy_keys' : 'pinned_tag_keys';
    try {
      const resp = await fetch('/api/preferences', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [field]: sel.map(p => p.value) }),
      });
      if (!resp.ok) {
        const body = await resp.text();
        throw new Error(`HTTP ${resp.status}: ${body.slice(0, 200)}`);
      }
      clearCache();
      const count = sel.length;
      showFlash('success', count
        ? `Saved. ${count} attribute key${count === 1 ? '' : 's'} will surface in the top-bar filter.`
        : 'Saved. No attribute keys surfaced — top-bar attribute filters are hidden.');
    } catch (err) {
      showFlash('error', `Couldn't save preferences: ${err.message}`);
    } finally {
      setSavingPrefs(false);
    }
  };

  const saveAttrSource = async (next) => {
    setSavingSource(true);
    const prev = attrSource;
    setAttrSource(next);   // optimistic
    try {
      const resp = await fetch('/api/attribution/source', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: next }),
      });
      if (!resp.ok) {
        const body = await resp.text();
        throw new Error(`HTTP ${resp.status}: ${body.slice(0, 200)}`);
      }
      clearCache();
      const label = next === 'proxy' ? 'GenAI proxy events'
        : next === 'invocation_logs' ? 'Bedrock invocation-log tags'
        : 'disabled';
      showFlash('success', `Attribution source set to ${label}. The "Usage · Custom Attributes" tab and top-bar filter now reflect this source.`);
    } catch (err) {
      setAttrSource(prev);
      showFlash('error', `Couldn't update attribution source: ${err.message}`);
    } finally {
      setSavingSource(false);
    }
  };

  if (userLoading) return <Spinner size="large" />;

  if (!isAdmin) {
    return (
      <ContentLayout header={<Header variant="h1">Settings</Header>}>
        <Alert type="warning" header="Admin access required">
          The Settings page is only visible to members of the
          <strong> bedrock-lens-admins </strong> Cognito group. Ask the
          dashboard administrator if you need a configuration change.
        </Alert>
      </ContentLayout>
    );
  }

  const meta = status.data?.meta || {};
  const cfg = config.data || {};
  const avail = attrCfg.data?.available || {};
  const effectiveSource = attrCfg.data?.effective_source || 'off';

  return (
    <ContentLayout
      header={<Header variant="h1" description="Configure how the dashboard behaves. Read-only status is under System information at the bottom.">Settings</Header>}
    >
      <SpaceBetween size="l">
        {flash.length > 0 && <Flashbar items={flash} />}

        {/* ============ CONFIGURABLE ============ */}

        {/* Optional tabs -------------------------------------------------- */}
        {/* The governance/agent views only make sense for customers who run
            AgentCore, Guardrails, or want per-caller / registry governance.
            Off by default so a fresh deploy's nav isn't a wall of empty
            tabs; each user opts in here (stored per browser, like the
            theme). A "Data detected" flag nudges users who DO have data. */}
        <Container header={<Header variant="h2"
            description="Show or hide the governance and agent-observability tabs in the sidebar. Off by default — enable the ones relevant to your environment. Saved as your personal preference in this browser.">
          Optional tabs
        </Header>}>
          <SpaceBetween size="m">
            {Object.entries(OPTIONAL_TABS).map(([key, meta]) => (
              <Toggle
                key={key}
                checked={!!optTabs[key]}
                onChange={({ detail }) => toggleTab(key, detail.checked)}
              >
                <SpaceBetween direction="horizontal" size="xs">
                  <Box variant="span" fontWeight="bold">{meta.label}</Box>
                  {hasData[key] && !optTabs[key] && (
                    <StatusIndicator type="info">Data detected</StatusIndicator>
                  )}
                </SpaceBetween>
                <Box variant="small" color="text-body-secondary" display="block">
                  {TAB_DESC[key]}
                </Box>
              </Toggle>
            ))}
          </SpaceBetween>
        </Container>

        {/* Custom attribute attribution ---------------------------------- */}
        {/* One source powers the "Usage · Custom Attributes" tab + the top-bar
            attribute filter. Two mutually-exclusive sources — the admin's
            choice wins. Both surface the same UX; they differ in reach. */}
        <Container header={<Header variant="h2"
            info={<InfoLink sectionId="workloads-setup" onInfo={onInfo} />}
            description="Break down usage by your own custom attributes (workload, environment, business unit, team, …). Pick ONE source — both drive the same 'Usage · Custom Attributes' tab and the top-bar attribute filter.">
          Custom attribute attribution
        </Header>}>
          <SpaceBetween size="m">
            <RadioGroup
              value={attrSource || 'off'}
              onChange={({ detail }) => saveAttrSource(detail.value)}
              items={[
                {
                  value: 'off', label: 'Off',
                  description: 'No custom-attribute attribution. The tab and top-bar filter are hidden.',
                },
                {
                  value: 'invocation_logs',
                  label: 'Option 1 — Bedrock invocation-log tags',
                  description: `Attributes come from per-request requestMetadata in Bedrock model invocation logs. No proxy needed, but bedrock-runtime only, and it reports volume + tokens (not throttle, latency, or quota).${avail.invocation_logs ? '' : '  ·  No tag data ingested yet.'}`,
                },
                {
                  value: 'proxy',
                  label: 'Option 2 — GenAI proxy events',
                  description: `Attributes come from a proxy that emits one metadata-only event per request to S3. Covers bedrock-runtime + mantle and adds throttle rate, latency, and TPM quota utilization.${avail.proxy ? '' : '  ·  No proxy data ingested yet.'}`,
                },
              ]}
            />
            <Box variant="small" color={effectiveSource === 'off' ? 'text-body-secondary' : 'text-status-success'}>
              {savingSource ? 'Saving…'
                : effectiveSource === 'off'
                  ? 'Currently off — no attribution tab or filter is shown.'
                  : `Active source: ${effectiveSource === 'proxy' ? 'GenAI proxy events' : 'Bedrock invocation-log tags'}${
                      (attrSource && attrSource !== effectiveSource) ? ` (selected "${attrSource}" has no data yet — falling back to what's available)` : ''}.`}
            </Box>

            {/* Which attribute keys surface as top-bar filters — SAME control
                for both sources. Customers may emit many keys (30+); they pick
                the handful worth filtering on. Key list comes from the active
                source (proxy dimension keys or invocation-log tag keys). */}
            {effectiveSource !== 'off' && (() => {
              const isProxy = effectiveSource === 'proxy';
              const keyList = attrKeys.data?.keys || [];
              const sel = isProxy ? pinnedProxy : pinnedTag;
              const setSel = isProxy ? setPinnedProxy : setPinnedTag;
              const sourceLabel = isProxy ? 'proxy dimension' : 'invocation-log tag';
              return (
                <FormField
                  label="Attribute keys to surface in the top-bar filter"
                  description={`Your ${isProxy ? 'proxy' : 'Bedrock requestMetadata'} may carry many ${sourceLabel} keys — pick which become top-bar attribute filters (max 10).`}>
                  <SpaceBetween size="xs">
                    <Multiselect
                      selectedOptions={sel}
                      options={keyList.map(k => ({
                        value: k.key,
                        label: `${k.key} (${fmt(k.total_requests)} req · ${k.distinct_values} value${k.distinct_values === 1 ? '' : 's'})`,
                      }))}
                      onChange={({ detail }) => setSel(detail.selectedOptions)}
                      placeholder={keyList.length ? 'Pick the keys to surface…' : 'No attribute keys ingested yet.'}
                      empty="No attribute keys found."
                      tokenLimit={5}
                      filteringType="auto"
                      disabled={keyList.length === 0}
                    />
                    <Box>
                      <span className="action-orange-wrap">
                        <Button variant="primary" onClick={saveSurfacedKeys} loading={savingPrefs}
                                className="action-orange-btn"
                                disabled={keyList.length === 0}>
                          Save attribute keys
                        </Button>
                      </span>
                    </Box>
                  </SpaceBetween>
                </FormField>
              );
            })()}
          </SpaceBetween>
        </Container>

        {/* ============ READ-ONLY DIAGNOSTICS ============ */}
        <ExpandableSection
          variant="container"
          headerText="System information"
          headerDescription="Read-only status and deploy configuration. Change these by editing config.yaml and re-running the deploy/setup scripts.">
          <SpaceBetween size="l">
            {/* Account / auth */}
            <Box>
              <Header variant="h3">Your account</Header>
              <ColumnLayout columns={3} variant="text-grid">
                <K label="User ID" value={user?.sub} />
                <K label="Email"   value={user?.email || 'local@dev'} />
                <K label="Auth mode" value={authEnabled ? 'Cognito' : 'Disabled (local dev)'}
                   color={authEnabled ? 'text-status-success' : 'text-status-warning'} />
                <K label="Groups"  value={(user?.groups || []).join(', ') || 'none'} />
                <K label="Admin"   value={isAdmin ? 'yes' : 'no'} />
              </ColumnLayout>
            </Box>

            {/* Ingestion freshness */}
            <Box>
              <Header variant="h3" description="When each ingester last refreshed.">Ingestion freshness</Header>
              {status.loading ? <Spinner /> :
                <ColumnLayout columns={4} variant="text-grid">
                  <K label="CloudWatch Metrics" value={meta.last_cw_metrics_refresh?.value?.replace('T', ' ').slice(0, 19)} />
                  <K label="Service Quotas"     value={meta.last_quotas_refresh?.value?.replace('T', ' ').slice(0, 19)} />
                  <K label="Cost Explorer"      value={meta.last_cost_refresh?.value?.replace('T', ' ').slice(0, 19)} />
                  <K label="Invocation logs"    value={meta.last_invocation_logs_refresh?.value?.replace('T', ' ').slice(0, 19) || 'never'} />
                </ColumnLayout>
              }
            </Box>

            {/* Region & account scope */}
            <Box>
              <Header variant="h3" description="Edit config.yaml in the project root and re-run ./setup-pipeline.sh to apply.">Region &amp; account scope</Header>
              {config.loading ? <Spinner /> :
                <ColumnLayout columns={3} variant="text-grid">
                  <K label="Deploy region"     value={cfg.deploy_region} />
                  <K label="Region preset"     value={cfg.monitored_regions_preset} />
                  <K label="Resolved regions"  value={(cfg.resolved_regions || []).join(', ')} />
                  <K label="Account mode"      value={cfg.monitored_accounts_mode} />
                  <K label="Accounts in scope" value={fmt((accounts.data || []).length)} />
                  <K label="Bedrock model (Ops Review)" value={cfg.bedrock_model_id} />
                </ColumnLayout>
              }
            </Box>
          </SpaceBetween>
        </ExpandableSection>
      </SpaceBetween>
    </ContentLayout>
  );
}
