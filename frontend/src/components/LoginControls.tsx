/**
 * Cognito hosted-UI login / logout controls (Requirement 13.3).
 *
 * Renders a "Sign in" button when signed out and the current username plus a
 * "Sign out" button when signed in. Buttons are disabled with an explanatory
 * title when Cognito is not configured via env vars.
 */

import type { UseAuthResult } from "../auth/useAuth";

interface LoginControlsProps {
  auth: UseAuthResult;
}

export function LoginControls({ auth }: LoginControlsProps) {
  const { isAuthenticated, authConfigured, username, login, logout } = auth;

  if (isAuthenticated) {
    return (
      <div className="login-controls" aria-label="account">
        <span className="login-controls__user" title="Signed in">
          {username ?? "Signed in"}
        </span>
        <button type="button" className="btn btn--secondary" onClick={logout}>
          Sign out
        </button>
      </div>
    );
  }

  return (
    <div className="login-controls">
      <button
        type="button"
        className="btn btn--primary"
        onClick={login}
        disabled={!authConfigured}
        title={
          authConfigured
            ? "Sign in with Cognito"
            : "Cognito is not configured (set VITE_COGNITO_DOMAIN and VITE_COGNITO_CLIENT_ID)"
        }
      >
        Sign in
      </button>
    </div>
  );
}
