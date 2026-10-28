/**
 * Tool-call trace panel tests (Requirement 13.2).
 *
 * Verifies ToolTracePanel renders one row per tool invocation showing the
 * tool name, lifecycle status, and the (expandable) input/output. Also covers
 * the empty state.
 */

import { describe, it, expect } from "vitest";
import { render, screen, within } from "@testing-library/react";

import { ToolTracePanel } from "../ToolTracePanel";
import type { ToolTraceEntry } from "../../types";

describe("ToolTracePanel (Req 13.2)", () => {
  it("renders an empty state when no tools have been invoked", () => {
    render(<ToolTracePanel entries={[]} />);
    expect(screen.getByText(/no tools invoked yet/i)).toBeInTheDocument();
  });

  it("renders one row per tool invocation with name and status", () => {
    const entries: ToolTraceEntry[] = [
      {
        id: "t1",
        tool: "search_drug_shortages",
        input: { name: "amoxicillin" },
        output: { status: "OK", data: [{ generic_name: "amoxicillin" }] },
        status: "completed",
      },
      {
        id: "t2",
        tool: "get_low_stock_items",
        input: { location: "RX-ATL" },
        status: "started",
      },
    ];

    render(<ToolTracePanel entries={entries} />);

    // Both tool names appear.
    expect(screen.getByText("search_drug_shortages")).toBeInTheDocument();
    expect(screen.getByText("get_low_stock_items")).toBeInTheDocument();

    // One list item per entry.
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);

    // Status labels are mapped: completed -> "done", started -> "running".
    expect(screen.getByText("done")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
  });

  it("renders input and output sections with serialized content", () => {
    const entries: ToolTraceEntry[] = [
      {
        id: "t1",
        tool: "lookup_ndc",
        input: { ndc: "0071-0155" },
        output: { status: "OK", data: { brand_name: "Lipitor" } },
        status: "completed",
      },
    ];

    render(<ToolTracePanel entries={entries} />);

    const item = screen.getByRole("listitem");
    expect(within(item).getByText("Input")).toBeInTheDocument();
    expect(within(item).getByText("Output")).toBeInTheDocument();

    // The serialized JSON input/output is present in the rendered <pre>.
    expect(within(item).getByText(/"ndc": "0071-0155"/)).toBeInTheDocument();
    expect(within(item).getByText(/"brand_name": "Lipitor"/)).toBeInTheDocument();
  });

  it("shows an error status label for failed tool calls", () => {
    const entries: ToolTraceEntry[] = [
      { id: "t1", tool: "submit_order", status: "error" },
    ];
    render(<ToolTracePanel entries={entries} />);
    expect(screen.getByText("error")).toBeInTheDocument();
  });
});
