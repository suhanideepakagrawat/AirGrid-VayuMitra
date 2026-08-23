import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { DelhiWardMap } from "@/components/DelhiWardMap";
import { MapView } from "@/components/MapView";
import { MethodPanel } from "@/components/HowItWorks";
import { aqiCategory, CELLS, type Cell } from "@/lib/air-data";
import {
  deploymentQuery,
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
  const dep = useQuery(deploymentQuery);
  const wards = useQuery(wardsQuery());
  const live = dep.isSuccess && dep.data.available && dep.data.items.length > 0;
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

  // Which teams the plan sends out, and how often - the "one glance" summary.
  const teamMix = useMemo(() => {
    if (!live) return [];
    const counts = new Map<string, number>();
    for (const w of dep.data!.items) {
      if (!w.recommended_team) continue;
      counts.set(w.recommended_team, (counts.get(w.recommended_team) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [live, dep.data]);

  const maxScore = live
    ? Math.max(...dep.data!.items.map((w) => w.deployment_score ?? 0), 1)
    : 1;

  // Ward search over the live queue; when a real ward exists but isn't in the
  // top-30 plan, say so honestly instead of showing an empty list.
  const q = query.trim().toLowerCase();
  const queue = live
    ? dep.data!.items.filter((w) => !q || w.ward_name?.toLowerCase().includes(q))
    : [];
  const offQueueMatches =
    live && q && queue.length === 0 && wards.isSuccess
      ? wards.data.wards.filter((w) => w.name.toLowerCase().includes(q)).slice(0, 3)
      : [];

  return (
    <AppShell>
      {/* Viewport-locked only from md up. On a phone the document scrolls, so the
          dispatch queue below the map is reachable instead of being clipped. */}
      <div className="grid grid-cols-1 md:h-[calc(100vh-57px)] md:grid-cols-[1fr_500px] xl:grid-cols-[1fr_560px]">
        <section className="relative h-[52vh] min-h-[300px] overflow-hidden border-b border-border bg-bg-secondary md:sticky md:top-0 md:h-[calc(100vh-57px)] md:min-h-0 md:border-b-0 md:border-r">
          {live && wards.isSuccess ? (
            <>
              <DelhiWardMap
                liveWards={wards.data.wards}
                horizon="24"
                onPick={(w) => setQuery(w.name)}
                badges={dep.data!.items.slice(0, 10).map((w) => ({
                  id: `W${(w.ward_no ?? "").replace(/\.0$/, "").replace(/ /g, "_")}`,
                  label: String(w.rank),
                }))}
                className="p-2"
              />
              <div className="pointer-events-none absolute left-4 top-4">
                <div className="chip">Real Delhi wards · numbers = deployment rank · click a ward to find it in the queue</div>
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
              {live ? "Ward deployment plan · live pipeline" : "Enforcement queue"}
            </div>
            <h1 className="text-xl font-bold">Where to send inspectors first</h1>
            <p className="mono mt-1 text-[11px] text-text-mute">
              {live
                ? `${dep.data.items.length} wards ranked by deployment score (severity × source × persistence)`
                : usingSources
                  ? `${sorted.length} ranked sources · dispatchable targets, not grid cells`
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
            {live && (
              <div className="mt-3">
                <label htmlFor="enf-ward-search" className="sr-only">Search the deployment queue by ward</label>
                <input
                  id="enf-ward-search"
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Find a ward in the queue - Chhawla, Bawana…"
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

          {live && queue.length === 0 && (
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
                    Real ward, but not in today's top-30 deployment queue - inspection capacity
                    goes to worse wards first. See its forecast on the dashboard's ward finder.
                  </p>
                </div>
              ) : (
                <p className="mono text-[11px] text-text-mute">
                  No ward matches “{query.trim()}”.
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
                          urgent ? "border-l-4 border-l-[var(--aqi-poor,#ff5a4e)] " : ""
                        }${active ? "bg-surface-1" : "hover:bg-surface-1/40"}`}
                        style={urgent && !active ? { background: "color-mix(in srgb, var(--aqi-poor, #ff5a4e) 7%, transparent)" } : undefined}
                      >
                        <div className="flex items-baseline justify-between gap-2">
                          <span className={`text-sm font-semibold ${active ? "text-accent" : "text-foreground"}`}>
                            {urgent && (
                              <span
                                className="mono mr-1.5 rounded px-1.5 py-0.5 text-[10px] font-bold align-middle"
                                style={{ background: "var(--aqi-poor, #ff5a4e)", color: "#fff" }}
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

          {live && (
            <div className="bg-surface-1 px-5 py-2">
              <span className="mono text-[11px] font-bold text-foreground">
                WARD DEPLOYMENT PLAN · coverage
              </span>
            </div>
          )}

          {live ? (
            <ul>
              {queue.map((w) => {
                const cat = aqiCategory(w.max_aqi ?? 0);
                // Same rule as the source queue, so "red" means one thing on this
                // page: go here first.
                const urgent = w.rank <= 5 || (w.max_aqi ?? 0) >= 150;
                return (
                  <li
                    key={`${w.rank}-${w.ward_no}`}
                    className={`border-b border-border px-5 py-4 ${
                      urgent ? "border-l-4 border-l-[var(--aqi-poor,#ff5a4e)]" : ""
                    }`}
                    style={urgent ? { background: "color-mix(in srgb, var(--aqi-poor, #ff5a4e) 7%, transparent)" } : undefined}
                  >
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-sm font-semibold">
                        <span className="mono mr-2 text-text-mute">#{w.rank}</span>
                        {w.ward_name}
                      </span>
                      <span
                        className="mono shrink-0 rounded-md px-2 py-0.5 text-[12.5px] font-bold"
                        style={{ background: cat.color, color: cat.text }}
                        title={`Peak forecast AQI · ${cat.label}`}
                      >
                        {Math.round(w.max_aqi ?? 0)}
                      </span>
                    </div>
                    <div className="mono mt-1 flex flex-wrap gap-x-3 text-[11px] text-text-mute">
                      <span>Ward {w.ward_no}</span>
                      <span>·</span>
                      <span>{w.hotspots} hotspot cells</span>
                      <span>·</span>
                      <span>score {Math.round(w.deployment_score ?? 0)}</span>
                    </div>
                    <div
                      className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-1"
                      role="img"
                      aria-label={`Deployment score ${Math.round(w.deployment_score ?? 0)} of ${Math.round(maxScore)}`}
                    >
                      <div
                        className="h-full rounded-full bg-accent transition-all duration-300"
                        style={{ width: `${((w.deployment_score ?? 0) / maxScore) * 100}%` }}
                      />
                    </div>
                    <div className="mt-2 flex flex-wrap items-center gap-2 text-[12.5px] text-text-dim">
                      {w.dominant_source && (
                        <span className="rounded-md bg-surface-1 px-2 py-0.5">{w.dominant_source}</span>
                      )}
                      {w.recommended_team && (
                        <span className="rounded-md bg-surface-1 px-2 py-0.5">→ {w.recommended_team}</span>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : usingSources ? null : (
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
