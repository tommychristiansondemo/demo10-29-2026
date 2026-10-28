/**
 * Tool-call trace panel (Requirements 13.2).
 *
 * Lists the tool invocations recorded during agent responses — one row per
 * `ToolTraceEntry` collected by `useChat`. Each row shows the tool name, its
 * lifecycle status, and the (optionally expandable) input arguments and
 * output/result envelope so the user can understand what the agent did.
 *
 * The entries are populated by the streaming layer (`chatStream.ts`) from the
 * backend's tool-trace SSE events, so this component is purely presentational:
 * it renders whatever trace the hook has accumulated and performs no fetching.
 */

import type { ToolTraceEntry } from "../types";

interface ToolTracePanelProps {
  entries: ToolTraceEntry[];
}

/** Human-readable label + CSS modifier for a tool-call lifecycle status. */
function statusMeta(status: ToolTraceEntry["status"]): { label: string; modifier: string } {
  switch (status) {
    case "started":
      return { label: "running", modifier: "started" };
    case "completed":
      return { label: "done", modifier: "completed" };
    case "error":
      return { label: "error", modifier: "error" };
    default:
      return { label: "", modifier: "unknown" };
  }
}

/** Pretty-print a JSON-serializable value; fall back to String() on cycles. */
function formatValue(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function ToolTracePanel({ entries }: ToolTracePanelProps) {
  return (
    <section className="tool-trace" aria-label="tool-call trace">
      <h3 className="tool-trace__title">Tool calls</h3>

      {entries.length === 0 ? (
        <p className="tool-trace__empty">No tools invoked yet.</p>
      ) : (
        <ol className="tool-trace__list">
          {entries.map((entry) => {
            const meta = statusMeta(entry.status);
            const input = formatValue(entry.input);
            const output = formatValue(entry.output);
            return (
              <li key={entry.id} className="tool-trace__item">
                <div className="tool-trace__header">
                  <span className="tool-trace__tool">{entry.tool}</span>
                  {meta.label && (
                    <span
                      className={`tool-trace__status tool-trace__status--${meta.modifier}`}
                    >
                      {meta.label}
                    </span>
                  )}
                </div>

                {input && (
                  <details className="tool-trace__section">
                    <summary>Input</summary>
                    <pre className="tool-trace__code">{input}</pre>
                  </details>
                )}

                {output && (
                  <details className="tool-trace__section">
                    <summary>Output</summary>
                    <pre className="tool-trace__code">{output}</pre>
                  </details>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
