/**
 * Streaming chat client for the backend `POST /chat` SSE endpoint
 * (Requirements 13.1, 15.3).
 *
 * The backend streams tokens + tool-trace events as Server-Sent Events.
 * We use `fetch` + `ReadableStream` rather than `EventSource` because:
 *   1. the request must be a POST carrying the chat prompt, and
 *   2. we must attach the Cognito JWT as an `Authorization: Bearer` header,
 *      which `EventSource` cannot do.
 *
 * SSE frames are parsed incrementally and normalized into the
 * `ChatStreamEvent` union consumed by the UI. The parser is tolerant of the
 * exact event naming so it works whether the backend labels events by
 * `event:` name or encodes a typed JSON payload in `data:`.
 */

import { config } from "../config";
import type { ChatStreamEvent, ToolTraceEntry, PendingApproval } from "../types";

export interface SendChatOptions {
  prompt: string;
  bearerToken: string | null;
  /** Optional conversation/session id the backend may use for memory. */
  sessionId?: string;
  /** Called for every normalized stream event. */
  onEvent: (event: ChatStreamEvent) => void;
  /** Abort signal to cancel an in-flight stream. */
  signal?: AbortSignal;
}

/** One raw SSE frame: an optional event name and a data payload. */
interface SseFrame {
  event?: string;
  data: string;
}

/**
 * POST a prompt to the backend and stream the response, invoking `onEvent`
 * for each parsed event. Resolves when the stream completes.
 */
export async function sendChat(options: SendChatOptions): Promise<void> {
  const { prompt, bearerToken, sessionId, onEvent, signal } = options;

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  if (bearerToken) {
    headers["Authorization"] = `Bearer ${bearerToken}`;
  }

  let response: Response;
  try {
    response = await fetch(`${config.chatApiUrl}/chat`, {
      method: "POST",
      headers,
      body: JSON.stringify({ prompt, sessionId }),
      signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    onEvent({ type: "error", message: `Network error: ${(err as Error).message}` });
    return;
  }

  if (!response.ok) {
    const detail = await safeReadText(response);
    const message =
      response.status === 401
        ? "Not authorized. Please sign in again."
        : `Request failed (${response.status}). ${detail}`.trim();
    onEvent({ type: "error", message });
    return;
  }

  if (!response.body) {
    onEvent({ type: "error", message: "Streaming is not supported by this response." });
    return;
  }

  await consumeSseStream(response.body, onEvent, signal);
}

async function safeReadText(response: Response): Promise<string> {
  try {
    return (await response.text()).slice(0, 200);
  } catch {
    return "";
  }
}

/** Read a ReadableStream of SSE bytes and dispatch normalized events. */
async function consumeSseStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: ChatStreamEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    // eslint-disable-next-line no-constant-condition
    while (true) {
      if (signal?.aborted) break;
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line.
      let separatorIndex: number;
      while ((separatorIndex = indexOfFrameBoundary(buffer)) !== -1) {
        const rawFrame = buffer.slice(0, separatorIndex);
        buffer = buffer.slice(separatorIndex).replace(/^(\r?\n){2}/, "");
        const frame = parseSseFrame(rawFrame);
        if (frame) dispatchFrame(frame, onEvent);
      }
    }

    // Flush any trailing frame that lacked a terminating blank line.
    const tail = buffer.trim();
    if (tail) {
      const frame = parseSseFrame(tail);
      if (frame) dispatchFrame(frame, onEvent);
    }
  } finally {
    reader.releaseLock();
  }
}

/** Find the index of the next SSE frame boundary (blank line). */
function indexOfFrameBoundary(buffer: string): number {
  const lf = buffer.indexOf("\n\n");
  const crlf = buffer.indexOf("\r\n\r\n");
  if (lf === -1) return crlf;
  if (crlf === -1) return lf;
  return Math.min(lf, crlf);
}

