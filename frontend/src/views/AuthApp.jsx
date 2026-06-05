// Custom Cloudscape sign-in / sign-up / verify-email / forgot-password.
//
// Replaces Cognito's Hosted UI. Same User Pool, same Pre-Sign-Up Lambda
// gate — just a much nicer UI integrated with the rest of the dashboard.
//
// Backend endpoints used (see backend/app/auth.py):
//   POST /api/auth/signin            { email, password } → sets session cookie
//   POST /api/auth/signup            { email, password } → triggers email code
//   POST /api/auth/confirm           { email, code }     → confirms account
//   POST /api/auth/resend-code       { email }
//   POST /api/auth/forgot-password   { email }           → emails reset code
//   POST /api/auth/reset-password    { email, code, password }
//
// Cookie is HttpOnly + SameSite=Lax. After /signin succeeds we reload the
// page so the SPA re-fetches /me with the cookie attached and renders the
// real dashboard.

import { useState } from 'react';
import {
  Box, Button, Container, Form, FormField, Input, Header,
  SpaceBetween, Alert, Link,
} from '@cloudscape-design/components';

const SCREENS = {
  SIGNIN:       'signin',
  SIGNUP:       'signup',
  VERIFY:       'verify',
  FORGOT:       'forgot',
  RESET:        'reset',
  NEW_PASSWORD: 'new_password',  // FORCE_CHANGE_PASSWORD on first sign-in
};

