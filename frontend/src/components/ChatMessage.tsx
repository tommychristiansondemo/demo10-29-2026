/**
 * Renders a single chat message bubble. Assistant messages that are still
 * streaming show a blinking caret to convey live token output (Req 13.1).
 */

import type { ChatMessage as ChatMessageModel } from "../types";

interface ChatMessageProps {
  message: ChatMessageModel;
}

export function ChatMessage({ message }: ChatMessageProps) {
  const roleClass = `chat-message chat-message--${message.role}${
    message.isError ? " chat-message--error" : ""
  }`;

  return (
    <div className={roleClass} data-role={message.role}>
      <div className="chat-message__role">
        {message.role === "user" ? "You" : message.role === "assistant" ? "PharmaPulse" : "System"}
      </div>
      <div className="chat-message__content">
        {message.content}
        {message.streaming && <span className="chat-message__caret" aria-hidden="true" />}
      </div>
    </div>
  );
}
