/**
 * Cognito hosted-UI authentication helpers (Requirement 13.3).
 *
 * Login/logout are performed by redirecting the browser to the Cognito
 * hosted UI authorize / logout endpoints. We use the OAuth2 **implicit**
 * flow (`response_type=token`) so the SPA can obtain tokens without a
 * client secret or a token-exchange backend — appropriate for a public
 * SPA client in a classroom demo.
 *
 * Tokens are parsed from the URL fragment on return, stored in
 * `sessionStorage` (so they clear when the tab closes), and attached as a
 * bearer token on `POST /chat` requests. Logout clears the stored tokens
 * and redirects to the Cognito logout endpoint.
 *
 * No secrets live here: only the public hosted-UI domain, client id, and
 * redirect URIs (all from env config) are used.
 */

import { config, isAuthConfigured, type AppConfig } from "../config";
import type { AuthSession } from "../types";

const STORAGE_KEY = "pharmapulse.auth.session";
const STATE_KEY = "pharmapulse.auth.state";

/** Decode a base64url JWT payload into a claims object (no verification). */
function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const payload = token.split(".")[1];
    if (!payload) return null;
    const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized.padEnd(
      normalized.length + ((4 - (normalized.length % 4)) % 4),
      "="
    );
    const json = atob(padded);
    return JSON.parse(json) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function randomState(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Build the Cognito hosted-UI authorize URL and redirect the browser. */
export function login(cfg: AppConfig = config): void {
  if (!isAuthConfigured(cfg)) {
    throw new Error(
      "Cognito is not configured. Set VITE_COGNITO_DOMAIN and VITE_COGNITO_CLIENT_ID."
    );
  }
  const state = randomState();
  sessionStorage.setItem(STATE_KEY, state);

  const params = new URLSearchParams({
    client_id: cfg.cognitoClientId,
    response_type: "token",
    scope: cfg.cognitoScope,
    redirect_uri: cfg.cognitoRedirectUri,
    state,
  });
  window.location.assign(`${cfg.cognitoDomain}/oauth2/authorize?${params.toString()}`);
}

/** Clear local tokens and redirect to the Cognito hosted-UI logout endpoint. */
export function logout(cfg: AppConfig = config): void {
  clearSession();
  if (!isAuthConfigured(cfg)) {
    // Nothing to redirect to; just clear local state.
    return;
  }
  const params = new URLSearchParams({
    client_id: cfg.cognitoClientId,
    logout_uri: cfg.cognitoLogoutUri,
  });
  window.location.assign(`${cfg.cognitoDomain}/logout?${params.toString()}`);
}

/** Persist a session to sessionStorage. */
function storeSession(session: AuthSession): void {
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
}

/** Remove any stored session. */
export function clearSession(): void {
  sessionStorage.removeItem(STORAGE_KEY);
  sessionStorage.removeItem(STATE_KEY);
}

/** Return the current stored session, or null if absent/expired. */
export function getSession(): AuthSession | null {
  const raw = sessionStorage.getItem(STORAGE_KEY);
  if (!raw) return null;
  try {
    const session = JSON.parse(raw) as AuthSession;
    if (session.expiresAt && session.expiresAt * 1000 <= Date.now()) {
      clearSession();
      return null;
    }
    return session;
  } catch {
    clearSession();
    return null;
  }
}

/**
 * Parse tokens from the URL fragment after a hosted-UI redirect.
 *
 * On success: stores the session, clears the fragment from the address
 * bar, and returns the session. On no-fragment / mismatch: returns the
 * existing stored session (or null). This is safe to call once on app load.
 */
export function completeLoginFromRedirect(): AuthSession | null {
  const hash = window.location.hash.startsWith("#")
    ? window.location.hash.slice(1)
    : window.location.hash;

  if (!hash) {
    return getSession();
  }

  const params = new URLSearchParams(hash);
  const accessToken = params.get("access_token");
  const idToken = params.get("id_token");
  if (!accessToken && !idToken) {
    return getSession();
  }

  // Validate state to mitigate CSRF on the redirect.
  const expectedState = sessionStorage.getItem(STATE_KEY);
  const returnedState = params.get("state");
  if (expectedState && returnedState && expectedState !== returnedState) {
    clearSession();
    clearFragment();
    return null;
  }

  const expiresIn = Number(params.get("expires_in") ?? "0");
  const idClaims = idToken ? decodeJwtPayload(idToken) : null;
  const username =
    (idClaims?.["email"] as string | undefined) ??
    (idClaims?.["cognito:username"] as string | undefined) ??
    (idClaims?.["sub"] as string | undefined);

  const session: AuthSession = {
    idToken: idToken ?? "",
    accessToken: accessToken ?? "",
    expiresAt: expiresIn > 0 ? Math.floor(Date.now() / 1000) + expiresIn : undefined,
    username,
  };

  storeSession(session);
  sessionStorage.removeItem(STATE_KEY);
  clearFragment();
  return session;
}

/** Remove the OAuth fragment from the address bar without reloading. */
function clearFragment(): void {
  const { pathname, search } = window.location;
  window.history.replaceState(null, "", pathname + search);
}

/**
 * Return the bearer token to attach to backend requests. The backend
 * validates the Cognito JWT; the id token carries the audience + group
 * claims the backend/gateway expect.
 */
export function getBearerToken(session: AuthSession | null): string | null {
  if (!session) return null;
  return session.idToken || session.accessToken || null;
}