async function postAuth(path, body) {
  const r = await fetch(`/api/auth/${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await r.json(); } catch { /* empty body on success */ }
  return { ok: r.ok, status: r.status, ...data };
}

// Page chrome: centred card, Bedrock-Lens logo at top, dark-bg friendly.
function AuthCard({ title, description, error, children, footer }) {
  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '24px',
    }}>
      <div style={{ width: '100%', maxWidth: 440 }}>
        <Box textAlign="center" margin={{ bottom: 'l' }}>
          <h1 style={{ fontSize: 22, margin: 0, fontWeight: 600 }}>Bedrock Ops Lens</h1>
        </Box>
        <Container header={<Header variant="h2" description={description}>{title}</Header>}>
          <SpaceBetween size="m">
            {error && <Alert type="error" header="Couldn't continue">{error}</Alert>}
            {children}
          </SpaceBetween>
        </Container>
        {footer && (
          <Box textAlign="center" margin={{ top: 'm' }}>
            {footer}
          </Box>
        )}
      </div>
    </div>
  );
}

function SignIn({ onSuccess, goSignUp, goForgot, prefill }) {
  const [email, setEmail] = useState(prefill?.email || '');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async () => {
    setError(''); setBusy(true);
    const r = await postAuth('signin', { email, password });
    setBusy(false);
    if (r.ok) { onSuccess(); return; }
    if (r.challenge === 'NEW_PASSWORD_REQUIRED') {
      // Admin-created user with a temporary password — Cognito requires a
      // password change before the first session is issued. Hand the
      // session token to the new-password screen.
      onSuccess({ goNewPassword: { email, session: r.session } });
      return;
    }
    if (r.error === 'UserNotConfirmedException') {
      // Account exists but email never verified — push to verify screen
      // pre-filled with the email they entered.
      onSuccess({ goVerify: { email } });
      return;
    }
    setError(r.message || 'Sign-in failed.');
  };

  return (
    <AuthCard
      title="Sign in"
      description="Use your work email."
      error={error}
      footer={<>Need an account? <Link onFollow={(e) => { e?.preventDefault?.(); goSignUp(); }}>Sign up</Link></>}
    >
      <form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <SpaceBetween size="m">
          <FormField label="Email">
            <Input
              type="email"
              value={email}
              autoFocus
              onChange={({ detail }) => setEmail(detail.value)}
              placeholder="you@example.com"
            />
          </FormField>
          <FormField label="Password" secondaryControl={
            <Link onFollow={(e) => { e?.preventDefault?.(); goForgot(email); }}>Forgot?</Link>
          }>
            <Input
              type="password"
              value={password}
              onChange={({ detail }) => setPassword(detail.value)}
              placeholder="Your password"
            />
          </FormField>
          <Button variant="primary" loading={busy} formAction="submit" fullWidth>
            Sign in
          </Button>
        </SpaceBetween>
      </form>
    </AuthCard>
  );
}

function SignUp({ goSignIn, onAwaitingVerify }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async () => {
    setError(''); setBusy(true);
    const r = await postAuth('signup', { email, password });
    setBusy(false);
    if (r.ok) { onAwaitingVerify({ email }); return; }
    setError(r.message || 'Sign-up failed.');
  };

  return (
    <AuthCard
      title="Create account"
      description="We'll email you a verification code."
      error={error}
      footer={<>Already have an account? <Link onFollow={(e) => { e?.preventDefault?.(); goSignIn(); }}>Sign in</Link></>}
    >
      <form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <SpaceBetween size="m">
          <FormField label="Email">
            <Input
              type="email"
              value={email}
              autoFocus
              onChange={({ detail }) => setEmail(detail.value)}
              placeholder="you@example.com"
            />
          </FormField>
          <FormField
            label="Password"
            description="At least 12 characters with upper- and lower-case letters, a number, and a symbol."
          >
            <Input
              type="password"
              value={password}
              onChange={({ detail }) => setPassword(detail.value)}
              placeholder="Choose a password"
            />
          </FormField>
          <Button variant="primary" loading={busy} formAction="submit" fullWidth>
            Create account
          </Button>
        </SpaceBetween>
      </form>
    </AuthCard>
  );
}

function Verify({ email: initialEmail, goSignIn }) {
  const [email, setEmail] = useState(initialEmail || '');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [resending, setResending] = useState(false);
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');

  const submit = async () => {
    setError(''); setInfo(''); setBusy(true);
    const r = await postAuth('confirm', { email, code });
    setBusy(false);
    if (r.ok) {
      setInfo('Email confirmed — sign in below.');
      setTimeout(() => goSignIn(email), 1200);
      return;
    }
    setError(r.message || 'Confirmation failed.');
  };

  const resend = async () => {
    setError(''); setInfo(''); setResending(true);
    const r = await postAuth('resend-code', { email });
    setResending(false);
    if (r.ok) setInfo('A new code is on its way.');
    else setError(r.message || 'Could not resend.');
  };

  return (
    <AuthCard
      title="Verify your email"
      description={`Enter the 6-digit code we sent to ${email || 'your email'}.`}
      error={error}
      footer={<Link onFollow={(e) => { e?.preventDefault?.(); goSignIn(email); }}>Back to sign in</Link>}
    >
      {info && <Alert type="success">{info}</Alert>}
      <form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <SpaceBetween size="m">
          <FormField label="Email">
            <Input
              type="email"
              value={email}
              onChange={({ detail }) => setEmail(detail.value)}
            />
          </FormField>
          <FormField label="Verification code" secondaryControl={
            <Button onClick={resend} loading={resending} disabled={!email}>
              Resend
            </Button>
          }>
            <Input
              value={code}
              autoFocus
              onChange={({ detail }) => setCode(detail.value)}
              placeholder="6-digit code"
            />
          </FormField>
          <Button variant="primary" loading={busy} formAction="submit" fullWidth>
            Confirm
          </Button>
        </SpaceBetween>
      </form>
    </AuthCard>
  );
}

function Forgot({ email: initialEmail, goSignIn, onCodeSent }) {
  const [email, setEmail] = useState(initialEmail || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const submit = async () => {
    setError(''); setBusy(true);
    const r = await postAuth('forgot-password', { email });
    setBusy(false);
    if (r.ok) { onCodeSent({ email }); return; }
    setError(r.message || 'Could not start reset.');
  };

  return (
    <AuthCard
      title="Reset password"
      description="We'll email you a code to set a new password."
      error={error}
      footer={<Link onFollow={(e) => { e?.preventDefault?.(); goSignIn(email); }}>Back to sign in</Link>}
    >
      <form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <SpaceBetween size="m">
          <FormField label="Email">
            <Input
              type="email"
              value={email}
              autoFocus
              onChange={({ detail }) => setEmail(detail.value)}
              placeholder="you@example.com"
            />
          </FormField>
          <Button variant="primary" loading={busy} formAction="submit" fullWidth>
            Send reset code
          </Button>
        </SpaceBetween>
      </form>
    </AuthCard>
  );
}

function Reset({ email: initialEmail, goSignIn }) {
  const [email, setEmail] = useState(initialEmail || '');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');

  const submit = async () => {
    setError(''); setInfo(''); setBusy(true);
    const r = await postAuth('reset-password', { email, code, password });
    setBusy(false);
    if (r.ok) {
      setInfo('Password reset — sign in below.');
      setTimeout(() => goSignIn(email), 1200);
      return;
    }
    setError(r.message || 'Reset failed.');
  };

  return (
    <AuthCard
      title="Set new password"
      description={`Enter the code we sent to ${email || 'your email'} and choose a new password.`}
      error={error}
      footer={<Link onFollow={(e) => { e?.preventDefault?.(); goSignIn(email); }}>Back to sign in</Link>}
    >
      {info && <Alert type="success">{info}</Alert>}
      <form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <SpaceBetween size="m">
          <FormField label="Email">
            <Input
              type="email"
              value={email}
              onChange={({ detail }) => setEmail(detail.value)}
            />
          </FormField>
          <FormField label="Verification code">
            <Input
              value={code}
              autoFocus
              onChange={({ detail }) => setCode(detail.value)}
              placeholder="6-digit code"
            />
          </FormField>
          <FormField
            label="New password"
            description="At least 12 characters with upper- and lower-case letters, a number, and a symbol."
          >
            <Input
              type="password"
              value={password}
              onChange={({ detail }) => setPassword(detail.value)}
              placeholder="New password"
            />
          </FormField>
          <Button variant="primary" loading={busy} formAction="submit" fullWidth>
            Reset password
          </Button>
        </SpaceBetween>
      </form>
    </AuthCard>
  );
}

// FORCE_CHANGE_PASSWORD screen. Triggered when an admin-created user signs in
// with their temporary password. Cognito returns a Session token (NOT a JWT)
// which we forward to /api/auth/set-new-password along with the chosen new
// password. On success, the backend sets the same session cookie /signin would.
function NewPassword({ email, session, onSuccess, goSignIn }) {
  const [password, setPassword]   = useState('');
  const [confirm, setConfirm]     = useState('');
  const [busy, setBusy]           = useState(false);
  const [error, setError]         = useState('');

  const submit = async () => {
    setError('');
    if (password !== confirm) { setError("Passwords don't match."); return; }
    if (password.length < 12)  { setError('Password must be at least 12 characters.'); return; }
    setBusy(true);
    const r = await postAuth('set-new-password', { email, session, password });
    setBusy(false);
    if (r.ok) { onSuccess(); return; }
    setError(r.message || 'Could not set new password.');
  };

  return (
    <AuthCard
      title="Set a new password"
      description={`First sign-in for ${email}. Choose a new password to continue.`}
      error={error}
      footer={<Link onFollow={(e) => { e?.preventDefault?.(); goSignIn(email); }}>Back to sign in</Link>}
    >
      <form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <SpaceBetween size="m">
          <FormField
            label="New password"
            description="At least 12 characters with upper- and lower-case letters, a number, and a symbol."
          >
            <Input
              type="password"
              value={password}
              autoFocus
              onChange={({ detail }) => setPassword(detail.value)}
              placeholder="New password"
            />
          </FormField>
          <FormField label="Confirm new password">
            <Input
              type="password"
              value={confirm}
              onChange={({ detail }) => setConfirm(detail.value)}
              placeholder="Confirm new password"
            />
          </FormField>
          <Button variant="primary" loading={busy} formAction="submit" fullWidth>
            Set password and sign in
          </Button>
        </SpaceBetween>
      </form>
    </AuthCard>
  );
}

// Top-level state machine. The screens call back here to navigate.
export default function AuthApp() {
  const [screen, setScreen] = useState(SCREENS.SIGNIN);
  const [carry, setCarry] = useState({});   // { email, session, ... } passed between screens

  const goSignIn      = (email) => { setCarry({ email });               setScreen(SCREENS.SIGNIN); };
  const goSignUp      = ()      => { setCarry({});                      setScreen(SCREENS.SIGNUP); };
  const goVerify      = ({ email }) => { setCarry({ email });           setScreen(SCREENS.VERIFY); };
  const goForgot      = (email) => { setCarry({ email });               setScreen(SCREENS.FORGOT); };
  const goReset       = ({ email }) => { setCarry({ email });           setScreen(SCREENS.RESET); };
  const goNewPassword = ({ email, session }) => {
    setCarry({ email, session });
    setScreen(SCREENS.NEW_PASSWORD);
  };

  // After successful sign-in the cookie is set by the backend. Easiest way
  // to surface the authenticated state to the rest of the SPA is a hard
  // reload — the UserContext re-fetches /me, sees the new cookie's claims,
  // and renders the dashboard. Avoids prop-drilling auth state.
  const onSignInSuccess = (next) => {
    if (next?.goVerify) { goVerify(next.goVerify); return; }
    if (next?.goNewPassword) { goNewPassword(next.goNewPassword); return; }
    window.location.reload();
  };

  switch (screen) {
    case SCREENS.SIGNUP: return <SignUp goSignIn={() => goSignIn(carry.email)} onAwaitingVerify={goVerify} />;
    case SCREENS.VERIFY: return <Verify email={carry.email} goSignIn={() => goSignIn(carry.email)} />;
    case SCREENS.FORGOT: return <Forgot email={carry.email} goSignIn={() => goSignIn(carry.email)} onCodeSent={goReset} />;
    case SCREENS.RESET:  return <Reset  email={carry.email} goSignIn={() => goSignIn(carry.email)} />;
    case SCREENS.NEW_PASSWORD:
      return (
        <NewPassword
          email={carry.email}
          session={carry.session}
          onSuccess={() => window.location.reload()}
          goSignIn={() => goSignIn(carry.email)}
        />
      );
    case SCREENS.SIGNIN:
    default:
      return (
        <SignIn
          prefill={{ email: carry.email }}
          onSuccess={onSignInSuccess}
          goSignUp={goSignUp}
          goForgot={(email) => goForgot(email)}
        />
      );
  }
}
