/**
 * Streaming render tests (Requirement 13.1).
 *
 * Verifies the chat panel renders streamed assistant tokens as they arrive.
 * We mock the SSE client (`api/chatStream`) so `sendChat` emits a sequence of
 * `token` events (and a terminating `done`) and assert the accumulated text
 * appears in the conversation. The test drives the real `useChat` hook through
 * a small harness so the streaming lifecycle is exercised end-to-end, without
 * any network.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { SendChatOptions } from "../../api/chatStream";

// Mock the streaming client. Each test installs an event script that the
// mocked sendChat replays into the hook's onEvent callback.
const sendChatMock = vi.fn<(opts: SendChatOptions) => Promise<void>>();
vi.mock("../../api/chatStream", () => ({
  sendChat: (opts: SendChatOptions) => sendChatMock(opts),
}));

// Import after the mock is registered so the hook binds to the mock.
import { useChat } from "../../hooks/useChat";
import { ChatPanel } from "../ChatPanel";

/** Minimal harness wiring the real useChat hook to ChatPanel. */
function Harness() {
  const chat = useChat("fake-bearer-token");
  return (
    <ChatPanel
      messages={chat.messages}
      isStreaming={chat.isStreaming}
      canSend={true}
      onSend={chat.sendMessage}
      onCancel={chat.cancel}
    />
  );
}

describe("ChatPanel streaming render (Req 13.1)", () => {
  beforeEach(() => {
    sendChatMock.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders streamed assistant tokens as they arrive", async () => {
    const user = userEvent.setup();
    sendChatMock.mockResolvedValue(undefined);

    render(<Harness />);

    // Send a prompt -> useChat invokes the mocked sendChat.
    await user.type(screen.getByLabelText("message"), "hi there");
    await user.click(screen.getByRole("button", { name: /send/i }));

    // The user's message renders immediately.
    expect(screen.getByText("hi there")).toBeInTheDocument();

    // Drive the token stream through the captured onEvent callback.
    const onEvent = sendChatMock.mock.calls[0][0].onEvent;
    await act(async () => {
      onEvent({ type: "token", text: "Hello" });
      onEvent({ type: "token", text: ", " });
      onEvent({ type: "token", text: "world" });
      onEvent({ type: "done" });
    });

    // The accumulated assistant content is present.
    expect(screen.getByText("Hello, world")).toBeInTheDocument();
  });

  it("accumulates tokens progressively (partial text visible mid-stream)", async () => {
    sendChatMock.mockResolvedValue(undefined);

    const user = userEvent.setup();
    render(<Harness />);
    await user.type(screen.getByLabelText("message"), "stream please");
    await user.click(screen.getByRole("button", { name: /send/i }));

    const onEvent = sendChatMock.mock.calls[0][0].onEvent;

    // First token.
    await act(async () => {
      onEvent({ type: "token", text: "Partial" });
    });
    expect(screen.getByText("Partial")).toBeInTheDocument();

    // Second token extends the same assistant message.
    await act(async () => {
      onEvent({ type: "token", text: " answer" });
      onEvent({ type: "done" });
    });
    expect(screen.getByText("Partial answer")).toBeInTheDocument();
  });
});
