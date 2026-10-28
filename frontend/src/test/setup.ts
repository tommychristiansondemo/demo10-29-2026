/**
 * Vitest test setup (task 17.3).
 *
 * - Registers `@testing-library/jest-dom` matchers (e.g. `toBeInTheDocument`).
 * - Auto-cleans the rendered DOM between tests to keep cases hermetic.
 * - Polyfills `crypto.getRandomValues` if the jsdom environment lacks it so
 *   the Cognito helper's `randomState()` does not throw when exercised.
 */

import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});

const cryptoObj = globalThis.crypto as Crypto | undefined;
if (!cryptoObj || typeof cryptoObj.getRandomValues !== "function") {
  // Minimal, test-only shim. jsdom in modern Node usually provides webcrypto,
  // so this branch is a safety net for environments that do not.
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: {
      getRandomValues<T extends ArrayBufferView | null>(arr: T): T {
        if (arr && "length" in (arr as object)) {
          const view = arr as unknown as Uint8Array;
          for (let i = 0; i < view.length; i++) view[i] = Math.floor(Math.random() * 256);
        }
        return arr;
      },
    },
  });
}
