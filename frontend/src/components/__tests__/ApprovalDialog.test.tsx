/**
 * Approval dialog + re-submit flow tests (Requirements 13.4, 6.3).
 *
 * Two layers are covered:
 *   1. The presentational ApprovalDialog: confirming invokes `onConfirm` with
 *      the draft id; cancelling (button, Escape, backdrop) invokes `onCancel`.
 *   2. The re-submit flow through the real `useChat` hook: an `approval` SSE
 *      event raises `pendingApproval`; confirming clears it and re-invokes the
 *      chat stream with a `submit_order ... confirm=true` instruction (Req
 *      6.3/6.4), while cancelling simply clears the pending approval.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { SendChatOptions } from "../../api/chatStream";
import type { PendingApproval } from "../../types";

const sendChatMock = vi.fn<(opts: SendChatOptions) => Promise<void>>();
vi.mock("../../api/chatStream", () => ({
  sendChat: (opts: SendChatOptions) => sendChatMock(opts),
}));

import { ApprovalDialog } from "../ApprovalDialog";
import { useChat } from "../../hooks/useChat";

const approval: PendingApproval = {
  draftId: "draft-123",
  total: 7500,
  message: "This order exceeds the $5,000 approval threshold.",
};

describe("ApprovalDialog presentational (Req 13.4, 6.3)", () => {
  it("renders the draft id, total, and message", () => {
    render(
      <ApprovalDialog approval={approval} onConfirm={vi.fn()} onCancel={vi.fn()} />
    );
    expect(screen.getByText("draft-123")).toBeInTheDocument();
    expect(screen.getByText(/exceeds the \$5,000 approval threshold/i)).toBeInTheDocument();
    // Total formatted as USD currency.
    expect(screen.getByText("$7,500.00")).toBeInTheDocument();
  });

  it("confirming invokes onConfirm with the draft id (re-submit intent)", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <ApprovalDialog approval={approval} onConfirm={onConfirm} onCancel={vi.fn()} />
    );
    await user.click(screen.getByRole("button", { name: /confirm & submit/i }));
    expect(onConfirm).toHaveBeenCalledWith("draft-123");
  });

  it("cancelling invokes onCancel", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(
      <ApprovalDialog approval={approval} onConfirm={vi.fn()} onCancel={onCancel} />
    );
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("disables the confirm action while busy", () => {
    render(
      <ApprovalDialog approval={approval} onConfirm={vi.fn()} onCancel={vi.fn()} busy />
    );
    expect(screen.getByRole("button", { name: /submitting/i })).toBeDisabled();
  });
});

/** Harness exposing the useChat approval flow via ApprovalDialog. */
function ApprovalHarness() {
  const chat = useChat("fake-bearer-token");
  return (
    <div>
      <button type="button" onClick={() => chat.sendMessage("draft an order")}>
        send
      </button>
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

describe("Approval re-submit flow via useChat (Req 6.3, 13.4)", () => {
  beforeEach(() => sendChatMock.mockReset());

  it("confirming re-invokes the chat stream with a confirm=true submit_order instruction", async () => {
    const user = userEvent.setup();
    sendChatMock.mockResolvedValue(undefined);

    render(<ApprovalHarness />);

    // First turn: send a prompt, then the backend emits a PENDING_APPROVAL event.
    await user.click(screen.getByText("send"));
    expect(sendChatMock).toHaveBeenCalledTimes(1);

    const firstOnEvent = sendChatMock.mock.calls[0][0].onEvent;
    await act(async () => {
      firstOnEvent({ type: "approval", approval });
      firstOnEvent({ type: "done" });
    });

    // The dialog surfaces on PENDING_APPROVAL.
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toBeInTheDocument();

    // Confirm -> clears pending approval and re-invokes the stream.
    await user.click(screen.getByRole("button", { name: /confirm & submit/i }));

    // Dialog closes (pendingApproval cleared).
    await waitFor(() =>
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()
    );

    // A second chat turn was started carrying the confirm=true re-submit intent.
    expect(sendChatMock).toHaveBeenCalledTimes(2);
    const resubmit = sendChatMock.mock.calls[1][0];
    expect(resubmit.prompt).toMatch(/draft-123/);
    expect(resubmit.prompt).toMatch(/confirm=true/i);
    expect(resubmit.prompt.toLowerCase()).toContain("submit_order");
  });

  it("cancelling clears the pending approval without re-submitting", async () => {
    const user = userEvent.setup();
    sendChatMock.mockResolvedValue(undefined);

    render(<ApprovalHarness />);
    await user.click(screen.getByText("send"));

    const firstOnEvent = sendChatMock.mock.calls[0][0].onEvent;
    await act(async () => {
      firstOnEvent({ type: "approval", approval });
      firstOnEvent({ type: "done" });
    });

    expect(await screen.findByRole("alertdialog")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /cancel/i }));

    await waitFor(() =>
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()
    );

    // No second chat turn: cancel does not resubmit.
    expect(sendChatMock).toHaveBeenCalledTimes(1);
  });
});
