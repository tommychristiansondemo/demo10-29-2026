/**
 * Streaming chat panel (Requirements 13.1, 13.5).
 *
 * Renders the conversation, auto-scrolls as streaming tokens arrive, and
 * provides the prompt composer. Sending requires an authenticated session
 * so the Cognito JWT can be attached to the backend `POST /chat` request.
 *
 * The layout reserves a right-hand slot (`aside`) for the tool-call trace
 * panel added in task 17.2; it is passed in as `traceSlot` so this component
 * does not need to change when that panel lands.
 */

import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChatMessage } from "./ChatMessage";
import type { ChatMessage as ChatMessageModel } from "../types";

interface ChatPanelProps {
  messages: ChatMessageModel[];
  isStreaming: boolean;
  canSend: boolean;
  disabledReason?: string;
  onSend: (prompt: string) => void;
  onCancel: () => void;
  /** Optional slot for the tool-call trace panel (task 17.2). */
  traceSlot?: ReactNode;
}

export function ChatPanel({
  messages,
  isStreaming,
  canSend,
  disabledReason,
  onSend,
  onCancel,
  traceSlot,
}: ChatPanelProps) {
  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to the newest content as tokens stream in.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const submit = () => {
    if (!draft.trim() || !canSend || isStreaming) return;
    onSend(draft);
    setDraft("");
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="chat-layout">
      <section className="chat-panel" aria-label="chat">
        <div className="chat-panel__messages" ref={scrollRef}>
          {messages.length === 0 ? (
            <div className="chat-panel__empty">
              <h2>PharmaPulse</h2>
              <p>
                Ask about FDA drug shortages, recalls, NDC details, pharmacy inventory,
                therapeutic alternatives, or draft a replenishment order.
              </p>
            </div>
          ) : (
            messages.map((m) => <ChatMessage key={m.id} message={m} />)
          )}
        </div>

        <div className="chat-panel__composer">
          <textarea
            className="chat-panel__input"
            placeholder={
              canSend ? "Ask PharmaPulse…" : disabledReason ?? "Sign in to start chatting"
            }
            value={draft}
            disabled={!canSend}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={2}
            aria-label="message"
          />
          {isStreaming ? (
            <button type="button" className="btn btn--secondary" onClick={onCancel}>
              Stop
            </button>
          ) : (
            <button
              type="button"
              className="btn btn--primary"
              onClick={submit}
              disabled={!canSend || !draft.trim()}
            >
              Send
            </button>
          )}
        </div>
      </section>

      {traceSlot && <aside className="chat-aside">{traceSlot}</aside>}
    </div>
  );
}
