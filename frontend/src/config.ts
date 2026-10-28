/**
 * Runtime configuration for the PharmaPulse SPA.
 *
 * All values are sourced from Vite environment variables (prefixed
 * `VITE_`) so nothing is hard-coded and no secrets live in source.
 * These are PUBLIC client-side values only: the Cognito hosted UI uses a
 * public OAuth client, so there is no client secret here.
 *
 * Provide values via `.env.local` (local dev) or build-time env (CI/deploy).
 * See `.env.example` for the full list.
 */

export interface AppConfig {
  /** Base URL of the streaming backend exposing `POST /chat`. */
  chatApiUrl: string;
  /** Cognito hosted-UI domain, e.g. https://x.auth.us-east-2.amazoncognito.com */
  cognitoDomain: string;
  /** Cognito app client id (public). */
  cognitoClientId: string;
  /** Redirect URI registered as a callback on the app client. */
  cognitoRedirectUri: string;
  /** Redirect URI registered as a sign-out URL on the app client. */
  cognitoLogoutUri: string;
  /** OAuth scopes requested at the authorize endpoint. */
  cognitoScope: string;
}

function readEnv(key: keyof ImportMetaEnv, fallback = ""): string {
  const value = import.meta.env[key];
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

/** Strip a single trailing slash so URL joins are predictable. */
function trimTrailingSlash(url: string): string {
  return url.endsWith("/") ? url.slice(0, -1) : url;
}

export const config: AppConfig = {
  chatApiUrl: trimTrailingSlash(readEnv("VITE_CHAT_API_URL")),
  cognitoDomain: trimTrailingSlash(readEnv("VITE_COGNITO_DOMAIN")),
  cognitoClientId: readEnv("VITE_COGNITO_CLIENT_ID"),
  cognitoRedirectUri: readEnv(
    "VITE_COGNITO_REDIRECT_URI",
    typeof window !== "undefined" ? window.location.origin : ""
  ),
  cognitoLogoutUri: readEnv(
    "VITE_COGNITO_LOGOUT_URI",
    typeof window !== "undefined" ? window.location.origin : ""
  ),
  cognitoScope: readEnv("VITE_COGNITO_SCOPE", "openid email profile"),
};

/** True when the Cognito hosted UI is sufficiently configured to attempt login. */
export function isAuthConfigured(cfg: AppConfig = config): boolean {
  return Boolean(cfg.cognitoDomain && cfg.cognitoClientId);
}
