/**
 * Login/logout controls tests (Requirement 13.3).
 *
 * Verifies LoginControls shows a "Sign in" control when unauthenticated and a
 * username + "Sign out" control when authenticated, invoking the auth actions
 * on click. The auth object is a plain stub matching `UseAuthResult` so the
 * component is tested in isolation from Cognito redirects.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { LoginControls } from "../LoginControls";
import type { UseAuthResult } from "../../auth/useAuth";

/** Build a UseAuthResult stub with sensible defaults, overridable per test. */
function makeAuth(overrides: Partial<UseAuthResult> = {}): UseAuthResult {
  return {
    session: null,
    isAuthenticated: false,
    authConfigured: true,
    username: undefined,
    login: vi.fn(),
    logout: vi.fn(),
    bearerToken: null,
    ...overrides,
  };
}

describe("LoginControls (Req 13.3)", () => {
  it("shows a Sign in control when unauthenticated and invokes login", async () => {
    const user = userEvent.setup();
    const auth = makeAuth({ isAuthenticated: false });
    render(<LoginControls auth={auth} />);

    const signIn = screen.getByRole("button", { name: /sign in/i });
    expect(signIn).toBeInTheDocument();
    expect(signIn).toBeEnabled();
    expect(screen.queryByRole("button", { name: /sign out/i })).not.toBeInTheDocument();

    await user.click(signIn);
    expect(auth.login).toHaveBeenCalledTimes(1);
  });

  it("disables Sign in when Cognito is not configured", () => {
    const auth = makeAuth({ isAuthenticated: false, authConfigured: false });
    render(<LoginControls auth={auth} />);
    expect(screen.getByRole("button", { name: /sign in/i })).toBeDisabled();
  });

  it("shows the username and Sign out control when authenticated and invokes logout", async () => {
    const user = userEvent.setup();
    const auth = makeAuth({
      isAuthenticated: true,
      username: "demo-buyer@example.com",
      bearerToken: "token",
    });
    render(<LoginControls auth={auth} />);

    expect(screen.getByText("demo-buyer@example.com")).toBeInTheDocument();
    const signOut = screen.getByRole("button", { name: /sign out/i });
    expect(signOut).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign in/i })).not.toBeInTheDocument();

    await user.click(signOut);
    expect(auth.logout).toHaveBeenCalledTimes(1);
  });

  it("falls back to 'Signed in' label when authenticated without a username", () => {
    const auth = makeAuth({ isAuthenticated: true, username: undefined });
    render(<LoginControls auth={auth} />);
    expect(screen.getByText("Signed in")).toBeInTheDocument();
  });
});