/** Parse a raw SSE frame into its event name + concatenated data lines. */
function parseSseFrame(raw: string): SseFrame | null {
  const lines = raw.split(/\r?\n/);
  let event: string | undefined;
  const dataLines: string[] = [];

  for (const line of lines) {
    if (!line || line.startsWith(":")) continue; // comment / keep-alive
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }

  if (dataLines.length === 0 && event === undefined) return null;
  return { event, data: dataLines.join("\n") };
}

/** Normalize a raw SSE frame into a `ChatStreamEvent` and dispatch it. */
function dispatchFrame(frame: SseFrame, onEvent: (event: ChatStreamEvent) => void): void {
  const { event, data } = frame;

  // The conventional SSE end-of-stream sentinel.
  if (data === "[DONE]" || event === "done") {
    onEvent({ type: "done" });
    return;
  }

  const payload = tryParseJson(data);

  // Case 1: the data is a typed JSON object (preferred backend contract).
  if (payload && typeof payload === "object") {
    const typed = normalizeTypedPayload(event, payload as Record<string, unknown>);
    if (typed) {
      onEvent(typed);
      return;
    }
  }

  // Case 2: named events carrying plain-text/JSON payloads.
  switch (event) {
    case "token":
    case "message":
    case "delta":
      onEvent({ type: "token", text: typeof payload === "string" ? payload : data });
      return;
    case "tool":
    case "tool_call":
    case "tool_trace":
      onEvent({ type: "tool", entry: coerceToolEntry(payload ?? data) });
      return;
    case "approval":
    case "pending_approval":
      onEvent({ type: "approval", approval: coerceApproval(payload ?? data) });
      return;
    case "error":
      onEvent({ type: "error", message: typeof payload === "string" ? payload : data });
      return;
    default:
      // Unnamed event: treat the data as a raw token.
      if (data) onEvent({ type: "token", text: data });
  }
}

/** Interpret a typed JSON payload `{ type: ... }` into a stream event. */
function normalizeTypedPayload(
  eventName: string | undefined,
  payload: Record<string, unknown>
): ChatStreamEvent | null {
  const type = (payload["type"] as string | undefined) ?? eventName;
  switch (type) {
    case "token":
    case "delta":
    case "message": {
      const text =
        (payload["text"] as string | undefined) ??
        (payload["token"] as string | undefined) ??
        (payload["content"] as string | undefined) ??
        "";
      return { type: "token", text };
    }
    case "tool":
    case "tool_call":
    case "tool_trace":
      return { type: "tool", entry: coerceToolEntry(payload) };
    case "approval":
    case "pending_approval":
      return { type: "approval", approval: coerceApproval(payload) };
    case "error":
      return {
        type: "error",
        message: (payload["message"] as string | undefined) ?? "Unknown error",
      };
    case "done":
      return { type: "done" };
    default:
      return null;
  }
}

function tryParseJson(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return data;
  }
}

let toolCounter = 0;
function coerceToolEntry(value: unknown): ToolTraceEntry {
  if (value && typeof value === "object") {
    const obj = value as Record<string, unknown>;
    return {
      id:
        (obj["id"] as string | undefined) ??
        `tool-${Date.now()}-${toolCounter++}`,
      tool: (obj["tool"] as string | undefined) ?? (obj["name"] as string | undefined) ?? "tool",
      input: obj["input"] ?? obj["arguments"],
      output: obj["output"] ?? obj["result"],
      status: obj["status"] as ToolTraceEntry["status"],
    };
  }
  return { id: `tool-${Date.now()}-${toolCounter++}`, tool: String(value) };
}

function coerceApproval(value: unknown): PendingApproval {
  if (value && typeof value === "object") {
    const obj = value as Record<string, unknown>;
    return {
      draftId:
        (obj["draftId"] as string | undefined) ??
        (obj["draft_id"] as string | undefined) ??
        "",
      total: obj["total"] as number | undefined,
      message: obj["message"] as string | undefined,
    };
  }
  return { draftId: "", message: String(value) };
}
