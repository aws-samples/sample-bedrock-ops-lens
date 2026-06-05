// User context — single source of truth for "who am I" and "what can I do".
//
// In local dev (AUTH_ENABLED=false), the backend's auth middleware injects
// {sub: 'default', email: 'local@dev'} and we treat the user as admin so
// the full UI is reachable without a Cognito setup.
//
// When Cognito is wired up, the backend reads the JWT, sets request.state.user
// with the real claims, and we render Settings only for users in the
// `bedrock-lens-admins` Cognito group.

import { createContext, useContext, useEffect, useState } from 'react';
import { api } from '../api.js';

const UserContext = createContext({
  user: null,
  loading: true,
  isAdmin: false,
  authEnabled: false,
  isAuthenticated: false,
});

const ADMIN_GROUP = 'bedrock-lens-admins';

export function UserProvider({ children }) {
  const [state, setState] = useState({
    user: null,
    loading: true,
    isAdmin: false,
    authEnabled: false,
    isAuthenticated: false,
  });

  useEffect(() => {
    let cancelled = false;
    api('/me', {}, { useCache: false })
      .then(d => {
        if (cancelled) return;
        const groups = Array.isArray(d.groups) ? d.groups : [];
        // Admin gating:
        //   - When auth is OFF (local dev) everyone is admin so Settings
        //     is reachable without Cognito setup.
        //   - When auth is ON, only users in the bedrock-lens-admins group.
        const isAdmin = !d.auth_enabled || groups.includes(ADMIN_GROUP);
        setState({
          user: { sub: d.sub, email: d.email, groups },
          loading: false,
          isAdmin,
          authEnabled: !!d.auth_enabled,
          isAuthenticated: true,
        });
      })
      .catch((err) => {
        if (cancelled) return;
        // Distinguish "not signed in" (401 sentinel from api.js) from a real
        // network error. Either way, expose isAuthenticated=false so AppShell
        // mounts <AuthApp/>; the difference is just the loading state hint.
        const isUnauth = String(err?.message || '').includes('unauthenticated');
        setState({
          user: null,
          loading: false,
          isAdmin: false,
          authEnabled: true,
          isAuthenticated: false,
          networkError: !isUnauth,
        });
      });
    return () => { cancelled = true; };
  }, []);

  return (
    <UserContext.Provider value={state}>
      {children}
    </UserContext.Provider>
  );
}

export function useUser() {
  return useContext(UserContext);
}
