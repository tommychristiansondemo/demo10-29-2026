/**
 * Order approval dialog (Requirements 13.4, 6.3).
 *
 * Shown whenever a `submit_order` tool call returns `PENDING_APPROVAL` — i.e.
 * the draft total exceeds the Approval_Threshold and awaits explicit human
 * confirmation (Req 6.2/6.3). The dialog surfaces the draft id and total and
 * requires an explicit action:
 *
 *   - Confirm  -> `onConfirm(draftId)` re-invokes `submit_order` with
 *                 `confirm=true` (Req 6.4). Because the SPA reaches tools only
 *                 through the agent via the backend `POST /chat` flow, the
 *                 confirmation is re-sent through that same chat path (see
 *                 `useChat.confirmApproval`), preserving the design's single
 *                 tool path (agent -> Gateway -> submit_order). It does NOT call
 *                 the MCP server directly.
 *   - Cancel   -> `onCancel()` clears the pending approval without submitting.
 *
 * It renders as a modal overlay so the approval is a deliberate, blocking
 * decision rather than an easily-missed inline control.
 */

import { useEffect, useRef } from "react";
import type { PendingApproval } from "../types";

interface ApprovalDialogProps {
  approval: PendingApproval;
  /** Re-invoke submit_order with confirm=true for the given draft. */
  onConfirm: (draftId: string) => void;
  /** Dismiss the dialog and clear pending-approval state. */
  onCancel: () => void;
  /** Disable the confirm action while a resubmission is in flight. */
  busy?: boolean;
}

/** Format a numeric total as USD; omit when the total is unknown. */
function formatTotal(total?: number): string | null {
  if (typeof total !== "number" || Number.isNaN(total)) return null;
  try {
    return total.toLocaleString(undefined, { style: "currency", currency: "USD" });
  } catch {
    return `$${total.toFixed(2)}`;
  }
}

export function ApprovalDialog({ approval, onConfirm, onCancel, busy }: ApprovalDialogProps) {
  const confirmRef = useRef<HTMLButtonElement>(null);

  // Move focus to the primary action and allow Escape to cancel.
  useEffect(() => {
    confirmRef.current?.focus();
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onCancel]);

  const total = formatTotal(approval.total);

  return (
    <div
      className="approval-overlay"
      role="presentation"
      onClick={(e) => {
        // Click on the backdrop (not the dialog) cancels.
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div
        className="approval-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="approval-title"
        aria-describedby="approval-body"
      >
        <h2 id="approval-title" className="approval-dialog__title">
          Order approval required
        </h2>

        <div id="approval-body" className="approval-dialog__body">
          <p>
            {approval.message ??
              "This order exceeds the approval threshold and needs your confirmation before it is submitted."}
          </p>
          <dl className="approval-dialog__details">
            <div>
              <dt>Draft</dt>
              <dd>{approval.draftId || "(unknown draft)"}</dd>
            </div>
            {total && (
              <div>
                <dt>Total</dt>
                <dd>{total}</dd>
              </div>
            )}
          </dl>
        </div>

        <div className="approval-dialog__actions">
          <button type="button" className="btn btn--secondary" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            ref={confirmRef}
            type="button"
            className="btn btn--primary"
            onClick={() => onConfirm(approval.draftId)}
            disabled={busy || !approval.draftId}
          >
            {busy ? "Submitting…" : "Confirm & submit"}
          </button>
        </div>
      </div>
    </div>
  );
}
