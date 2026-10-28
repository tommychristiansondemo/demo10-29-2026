/**
 * PharmaPulse SPA root (Requirement 13.5).
 *
 * Composes the header (with Cognito login/logout controls, Req 13.3) and the
 * streaming chat panel (Req 13.1). The tool-call trace panel and approval
 * dialog (task 17.2) plug into the `traceSlot` / a dialog overlay without
 * reshaping this component — the chat hook already collects `toolTrace` and
 * `pendingApproval`.
 */

import { useAuth } from "./auth/useAuth";
import { useChat } from "./hooks/useChat";
import { LoginControls } from "./components/LoginControls";
import { ChatPanel } from "./components/ChatPanel";
import { ToolTracePanel } from "./components/ToolTracePanel";
import { ApprovalDialog } from "./components/ApprovalDialog";

export default function App() {
  const auth = useAuth();
  const chat = useChat(auth.bearerToken);

  const canSend = auth.isAuthenticated;
  const disabledReason = auth.authConfigured
    ? "Sign in to start chatting"
    : "Cognito is not configured";

  return (
    <div className="app">
      <header className="app__header">
        <div className="app__brand">
          <span className="app__logo" aria-hidden="true">
            ℞
          </span>
          <span className="app__title">PharmaPulse</span>
        </div>
        <LoginControls auth={auth} />
      </header>

      <main className="app__main">
        <ChatPanel
          messages={chat.messages}
          isStreaming={chat.isStreaming}
          canSend={canSend}
          disabledReason={disabledReason}
          onSend={chat.sendMessage}
          onCancel={chat.cancel}
          traceSlot={<ToolTracePanel entries={chat.toolTrace} />}
        />
      </main>

      {chat.pendingApproval && (
        <ApprovalDialog
          approval={chat.pendingApproval}
          busy={chat.isStreaming}
          onConfirm={chat.confirmApproval}
          onCancel={chat.clearPendingApproval}
        />
      )}
    </div>
  );
}
