// What actually produced the answer beside this panel.
//
// VayuMitra's replies are composed by a four-stage pipeline rather than one model call,
// and the stage that matters is the last one: a verifier that reads the draft back
// against the retrieved evidence and can reject it. A rejected draft never reaches the
// reader - the deterministic CPCB template does. That is a claim worth being able to
// check, so this panel shows the pipeline, the corpus it retrieves from, and the graph
// it walks, straight from the API rather than from a caption we wrote.
//
// Renders nothing at all if the endpoint is unavailable: it is an explanation of a
// feature, and an explanation of an absent feature is worse than silence.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { API_BASE } from "@/lib/api";

type Agent = { name: string; model: string; job: string };
type Pipeline = {
  available: boolean;
  used_by_chat?: boolean;
  agents?: Agent[];
  on_rejection?: string;
  corpus?: { documents?: number; passages?: number; authorities?: number; method?: string };
  graph?: { nodes?: number; edges?: number; node_kinds?: Record<string, number> };
};

const pipelineQuery = {
  queryKey: ["ai-pipeline"],
  queryFn: async (): Promise<Pipeline> => {
    const r = await fetch(`${API_BASE}/ai/pipeline`);
    if (!r.ok) throw new Error(String(r.status));
    return (await r.json()) as Pipeline;
  },
  staleTime: 10 * 60_000,
  retry: 1,
};

export function ReasoningPanel() {
  const q = useQuery(pipelineQuery);
  const [open, setOpen] = useState(false);
  const p = q.data;

  // Render whenever the layer is actually up. Whether the chat route currently uses it
  // is a separate fact, and one the panel states rather than implies.
  if (!q.isSuccess || !p?.available || !p.agents?.length) return null;
  const usedByChat = p.used_by_chat !== false;

  return (
    <div className="border-b border-border px-5 py-3">
      <button
        onClick={() => setOpen((s) => !s)}
        aria-expanded={open}
        className={`rounded-full border px-3 py-1.5 text-[12px] font-semibold transition-colors ${
          open
            ? "border-accent bg-accent text-white"
            : "border-border text-text-dim hover:border-accent-dim hover:text-accent"
        }`}
      >
        {open ? "Hide the reasoning layer" : "How does it check itself?"}
      </button>

      {open && (
        <div className="mt-3">
          <ol className="space-y-2">
            {p.agents.map((a, i) => (
              <li key={a.name} className="flex gap-2.5">
                <span className="mono mt-0.5 h-4 w-4 flex-none rounded-sm bg-surface-1 text-center text-[10.5px] font-bold leading-4 text-accent">
                  {i + 1}
                </span>
                <span className="min-w-0">
                  <span className="text-[12.5px] font-semibold capitalize">{a.name}</span>
                  <span className="mono ml-1.5 text-[10.5px] text-text-mute">{a.model}</span>
                  <span className="mono block text-[11px] leading-snug text-text-dim">{a.job}</span>
                </span>
              </li>
            ))}
          </ol>

          {!usedByChat && (
            <p className="mono mt-3 rounded-md bg-surface-1 px-2.5 py-2 text-[11px] leading-snug text-text-dim">
              Running and inspectable at <b className="text-foreground">/ai/pipeline</b>,{" "}
              <b className="text-foreground">/rag/search</b> and{" "}
              <b className="text-foreground">/graph</b>. Chat is currently set to the
              single-pass path, which answers about four seconds faster.
            </p>
          )}
          {p.on_rejection && (
            <p className="mono mt-3 border-t border-border pt-2.5 text-[11px] leading-snug text-text-dim">
              If the verifier rejects a draft: <b className="text-foreground">{p.on_rejection}</b>.
              The answer can only ever get safer, never less grounded.
            </p>
          )}

          <div className="mono mt-2.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-text-mute">
            {p.corpus?.passages ? (
              <span>
                Corpus <b className="text-foreground">{p.corpus.documents}</b> documents ·{" "}
                <b className="text-foreground">{p.corpus.passages}</b> passages
              </span>
            ) : null}
            {p.graph?.nodes ? (
              <span>
                Graph <b className="text-foreground">{p.graph.nodes}</b> nodes ·{" "}
                <b className="text-foreground">{p.graph.edges}</b> edges
              </span>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}
