import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { DelhiWardMap } from "@/components/DelhiWardMap";
import { MapView } from "@/components/MapView";
import { MethodPanel } from "@/components/HowItWorks";
import { aqiCategory, CELLS, type Cell } from "@/lib/air-data";
import {
  enforcementSourcesQuery,
  topTargetsQuery,
  wardsQuery,
} from "@/lib/api";

/** One row of the dispatch queue, normalised so the page renders the same whether
 *  it is showing ranked sources or the cell-level fallback. */
type QueueRow = {
  key: string;
  rank: number;
  title: string;
  ward: string | null;
  /** MCD ward number, so the map can paint the ward this source sits in. */
  wardNo: string | null;
  kind: string | null;
  priority: number;
  aqi: number;
  action: string;
  team: string | null;
  evidence: string;
  proxyOnly: boolean;
};

export const Route = createFileRoute("/enforcement")({
  head: () => ({
    meta: [
      { title: "Enforcement - AirGrid NCR" },
      { name: "description", content: "Live ward-level inspector deployment plan and ranked enforcement targets from the pipeline." },
    ],
  }),
  component: Enforcement,
});

function Enforcement() {
  const wards = useQuery(wardsQuery());
  const [showMethod, setShowMethod] = useState(false);
  const [query, setQuery] = useState("");

  // Two queues, and the better one wins.
  //
  // /enforcement/sources ranks individual physical sources - a named road, a
  // specific industrial polygon - which is what a team can actually be dispatched
  // to. /enforcement/top ranks grid cells, which nobody can be sent to, and is kept
  // only as a fallback so the page still works if the source list is unavailable.
  const srcQ = useQuery(enforcementSourcesQuery);
  const tops = useQuery(topTargetsQuery);

  const usingSources = Boolean(srcQ.data?.available && srcQ.data.items.length);

  const sorted: QueueRow[] = useMemo(() => {
    if (srcQ.data?.available && srcQ.data.items.length) {
      return [...srcQ.data.items]
        .sort((a, b) => a.rank - b.rank)
        .map((s) => ({
          key: s.source_id,
          rank: s.rank,
          title: s.source_name || s.source_id,
          ward: s.Ward_Name,
          wardNo: s.Ward_No,
          kind: s.source_type,
          priority: s.priority,
          aqi: s.peak_aqi,
          action: s.action,
          team: s.recommended_team,
          evidence: s.evidence,
          // Traffic uses placeholder emission factors and the proxy types have no
          // emissions data at all, so the basis travels with the row rather than
          // being flattened away.
          proxyOnly: s.basis !== "modelled_pm25_contribution",
        }));
    }
    const items = tops.data?.available ? tops.data.items : [];
    return [...items]
      .sort((a, b) => a.rank - b.rank)
      .map((t) => ({
        key: String(t.cell_id),
        rank: t.rank,
        title: t.ward_name ?? `Cell ${t.cell_id}`,
        ward: t.ward_name ?? null,
        wardNo: t.ward_no ?? null,
        kind: t.dominant_source,
        priority: t.max_priority,
        aqi: t.max_aqi,
        action: t.action,
        team: null,
        evidence: t.evidence,
        proxyOnly: false,
      }));
  }, [srcQ.data, tops.data]);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const target = sorted.find((t) => t.key === selectedId) ?? sorted[0];

  // Which teams the queue sends out, and how often - the "one glance" summary.
  const teamMix = useMemo(() => {
    const counts = new Map<string, number>();
    for (const t of sorted) {
      if (!t.team) continue;
      counts.set(t.team, (counts.get(t.team) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [sorted]);

  // Search runs over the dispatch queue itself - by source name, ward or type.
  // When a real ward exists but has no ranked source, say so rather than showing
  // an empty list.
  const q = query.trim().toLowerCase();
  const queue = q
    ? sorted.filter(
        (t) =>
          t.title.toLowerCase().includes(q) ||
          (t.ward ?? "").toLowerCase().includes(q) ||
          (t.kind ?? "").toLowerCase().includes(q),
      )
    : sorted;
  const offQueueMatches =
    q && queue.length === 0 && wards.isSuccess
      ? wards.data.wards.filter((w) => w.name.toLowerCase().includes(q)).slice(0, 3)
      : [];

  // The wards the ranked sources sit in. These are painted solid red on the map so
  // "where do we go first" is answerable from across the room - the same red the
  // DISPATCH rows carry in the list beside it.
  // One badge per ward, carrying the BEST rank sitting in it - two sources in the
  // same ward would otherwise stack two numbers on the same centroid.
  const rankBadges = useMemo(() => {
    const best = new Map<string, number>();
    for (const t of sorted) {
      const n = (t.wardNo ?? "").replace(/\.0$/, "").replace(/ /g, "_");
      if (!n) continue;
      const id = `W${n}`;
      if (!best.has(id) || t.rank < best.get(id)!) best.set(id, t.rank);
    }
    return [...best.entries()]
      .sort((a, b) => a[1] - b[1])
      .slice(0, 10)
      .map(([id, rank]) => ({ id, label: String(rank) }));
  }, [sorted]);

  const dispatchWardIds = useMemo(
    () => [
      ...new Set(
        sorted
          .map((t) => (t.wardNo ?? "").replace(/\.0$/, "").replace(/ /g, "_"))
          .filter(Boolean)
          .map((n) => `W${n}`),
      ),
    ],
    [sorted],
  );

  return (
    <AppShell>
      {/* Viewport-locked only from md up. On a phone the document scrolls, so the
          dispatch queue below the map is reachable instead of being clipped. */}
      <div className="grid grid-cols-1 md:h-[calc(100vh-57px)] md:grid-cols-[1fr_500px] xl:grid-cols-[1fr_560px]">
        <section className="relative h-[52vh] min-h-[300px] overflow-hidden border-b border-border bg-bg-secondary md:sticky md:top-0 md:h-[calc(100vh-57px)] md:min-h-0 md:border-b-0 md:border-r">
          {sorted.length > 0 && wards.isSuccess ? (
            <>
              {/* The map answers the same question as the list beside it: where do
                  we go first. Wards holding a ranked source are painted solid red -
                  a dispatch overlay, not a CPCB band - and numbered with that
                  source's rank. */}
              <DelhiWardMap
                liveWards={wards.data.wards}
                horizon="24"
                urgentIds={dispatchWardIds}
                onPick={(w) => setQuery(w.name)}
                badges={rankBadges}
                className="p-2"
              />
              <div className="pointer-events-none absolute left-4 top-4 flex flex-col items-start gap-2">
                <div className="chip">Real Delhi wards · numbers = dispatch rank · click a ward to find it in the queue</div>
                <div className="pointer-events-auto inline-flex items-center gap-1.5 rounded-full border border-border bg-panel px-2.5 py-1">
                  <span
                    className="inline-block h-2.5 w-2.5 rounded-[2px]"
                    style={{ background: "var(--aqi-very-poor)" }}
                  />
                  <span className="mono text-[11px] text-text-dim">
                    {dispatchWardIds.length} wards with a ranked source - dispatch first
                  </span>
                </div>
              </div>
            </>
          ) : (
            // Fallback illustration only, shown while the live ward feed is
            // unavailable. Nothing is selected in it: the queue beside it is now
            // driven by real pipeline targets, which have no synthetic-cell twin.
            <MapView layers={{ enforcement: true, windCorridor: true, fires: false }} />
          )}
        </section>

        <aside className="bg-panel md:h-[calc(100vh-57px)] md:overflow-y-auto">
          <div className="border-b border-border p-5">
            <div className="chip mb-3">
              {usingSources ? "Ranked sources · live pipeline" : "Enforcement queue"}
            </div>
            <h1 className="text-xl font-bold">Where to send inspectors first</h1>
            <p className="mono mt-1 text-[11px] text-text-mute">
              {usingSources
                ? `${sorted.length} ranked sources across ${dispatchWardIds.length} wards · dispatchable targets, not grid cells`
                : `${sorted.length} pipeline targets · ranked by fused priority score`}
            </p>
            {teamMix.length > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {teamMix.map(([team, n]) => (
                  <span key={team} className="mono rounded-full bg-surface-1 px-2.5 py-1 text-[11px] text-text-dim">
                    {team} <b className="text-foreground">×{n}</b>
                  </span>
                ))}
              </div>
            )}
            {sorted.length > 0 && (
              <div className="mt-3">
                <label htmlFor="enf-ward-search" className="sr-only">Search the dispatch queue by source, ward or type</label>
                <input
                  id="enf-ward-search"
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Find a source, ward or type - Bawana, road, industry…"
                  className="w-full rounded-full border border-border bg-panel px-3.5 py-2 text-[12.5px] text-foreground placeholder:text-text-mute focus:border-accent-dim focus:outline-none focus:ring-2 focus:ring-[color:var(--accent-glow)]"
                />
              </div>
            )}
            <button
              onClick={() => setShowMethod((s) => !s)}
              aria-expanded={showMethod}
              className={`mt-3 rounded-full border px-3 py-1.5 text-[12px] font-semibold transition-colors ${
                showMethod
                  ? "border-accent bg-accent text-white"
                  : "border-border text-text-dim hover:border-accent-dim hover:text-accent"
              }`}
            >
              {showMethod ? "Hide the method" : "How is this ranked?"}
            </button>
            {showMethod && (
              <div className="mt-4 border-t border-border pt-4">
                <MethodPanel method="enforcement" compact />
              </div>
            )}
          </div>

          {q && queue.length === 0 && (
            <div className="border-b border-border px-5 py-4">
              {offQueueMatches.length > 0 ? (
                <div>
                  {offQueueMatches.map((w) => {
                    const cat = aqiCategory(w.aqi);
                    return (
                      <div key={w.zone_id} className="mb-2 flex items-center justify-between gap-2">
                        <span className="text-sm font-semibold">{w.name}</span>
                        <span
                          className="mono shrink-0 rounded-md px-2 py-0.5 text-[11px] font-bold"
                          style={{ background: cat.color, color: cat.text }}
                        >
                          {w.aqi}
                        </span>
                      </div>
                    );
                  })}
                  <p className="text-[12.5px] text-text-dim">
                    Real ward, but no ranked source in it this run - inspection capacity goes to
                    wards with an identified source first. See its forecast on the dashboard's
                    ward finder.
                  </p>
                </div>
              ) : (
                <p className="mono text-[11px] text-text-mute">
                  Nothing in the queue matches “{query.trim()}”.
                </p>
              )}
            </div>
          )}

          {/* Two views, both useful, in the order a team would use them.
              The ranked sources are the tactical list - a specific junction, a
              specific site, with a team and an action. The ward plan below is the
              strategic one: which wards to cover. Previously the sources only
              rendered when the ward plan was unavailable, which meant the more
              actionable list never appeared at all. */}
          {usingSources && (
            <div className="border-b-4 border-border">
              <div className="flex items-baseline justify-between gap-2 bg-surface-1 px-5 py-2">
                <span className="mono text-[11px] font-bold text-foreground">
                  RANKED SOURCES · dispatch first
                </span>
                <span className="mono text-[10px] text-text-mute">
                  {sorted.length} targets
                </span>
              </div>
              <ul>
                {sorted.map((t) => {
                  const active = t.key === (target?.key ?? "");
                  // The queue exists to send people somewhere. The rows that need a
                  // van today are marked in red rather than left for the reader to
                  // work out from a priority number: top of the queue, or feeding a
                  // ward already past the CPCB "Moderate" ceiling.
                  const urgent = t.rank <= 5 || t.aqi >= 150;
                  return (
                    <li key={t.key}>
                      <button
                        onClick={() => setSelectedId(t.key)}
                        className={`block w-full border-b border-border px-5 py-4 text-left transition-colors ${
                          urgent ? "border-l-4 border-l-[var(--aqi-very-poor)] " : ""
                        }${active ? "bg-surface-1" : "hover:bg-surface-1/40"}`}
                        style={urgent && !active ? { background: "color-mix(in srgb, var(--aqi-very-poor) 7%, transparent)" } : undefined}
                      >
                        <div className="flex items-baseline justify-between gap-2">
                          <span className={`text-sm font-semibold ${active ? "text-accent" : "text-foreground"}`}>
                            {urgent && (
                              <span
                                className="mono mr-1.5 rounded px-1.5 py-0.5 text-[10px] font-bold align-middle"
                                style={{ background: "var(--aqi-very-poor)", color: "#fff" }}
                              >
                                DISPATCH
                              </span>
                            )}
                            {t.title}
                          </span>
                          <span className="mono shrink-0 text-xs text-accent">
                            P{Math.round(t.priority)}
                          </span>
                        </div>
                        <div className="mono mt-1 flex flex-wrap gap-x-3 text-[11px] text-text-mute">
                          <span>#{t.rank}</span>
                          <span>·</span>
                          <span>{t.kind}</span>
                          {t.ward && (<><span>·</span><span>{t.ward}</span></>)}
                          <span>·</span>
                          <span>AQI {Math.round(t.aqi)}</span>
                          {t.proxyOnly && (
                            <>
                              <span>·</span>
                              <span title="Presence and proximity, not measured emissions">proxy</span>
                            </>
                          )}
                        </div>
                        {t.team && (
                          <div className="mono mt-1 text-[11px] text-accent">{t.team}</div>
                        )}
                        <div className="mt-2 text-[12.5px] text-text-dim">{t.action}</div>
                        {t.evidence && (
                          <div className="mono mt-1 text-[11px] text-text-mute">{t.evidence}</div>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
              {srcQ.data?.caveat && (
                <p className="mono border-b border-border px-5 py-3 text-[11px] text-text-mute">
                  {srcQ.data.caveat}
                </p>
              )}
            </div>
          )}

          {/* The ward-coverage list is gone: it ranked WARDS, which nobody can be
              dispatched to, and it duplicated the map. What survives is the
              source queue above - a named road, a specific site - and this cell
              fallback for when the source feed is unavailable. */}
          {!usingSources && (
            <ul>
              {sorted.map((t) => {
                const active = t.key === (target?.key ?? "");
                return (
                  <li key={t.key}>
                    <button
                      onClick={() => setSelectedId(t.key)}
                      className={`block w-full border-b border-border px-5 py-4 text-left transition-colors ${
                        active ? "bg-surface-1" : "hover:bg-surface-1/40"
                      }`}
                    >
                      <div className="flex items-baseline justify-between">
                        <span className={`text-sm font-semibold ${active ? "text-accent" : "text-foreground"}`}>
                          {t.title}
                        </span>
                        <span className="mono text-xs text-accent">
                          P{Math.round(t.priority)}
                        </span>
                      </div>
                      <div className="mono mt-1 flex flex-wrap gap-x-3 text-[11px] text-text-mute">
                        <span>#{t.rank}</span>
                        <span>·</span>
                        <span>{t.kind}</span>
                        {t.ward && (<><span>·</span><span>{t.ward}</span></>)}
                        <span>·</span>
                        <span>AQI {Math.round(t.aqi)}</span>
                        {t.proxyOnly && (
                          <>
                            <span>·</span>
                            <span title="Presence and proximity, not measured emissions">
                              proxy
                            </span>
                          </>
                        )}
                      </div>
                      {t.team && (
                        <div className="mono mt-1 text-[11px] text-accent">{t.team}</div>
                      )}
                      <div className="mt-2 text-[12.5px] text-text-dim">{t.action}</div>
                      {t.evidence && (
                        <div className="mono mt-1 text-[11px] text-text-mute">{t.evidence}</div>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>
      </div>
    </AppShell>
  );
}
