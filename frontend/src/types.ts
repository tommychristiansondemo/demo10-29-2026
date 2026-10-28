/**
 * Shared domain types for the PharmaPulse SPA.
 *
 * These model the SSE event contract streamed by the backend `POST /chat`
 * endpoint (design: "Backend streams tokens + tool-trace events as SSE")
 * and the client-side chat/auth state. Task 17.2 (tool-call trace panel +
 * approval dialog) builds on `ToolTraceEntry` and `PendingApproval`, which
 * are defined here so the components can be dropped in without reshaping
 * the streaming layer.
 */

/** Role of a chat message author. */
export type ChatRole = "user" | "assistant" | "system";

/** A single chat message rendered in the conversation panel. */
export interface ChatMessage {
  id: string;
  role: ChatRole;
  /** Accumulated text; grows as streaming tokens arrive for assistant turns. */
  content: string;
  /** True while the assistant message is still being streamed. */
  streaming?: boolean;
  /** True if this message represents an error surfaced from the stream. */
  isError?: boolean;
}

/**
 * A tool invocation recorded during an agent response. Consumed by the
 * tool-call trace panel (task 17.2). Defined here so the streaming layer
 * can populate it today.
 */
export interface ToolTraceEntry {
  id: string;
  /** Tool name, e.g. `search_drug_shortages`. */
  tool: string;
  /** Optional JSON-serializable input arguments. */
  input?: unknown;
  /** Optional JSON-serializable output / envelope. */
  output?: unknown;
  /** Lifecycle status of the tool call. */
  status?: "started" | "completed" | "error";
}

/**
 * Transient order-approval state surfaced when a `submit_order` tool call
 * returns `PENDING_APPROVAL`. The approval dialog (task 17.2) reads this.
 */
export interface PendingApproval {
  draftId: string;
  total?: number;
  message?: string;
}

/**
 * Normalized form of an SSE event emitted by the backend. The raw SSE
 * `event:`/`data:` frames are parsed into this discriminated union.
 */
export type ChatStreamEvent =
  | { type: "token"; text: string }
  | { type: "tool"; entry: ToolTraceEntry }
  | { type: "approval"; approval: PendingApproval }
  | { type: "error"; message: string }
  | { type: "done" };

/** Minimal set of claims / session info derived from the Cognito tokens. */
export interface AuthSession {
  idToken: string;
  accessToken: string;
  /** Epoch seconds when the tokens expire, if known. */
  expiresAt?: number;
  /** Human-readable identity (email or username) if present in the id token. */
  username?: string;
}
