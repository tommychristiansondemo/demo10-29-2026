/**
 * React hook wrapping the Cognito hosted-UI auth lifecycle (Requirement 13.3).
 *
 * Exposes the current session plus `login` / `logout` controls. On mount it
 * completes any pending hosted-UI redirect (parsing tokens from the URL
 * fragment) and loads any stored session.
 */

import { useCallback, useEffect, useState } from "react";
import type { AuthSession } from "../types";
import {
  completeLoginFromRedirect,
  getBearerToken,
  login as cognitoLogin,
  logout as cognitoLogout,
} from "./cognito";
import { isAuthConfigured } from "../config";

export interface UseAuthResult {
  session: AuthSession | null;
  isAuthenticated: boolean;
  /** Whether Cognito is configured (controls disabled state on buttons). */
  authConfigured: boolean;
  username?: string;
  login: () => void;
  logout: () => void;
  /** Bearer token for backend requests, or null when signed out. */
  bearerToken: string | null;
}

export function useAuth(): UseAuthResult {
  const [session, setSession] = useState<AuthSession | null>(null);

  useEffect(() => {
    // Completes a redirect (if present) or restores a stored session.
    setSession(completeLoginFromRedirect());
  }, []);

  const login = useCallback(() => {
    cognitoLogin();
  }, []);

  const logout = useCallback(() => {
    setSession(null);
    cognitoLogout();
  }, []);

  return {
    session,
    isAuthenticated: Boolean(session),
    authConfigured: isAuthConfigured(),
    username: session?.username,
    login,
    logout,
    bearerToken: getBearerToken(session),
  };
}
