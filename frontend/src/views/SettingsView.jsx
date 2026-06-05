// Settings — admin-only, gated on the Cognito `bedrock-lens-admins` group.
// In local dev (AUTH_ENABLED=false) every user is treated as admin.
//
// Strict rule for this page: every section reflects something that's
// actually wired up today. No "coming next" placeholders, no stubs for
// features that aren't implemented, no aspirational copy. If you're
// adding a section, the action it describes must work end-to-end now.

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ContentLayout, Header, Container, SpaceBetween, ColumnLayout, Box,
  Button, Alert, Flashbar, Multiselect, FormField, Spinner,
} from '@cloudscape-design/components';
import { useApi, fmt, clearCache } from '../api.js';
import { useUser } from '../components/UserContext.jsx';

function K({ label, value, color }) {
  return (
    <Box>
      <Box variant="awsui-key-label">{label}</Box>
      <Box variant="h3" color={color}>{value ?? '—'}</Box>
    </Box>
  );
}

export default function SettingsView() {
  const { user, isAdmin, authEnabled, loading: userLoading } = useUser();

  const status   = useApi('/ingestion-status', {}, []);
  const tags     = useApi('/tags', {}, []);
  const prefs    = useApi('/preferences', {}, []);
  const accounts = useApi('/accounts', {}, []);
  const config   = useApi('/system-config', {}, []);

  const [pinned, setPinned] = useState([]);
  const [savingPrefs, setSavingPrefs] = useState(false);
  // [{ id, type: 'success'|'error', content }] — Cloudscape Flashbar items.
  const [flash, setFlash] = useState([]);
  const flashTimerRef = useRef(null);

  useMemo(() => {
    if (prefs.data?.pinned_tag_keys) {
      setPinned(prefs.data.pinned_tag_keys.map(k => ({ value: k, label: k })));
    }
  }, [prefs.data]);

  // Auto-dismiss success flashes after 4s. Errors stay until the user dismisses them.
  useEffect(() => () => clearTimeout(flashTimerRef.current), []);

  const showFlash = (type, content) => {
    const id = `flash-${type}`;
    const item = {
      id,
      type,
      content,
      dismissible: true,
      onDismiss: () => setFlash([]),
    };
    setFlash([item]);
    clearTimeout(flashTimerRef.current);
    if (type === 'success') {
      flashTimerRef.current = setTimeout(() => setFlash([]), 4000);
    }
  };

  const savePinnedTags = async () => {
    setSavingPrefs(true);
    try {
      const resp = await fetch('/api/preferences', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pinned_tag_keys: pinned.map(p => p.value) }),
      });
      if (!resp.ok) {
        const body = await resp.text();
        throw new Error(`HTTP ${resp.status}: ${body.slice(0, 200)}`);
      }
      // Invalidate the in-memory cache so TagFilters picks up the new
      // pinned keys the next time the user navigates to a tab.
      clearCache();
      const count = pinned.length;
      showFlash(
        'success',
        count
          ? `Saved. ${count} tag key${count === 1 ? '' : 's'} pinned — they'll appear in the top-bar filter.`
          : 'Saved. No tag keys pinned — top-bar tag filters are hidden.'
      );
    } catch (err) {
      showFlash('error', `Couldn't save preferences: ${err.message}`);
    } finally {
      setSavingPrefs(false);
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

  return (
    <ContentLayout
      header={<Header variant="h1">Settings</Header>}
    >
      <SpaceBetween size="l">
        {/* Inline status — save success / save error toasts. */}
        {flash.length > 0 && <Flashbar items={flash} />}

        {/* Account / auth ------------------------------------------------- */}
        <Container header={<Header variant="h2">Your account</Header>}>
          <ColumnLayout columns={3} variant="text-grid">
            <K label="User ID" value={user?.sub} />
            <K label="Email"   value={user?.email || 'local@dev'} />
            <K label="Auth mode" value={authEnabled ? 'Cognito' : 'Disabled (local dev)'}
               color={authEnabled ? 'text-status-success' : 'text-status-warning'} />
            <K label="Groups"  value={(user?.groups || []).join(', ') || 'none'} />
            <K label="Admin"   value={isAdmin ? 'yes' : 'no'} />
          </ColumnLayout>
        </Container>

        {/* Ingestion freshness ------------------------------------------- */}
        <Container header={<Header variant="h2"
            description="When each ingester last refreshed.">
          Ingestion freshness
        </Header>}>
          {status.loading ? <Spinner /> :
            <ColumnLayout columns={3} variant="text-grid">
              <K label="CloudWatch Metrics" value={meta.last_cw_metrics_refresh?.value?.replace('T', ' ').slice(0, 19)} />
              <K label="Service Quotas"     value={meta.last_quotas_refresh?.value?.replace('T', ' ').slice(0, 19)} />
              <K label="Cost Explorer"      value={meta.last_cost_refresh?.value?.replace('T', ' ').slice(0, 19)} />
              <K label="Invocation logs"    value={meta.last_invocation_logs_refresh?.value?.replace('T', ' ').slice(0, 19) || 'never'} />
            </ColumnLayout>
          }
        </Container>

        {/* Region & account scope ---------------------------------------- */}
        <Container header={<Header variant="h2"
            description="Edit config.yaml in the project root and re-run ./setup-pipeline.sh to apply.">
          Region & account scope
        </Header>}>
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
        </Container>

        {/* Pinned tag keys ----------------------------------------------- */}
        <Container header={<Header variant="h2"
            description="Pin tag keys to surface them in the top-bar filter. Tags come from Bedrock per-request metadata via invocation logs.">
          Pinned tag keys
        </Header>}>
          <SpaceBetween size="s">
            <FormField label="Tag keys to pin">
              <Multiselect
                selectedOptions={pinned}
                options={(tags.data || []).map(t => ({
                  value: t.tag_key,
                  label: `${t.tag_key} (${fmt(t.total_requests_30d)} req)`,
                }))}
                onChange={({ detail }) => setPinned(detail.selectedOptions)}
                placeholder={(tags.data || []).length ? 'Pick the keys to pin...' : 'No tags ingested yet.'}
                empty="No tags found."
                tokenLimit={5}
                disabled={(tags.data || []).length === 0}
              />
            </FormField>
            <Box>
              <span className="action-orange-wrap">
                <Button variant="primary" onClick={savePinnedTags} loading={savingPrefs}
                        className="action-orange-btn"
                        disabled={(tags.data || []).length === 0}>
                  Save preferences
                </Button>
              </span>
            </Box>
          </SpaceBetween>
        </Container>
      </SpaceBetween>
    </ContentLayout>
  );
}
