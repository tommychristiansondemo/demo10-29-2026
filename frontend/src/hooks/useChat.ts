/**
 * Chat state management hook (Requirement 13.1).
 *
 * Owns the conversation messages and drives the streaming lifecycle via
 * `sendChat`. Assistant tokens are appended to the in-flight assistant
 * message so the UI renders responses as streaming output.
 *
 * Tool-trace and approval events are collected here too so task 17.2 can
 * render the trace panel / approval dialog without changing the streaming
 * layer. This task renders only the chat panel and login controls.
 */

import { useCallback, useRef, useState } from "react";
import { sendChat } from "../api/chatStream";
import type { ChatMessage, PendingApproval, ToolTraceEntry } from "../types";

let idSeq = 0;
function nextId(prefix: string): string {
  return `${prefix}-${Date.now()}-${idSeq++}`;
}

export interface UseChatResult {
  messages: ChatMessage[];
  toolTrace: ToolTraceEntry[];
  pendingApproval: PendingApproval | null;
  isStreaming: boolean;
  sendMessage: (prompt: string) => void;
  cancel: () => void;
  clearPendingApproval: () => void;
  /**
   * Confirm a `PENDING_APPROVAL` order, re-invoking `submit_order` with
   * `confirm=true` for the given draft (Requirements 6.3, 6.4, 13.4).
   */
  confirmApproval: (draftId: string) => void;
}

export function useChat(bearerToken: string | null): UseChatResult {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [toolTrace, setToolTrace] = useState<ToolTraceEntry[]>([]);
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const sessionIdRef = useRef<string>(nextId("session"));

  const appendToAssistant = useCallback((assistantId: string, text: string) => {
    setMessages((prev) =>
      prev.map((m) => (m.id === assistantId ? { ...m, content: m.content + text } : m))
    );
  }, []);

  const finalizeAssistant = useCallback((assistantId: string, isError = false) => {
    setMessages((prev) =>
      prev.map((m) =>
        m.id === assistantId ? { ...m, streaming: false, isError: isError || m.isError } : m
      )
    );
  }, []);

  /**
   * Start a streaming turn: append the user + placeholder assistant messages
   * and drive the SSE lifecycle. Shared by `sendMessage` and the approval
   * confirmation flow (`confirmApproval`) so both go through the single
   * backend `POST /chat` path (agent -> Gateway -> tools).
   */
  const runChat = useCallback(
    (prompt: string) => {
      const userMessage: ChatMessage = {
        id: nextId("user"),
        role: "user",
        content: prompt,
      };
      const assistantMessage: ChatMessage = {
        id: nextId("assistant"),
        role: "assistant",
        content: "",
        streaming: true,
      };
      setMessages((prev) => [...prev, userMessage, assistantMessage]);
      setIsStreaming(true);

      const controller = new AbortController();
      abortRef.current = controller;

      void sendChat({
        prompt,
        bearerToken,
        sessionId: sessionIdRef.current,
        signal: controller.signal,
        onEvent: (event) => {
          switch (event.type) {
            case "token":
              appendToAssistant(assistantMessage.id, event.text);
              break;
            case "tool":
              setToolTrace((prev) => mergeTool(prev, event.entry));
              break;
            case "approval":
              setPendingApproval(event.approval);
              break;
            case "error":
              appendToAssistant(
                assistantMessage.id,
                (assistantMessage.content ? "\n\n" : "") + `⚠️ ${event.message}`
              );
              finalizeAssistant(assistantMessage.id, true);
              break;
            case "done":
              finalizeAssistant(assistantMessage.id);
              break;
          }
        },
      })
        .catch((err: Error) => {
          appendToAssistant(assistantMessage.id, `\n\n⚠️ ${err.message}`);
          finalizeAssistant(assistantMessage.id, true);
        })
        .finally(() => {
          finalizeAssistant(assistantMessage.id);
          setIsStreaming(false);
          abortRef.current = null;
        });
    },
    [appendToAssistant, bearerToken, finalizeAssistant]
  );

  const sendMessage = useCallback(
    (prompt: string) => {
      const trimmed = prompt.trim();
      if (!trimmed || isStreaming) return;
      runChat(trimmed);
    },
    [isStreaming, runChat]
  );

  /**
   * Confirm a `PENDING_APPROVAL` order (Requirements 6.3, 6.4, 13.4).
   *
   * The SPA has no direct path to the MCP `submit_order` tool — tools are
   * reached only by the agent through the Gateway. So confirming an approval
   * re-sends through the same chat path with an explicit instruction that
   * carries the draft id and the `confirm=true` intent. The agent then
   * re-invokes `submit_order(draft_id, confirm=true)`, which records the
   * order as `SUBMITTED`. The transient pending-approval state is cleared so
   * the dialog closes before the confirmation turn streams back.
   */
  const confirmApproval = useCallback(
    (draftId: string) => {
      if (!draftId || isStreaming) return;
      setPendingApproval(null);
      runChat(
        `Approved. Please resubmit draft order ${draftId} with confirmation ` +
          `(submit_order with confirm=true).`
      );
    },
    [isStreaming, runChat]
  );

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setIsStreaming(false);
  }, []);

  const clearPendingApproval = useCallback(() => setPendingApproval(null), []);

  return {
    messages,
    toolTrace,
    pendingApproval,
    isStreaming,
    sendMessage,
    cancel,
    clearPendingApproval,
    confirmApproval,
  };
}

/** Merge a tool entry by id so status updates replace prior entries. */
function mergeTool(prev: ToolTraceEntry[], entry: ToolTraceEntry): ToolTraceEntry[] {
  const existingIndex = prev.findIndex((t) => t.id === entry.id);
  if (existingIndex === -1) return [...prev, entry];
  const next = prev.slice();
  next[existingIndex] = { ...next[existingIndex], ...entry };
  return next;
}
