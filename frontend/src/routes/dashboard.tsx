import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { DelhiWardMap } from "@/components/DelhiWardMap";
import { MapView } from "@/components/MapView";
import {
  AqiBall,
  BandBadge,
  BandDistribution,
  DeltaTag,
  HorizonSwitch,
  HorizonTriplet,
  HORIZON_LABEL,
  SourceStrip,
} from "@/components/charts";
import { MethodPanel } from "@/components/HowItWorks";
import { YourLocationBanner } from "@/components/YourLocationBanner";
import { DataFreshness } from "@/components/DataFreshness";
import { liveQuery, timeAgo, type LiveNowWard } from "@/lib/api";
import { useMyWard } from "@/lib/locate";
import {
  aqiCategory,
  CELLS,
  cellAqi,
  HORIZONS,
  SOURCE_COLORS,
  SOURCE_EVIDENCE,
  SOURCE_LABELS,
  type Cell,
  type Horizon,
  type HorizonSel,
  asForecast,
  isNow,
  type SourceKey,
} from "@/lib/air-data";
import {
  deploymentQuery,
  wardAqiAt,
  wardsQuery,
  type DeploymentRow,
  type LiveWard,
} from "@/lib/api";

export const Route = createFileRoute("/dashboard")({
  head: () => ({
    meta: [
      { title: "Dashboard - AirGrid NCR" },
      { name: "description", content: "Live map-first view of Delhi-NCR air quality, source attribution, and enforcement targets." },
    ],
  }),
  component: Dashboard,
});

// What the detail panel is focused on: a real live ward (picked by name) or a
// sample-scene grid cell (clicked on the map).
type Selection = { kind: "ward"; ward: LiveWard } | { kind: "cell"; cell: Cell };

const LIVE_SOURCE_META: Record<string, { label: string; color: string }> = {
  traffic: { label: "Traffic", color: "var(--source-traffic)" },
  industry: { label: "Industry", color: "var(--source-industry)" },
  construction: { label: "Construction dust", color: "var(--source-construction)" },
};

/**
 * Live AQI per ward, keyed by zone_id.
 *
 * Shared by every panel so "Now" means one thing across the page. Kept as a hook
 * rather than drilled through props: the pulse strip, the ward list and the ward
 * detail all need it, and react-query dedupes the fetch anyway.
 */
function useLiveAqiMap() {
  const q = useQuery(liveQuery);
  return useMemo(() => {
    const m = new Map<string, number>();
    if (q.data?.available) for (const w of q.data.wards) m.set(w.zone_id, w.aqi);
    return m;
  }, [q.data]);
}

/** AQI for a ward at the selected horizon. "now" uses the measured reading and
 *  falls back to +24 h only when a ward has no live station coverage. */
function aqiForSel(w: LiveWard, sel: HorizonSel, live: Map<string, number>): number {
  if (sel === "now") {
    const v = live.get(w.zone_id);
    if (typeof v === "number") return v;
  }
  return wardAqiAt(w, asForecast(sel));
}

function Dashboard() {
  const [sel, setSel] = useState<Selection | null>(null);
  const [horizon, setHorizon] = useState<HorizonSel>("now");
  const liveAqi = useLiveAqiMap();

  const [showMethod, setShowMethod] = useState(false);
  const [mapMode, setMapMode] = useState<"wards" | "grid">("wards");
  const [sourceFilter, setSourceFilter] = useState<SourceKey | "all">("all");
  const [layers, setLayers] = useState({
    windCorridor: true,
    fires: true,
    wards: false,
    enforcement: true,
    vulnerable: false,
  });

  const live = useQuery(wardsQuery());
  const liveWards: LiveWard[] | null =
    live.isSuccess && live.data.wards.length > 0 ? live.data.wards : null;
  // The ten worst wards at the selected horizon, ringed red on the map so the
  // question "where do we go first" is answerable at a glance rather than by
  // reading a list.
  const urgentWardIds = useMemo(() => {
    if (!liveWards) return [];
    return [...liveWards]
      .sort((a, b) => aqiForSel(b, horizon, liveAqi) - aqiForSel(a, horizon, liveAqi))
      .slice(0, 10)
      .map((w) => w.zone_id);
  }, [liveWards, horizon, liveAqi]);
  const deployment = useQuery(deploymentQuery);
  const deployRows: DeploymentRow[] =
    deployment.isSuccess && deployment.data.available ? deployment.data.items : [];

  // Geolocation -> the user's own ward. The dashboard asks on load (the
  // permission prompt is the ask); on success the ward becomes the focused
  // ward and the banner takes its band color. Search/map still change it.
  const my = useMyWard((zone) => setSel({ kind: "ward", ward: zone }), { auto: "always" });

  // Default focus: the worst live ward (a real place with real numbers), or
  // the sample cell when the API is unreachable.
  const active: Selection | null =
    sel ??
    (liveWards
      ? { kind: "ward", ward: liveWards[0] }
      : (() => {
          const cell = CELLS.find((c) => c.id === "c-5-2");
          return cell ? { kind: "cell", cell } : null;
        })());

  return (
    <AppShell>
      {/* Locked to the viewport from md up; below that the page scrolls normally,
          because pinning a phone to 100vh made the ward detail unreachable.

          This used to also require a viewport 820px tall, on the theory that a
          short window needed the document to scroll. That was wrong twice over: a
          1080p laptop at Windows 125% scaling reports ~756 CSS px, so the lock
          almost never engaged on real hardware - and when the document scrolled,
          the map and the rail slid away above a tall sidebar, leaving the blank
          band below them. Each pane owns its own scrollbar instead, so nothing
          is unreachable and nothing scrolls into emptiness. */}
      <div className="flex flex-col pb-16 md:h-[calc(100vh-57px)] md:overflow-hidden md:pb-0">
        {/* Provenance first: the live reading and the forecast run are different
            kinds of number, and the page says so before showing either. */}
        <DataFreshness className="mx-4 mt-3" />
        <PulseStrip
          horizon={horizon}
          onHorizon={setHorizon}
          liveWards={liveWards}
          liveAqi={liveAqi}
          dataKind={live.isSuccess ? live.data.data_kind : null}
          showMethod={showMethod}
          onToggleMethod={() => setShowMethod((s) => !s)}
        />
        <YourLocationBanner my={my} horizon={horizon} />
        {showMethod && (
          <div className="border-b border-border bg-bg-primary px-5 py-4">
            <MethodPanel method="forecast" />
          </div>
        )}

        <div className="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[260px_1fr] md:overflow-hidden">
          {/* Sidebar */}
          {/* Visible on phones as well: hiding the ward list below md removed the
              only way to find your own ward on the device most judges will use. */}
          <aside className="order-2 max-h-[70vh] overflow-y-auto overscroll-contain border-b border-border bg-bg-secondary md:order-none md:max-h-none md:min-h-0 md:border-b-0 md:border-r">
            <WardFinder
              horizon={horizon}
              liveWards={liveWards}
              pending={live.isPending}
              activeWardId={active?.kind === "ward" ? active.ward.zone_id : null}
              onPick={(w) => setSel({ kind: "ward", ward: w })}
            />
            <FilterSection title="Layers">
              {[
                ["windCorridor", "Wind corridor"],
                ["fires", "Fire hotspots"],
                ["wards", "Ward outlines"],
                ["enforcement", "Enforcement pins"],
                ["vulnerable", "Sensitive sites"],
              ].map(([k, label]) => (
                <ToggleRow
                  key={k}
                  label={label}
                  on={layers[k as keyof typeof layers]}
                  onChange={(v) => setLayers((s) => ({ ...s, [k]: v }))}
                />
              ))}
            </FilterSection>

            <FilterSection title="Source filter">
              <button
                onClick={() => setSourceFilter("all")}
                className={`mono w-full px-2 py-1.5 text-left text-[11px] ${
                  sourceFilter === "all" ? "bg-surface-1 text-accent" : "text-text-dim hover:text-foreground"
                }`}
              >
                All sources
              </button>
              {(Object.keys(SOURCE_LABELS) as SourceKey[]).map((k) => (
                <button
                  key={k}
                  onClick={() => setSourceFilter((s) => (s === k ? "all" : k))}
                  className={`flex w-full items-center gap-2 px-2 py-1.5 text-left text-[11px] ${
                    sourceFilter === k ? "bg-surface-1" : "hover:bg-surface-1/50"
                  }`}
                >
                  <span className="h-2 w-2 shrink-0" style={{ background: SOURCE_COLORS[k] }} />
                  <span className="mono" style={{ color: sourceFilter === k ? "var(--accent)" : "var(--text-dim)" }}>
                    {SOURCE_LABELS[k]}
                  </span>
                </button>
              ))}
            </FilterSection>

            <FilterSection title="Legend">
              <div className="mono space-y-1.5 text-[11px] text-text-mute">
                <div className="flex items-center justify-between"><span>Forecast load</span><span>Intensity</span></div>
                <div className="flex h-2 w-full bg-gradient-to-r from-[color:var(--accent-dim)]/15 to-accent" />
                <div className="flex items-center justify-between pt-1"><span>Cleaner</span><span>Worse</span></div>
              </div>
              <div className="mt-4 mono space-y-1.5 text-[11px] text-text-mute">
                <div>Marker shapes</div>
                <div className="flex items-center gap-2 text-text-dim"><span className="inline-block h-2 w-2 rotate-45 border border-accent" /> Sensitive site</div>
                <div className="flex items-center gap-2 text-text-dim">
                  <svg width="10" height="10"><polygon points="5,0 0,10 10,10" fill="var(--accent)" /></svg>
                  Enforcement target
                </div>
              </div>
            </FilterSection>
          </aside>

          {/* Map + detail */}
          <section className="grid min-h-0 grid-rows-[auto_auto] md:grid-rows-[1fr_auto] md:overflow-hidden xl:grid-cols-[minmax(0,1fr)_460px] xl:grid-rows-1 2xl:grid-cols-[minmax(0,1fr)_680px]">
            {/* Delhi is a PORTRAIT shape in a landscape slot, so the SVG always fits by
                height and leaves empty bands either side. Stacking the detail row under
                the map therefore wastes the worst dimension: the map loses height it
                needs and gains width it cannot use. From 2xl up the detail moves into a
                right rail instead, which both fills the empty band and roughly doubles
                the map. Below xl the detail stays stacked under it. */}
            <div className="relative h-[52vh] min-h-[300px] overflow-hidden border-b border-border bg-bg-secondary md:h-full md:min-h-0 xl:min-w-0 xl:border-b-0 xl:border-r">
              {mapMode === "wards" && liveWards ? (
                <DelhiWardMap
                  liveWards={liveWards}
                  horizon={horizon}
                  liveAqi={liveAqi}
                  urgentIds={urgentWardIds}
                  selectedId={active?.kind === "ward" ? active.ward.zone_id : null}
                  hereId={my.status === "found" ? my.zone?.zone_id ?? null : null}
                  onPick={(w) => setSel({ kind: "ward", ward: w })}
                  className="p-2"
                />
              ) : (
                <MapView
                  selectedId={active?.kind === "cell" ? active.cell.id : undefined}
                  onSelect={(cell) => setSel({ kind: "cell", cell })}
                  layers={layers}
                  sourceFilter={sourceFilter}
                  horizon={asForecast(horizon)}
                />
              )}
              <div className="pointer-events-none absolute left-4 top-4 flex flex-col gap-2">
                {liveWards && (
                  <div className="pointer-events-auto inline-flex overflow-hidden rounded-full border border-border bg-panel p-0.5">
                    {(
                      [
                        ["wards", "Delhi wards · real"],
                        ["grid", "Model grid · sample"],
                      ] as const
                    ).map(([mode, label]) => (
                      <button
                        key={mode}
                        onClick={() => setMapMode(mode)}
                        aria-pressed={mapMode === mode}
                        className={`rounded-full px-3 py-1 text-[11px] font-semibold transition-colors ${
                          mapMode === mode ? "bg-accent text-white" : "text-text-dim hover:bg-surface-1 hover:text-foreground"
                        }`}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* Scrolls within itself on desktop; on a phone it simply flows, so
                nothing is hidden below the fold. 46vh on a short laptop window was
                barely two rows. */}
            <div className="md:max-h-[52vh] md:overflow-y-auto xl:h-full xl:max-h-none xl:overflow-y-auto xl:bg-bg-secondary xl:[&_.detail-grid>*]:p-3.5 xl:[&_.detail-grid_.mix-legend]:mt-2">
              {active?.kind === "ward" ? (
                <WardDetail
                  ward={active.ward}
                  horizon={horizon}
                  onHorizon={setHorizon}
                  deployRows={deployRows}
                />
              ) : (
                <CellDetail cell={active?.cell ?? null} horizon={asForecast(horizon)} onHorizon={setHorizon} />
              )}
            </div>
          </section>
        </div>
      </div>
    </AppShell>
  );
}

/* ------------------------------------------------------------------ */
/* City pulse - the strip where the horizon control lives              */
/* ------------------------------------------------------------------ */

function PulseStrip({
  horizon,
  onHorizon,
  liveWards,
  liveAqi,
  dataKind,
  showMethod,
  onToggleMethod,
}: {
  horizon: HorizonSel;
  onHorizon: (h: HorizonSel) => void;
  liveWards: LiveWard[] | null;
  /** Measured AQI per ward. Without it the strip fell back to +24 h and reported
   *  a forecast average under a "measured now" caption - the page showed two
   *  different numbers for the same instant (62 beside 141). */
  liveAqi: Map<string, number>;
  dataKind: string | null;
  showMethod: boolean;
  onToggleMethod: () => void;
}) {
  const stats = useMemo(() => {
    if (liveWards) {
      const perHorizon = Object.fromEntries(
        HORIZONS.map((h) => [h, liveWards.map((w) => wardAqiAt(w, h))]),
      ) as Record<Horizon, number[]>;
      const mean = (xs: number[]) => Math.round(xs.reduce((a, b) => a + b, 0) / (xs.length || 1));
      // "Now" is a measurement, not the +24 h forecast. Reading it off the same
      // map every other panel uses is what keeps the average, the worst ward, the
      // band census and the map itself telling one story.
      const measured = isNow(horizon) && liveAqi.size > 0;
      const aqis = measured
        ? liveWards.map((w) => aqiForSel(w, "now", liveAqi))
        : perHorizon[asForecast(horizon)];
      const worstIdx = aqis.indexOf(Math.max(...aqis));
      return {
        aqis,
        avg: mean(aqis),
        avg24: mean(perHorizon["24"]),
        worstName: liveWards[worstIdx]?.name ?? "-",
        worstAqi: aqis[worstIdx] ?? 0,
        unit: "wards",
        count: liveWards.length,
        measured,
      };
    }
    const aqis = CELLS.map((c) => cellAqi(c, asForecast(horizon)));
    const aqis24 = CELLS.map((c) => c.aqi);
    const mean = (xs: number[]) => Math.round(xs.reduce((a, b) => a + b, 0) / xs.length);
    const worstIdx = aqis.indexOf(Math.max(...aqis));
    return {
      aqis,
      avg: mean(aqis),
      avg24: mean(aqis24),
      worstName: CELLS[worstIdx]?.ward ?? "-",
      worstAqi: aqis[worstIdx] ?? 0,
      unit: "cells",
      count: CELLS.length,
      measured: false,
    };
  }, [liveWards, horizon, liveAqi]);

  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-3 border-b border-border bg-panel px-5 py-3">
      <HorizonSwitch value={horizon} onChange={onHorizon} />

      <div className="flex items-center gap-3">
        <AqiBall aqi={stats.avg} size={46} />
        <div>
          <div className="flex items-center gap-2">
            <span className="text-[14px] font-bold">Delhi average</span>
            <BandBadge aqi={stats.avg} />
          </div>
          <DeltaTag now={stats.avg} base={stats.avg24} />
        </div>
      </div>

      <div className="hidden items-center gap-2 lg:flex">
        <span className="mono text-[11px] text-text-mute">Worst {stats.unit.slice(0, -1)}</span>
        <span className="text-[12.5px] font-bold">{stats.worstName}</span>
        <span
          className="mono rounded-md px-1.5 py-0.5 text-[11px] font-bold"
          style={{ background: aqiCategory(stats.worstAqi).color, color: aqiCategory(stats.worstAqi).text }}
        >
          {stats.worstAqi}
        </span>
      </div>

      <div className="hidden min-w-[220px] max-w-[340px] flex-1 xl:block">
        <BandDistribution
          aqis={stats.aqis}
          caption={`${stats.count} ${stats.unit} · ${dataKind === "real" ? "real pipeline forecast" : dataKind === "mock" ? "pipeline sample" : "sample scene"} · ${stats.measured ? "measured now" : isNow(horizon) ? "+24 h forecast · live feed down" : `+${horizon} h`}`}
        />
      </div>

      <button
        onClick={onToggleMethod}
        aria-expanded={showMethod}
        className={`ml-auto rounded-full border px-3.5 py-1.5 text-[12px] font-semibold transition-colors ${
          showMethod
            ? "border-accent bg-accent text-white"
            : "border-border text-text-dim hover:border-accent-dim hover:text-accent"
        }`}
      >
        {showMethod ? "Hide the method" : "How is this predicted?"}
      </button>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Ward finder - search all 209 real wards; worst-first when idle      */
/* ------------------------------------------------------------------ */

function WardFinder({
  horizon,
  liveWards,
  pending,
  activeWardId,
  onPick,
}: {
  horizon: HorizonSel;
  liveWards: LiveWard[] | null;
  pending: boolean;
  activeWardId: string | null;
  onPick: (w: LiveWard) => void;
}) {
  const [query, setQuery] = useState("");

  const liveById = useLiveAqiMap();

  const rows = useMemo(() => {
    if (!liveWards) return null;
    const q = query.trim().toLowerCase();
    const pool = q
      ? liveWards.filter((w) => w.name.toLowerCase().includes(q))
      : [...liveWards].sort((a, b) => aqiForSel(b, horizon, liveById) - aqiForSel(a, horizon, liveById));
    return pool.slice(0, q ? 12 : 15);
  }, [liveWards, query, horizon, liveById]);

  const title = !rows
    ? "Ward feed"
    : query.trim()
      ? `Matches · ${rows.length}${rows.length === 12 ? "+" : ""}`
      : liveById.size === 0
        // The station feed is down, so these rows are forecast values. Saying
        // "measured now" over them is the one thing this page must never do.
        ? `Worst wards · +${isNow(horizon) ? "24" : horizon} h forecast · live feed down`
        : isNow(horizon)
          ? "Worst wards · measured now"
          : `Worst wards · measured now vs +${horizon} h`;

  return (
    <div className="border-b border-border p-4">
      <label htmlFor="ward-search" className="mono mb-2 block text-[11px] text-text-mute">
        Find your ward · {liveWards ? `${liveWards.length} live` : "connecting"}
      </label>
      <input
        id="ward-search"
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Type a ward - Narela, Dabri…"
        disabled={!liveWards}
        className="mb-3 w-full rounded-full border border-border bg-panel px-3.5 py-2 text-[12.5px] text-foreground placeholder:text-text-mute focus:border-accent-dim focus:outline-none focus:ring-2 focus:ring-[color:var(--accent-glow)]"
      />
      <div className="mono mb-2 text-[11px] text-text-mute">{title}</div>
      {pending && (
        <div className="mono px-2 py-1.5 text-[11px] text-text-mute">Connecting to pipeline…</div>
      )}
      {!pending && !rows && (
        <div className="mono px-2 py-1.5 text-[11px] text-text-mute">
          API unreachable - map shows the bundled sample scene.
        </div>
      )}
      {rows && rows.length === 0 && (
        <div className="mono px-2 py-1.5 text-[11px] text-text-mute">
          No ward matches “{query.trim()}”.
        </div>
      )}
      {rows && rows.length > 0 && (
        <ul className="space-y-0.5">
          {rows.map((w) => {
            const aqi = wardAqiAt(w, asForecast(horizon));
            const cat = aqiCategory(aqi);
            const isActive = w.zone_id === activeWardId;
            return (
              <li key={w.zone_id}>
                <button
                  onClick={() => onPick(w)}
                  aria-pressed={isActive}
                  className={`flex w-full items-center justify-between gap-2 rounded-[4px] px-2 py-1.5 text-left transition-colors ${
                    isActive ? "bg-surface-1" : "hover:bg-surface-1/60"
                  }`}
                >
                  <span className={`min-w-0 truncate text-[12px] ${isActive ? "font-bold text-accent" : "text-text-dim"}`}>
                    {w.name}
                  </span>
                  <span className="flex shrink-0 items-center gap-1">
                    {/* Measured now, then the forecast. Two different kinds of
                        number, so they are shown as two chips rather than one. */}
                    {liveById.has(w.zone_id) && (
                      <span
                        className="mono rounded-md px-1.5 py-0.5 text-[11px] font-bold"
                        style={{
                          background: aqiCategory(liveById.get(w.zone_id)!).color,
                          color: aqiCategory(liveById.get(w.zone_id)!).text,
                        }}
                        title={`Measured now · ${aqiCategory(liveById.get(w.zone_id)!).label}`}
                      >
                        {liveById.get(w.zone_id)}
                      </span>
                    )}
                    <span className="text-[10px] text-text-mute" aria-hidden>→</span>
                    <span
                      className="mono rounded-md px-1.5 py-0.5 text-[11px] font-bold"
                      style={{ background: cat.color, color: cat.text }}
                      title={`Forecast +${asForecast(horizon)}h · ${cat.label}`}
                    >
                      {aqi}
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {rows && (
        <div className="mono mt-2 text-[11px] text-text-mute">
          Measured now (CPCB) → forecast +{asForecast(horizon)} h. Tap a ward for detail.
        </div>
      )}
    </div>
  );
}

function FilterSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-b border-border p-4">
      <div className="mono mb-3 text-[11px] text-text-mute">{title}</div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

function ToggleRow({ label, on, onChange }: { label: string; on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      onClick={() => onChange(!on)}
      className="flex w-full items-center justify-between px-2 py-1.5 text-left text-[12px] text-text-dim hover:text-foreground"
    >
      <span>{label}</span>
      <span className={`mono text-[11px] ${on ? "text-accent" : "text-text-mute"}`}>
        {on ? "● on" : "○ off"}
      </span>
    </button>
  );
}

/* ------------------------------------------------------------------ */
/* Shared bits for the two detail modes                                */
/* ------------------------------------------------------------------ */

// Legend rows that cannot overlap: label truncates, value never shrinks.
function MixLegend({ items }: { items: { key: string; label: string; color: string; pct: number }[] }) {
  return (
    // Two-up only when the PANEL is wide enough - a viewport breakpoint got this
    // wrong in the detail rail, where a 1440px screen still leaves each panel under
    // 200px and clipped "Construction dust" to "Constru...".
    <div className="mix-legend @container mt-3">
      <div className="mono grid grid-cols-1 gap-x-6 gap-y-1 text-[11px] @[248px]:grid-cols-2">
      {items.map((m) => (
        <div key={m.key} className="flex items-center justify-between gap-2" title={`${m.label} · ${Math.round(m.pct)}%`}>
          <span className="flex min-w-0 items-center gap-2 text-text-dim">
            <span className="h-1.5 w-1.5 shrink-0" style={{ background: m.color }} />
            <span className="min-w-0">{m.label}</span>
          </span>
          <span className="shrink-0 text-foreground">{Math.round(m.pct)}%</span>
        </div>
      ))}
      </div>
    </div>
  );
}

const detailGrid =
  "detail-grid grid grid-cols-1 gap-px bg-border sm:grid-cols-2";

/* ------------------------------------------------------------------ */
/* Ward detail - a REAL ward: live forecast, sources, deployment       */
/* ------------------------------------------------------------------ */

/**
 * What the instruments actually read for this ward in the last hour.
 *
 * Sits directly above the forecast so the two are never conflated: this is a
 * measurement, the ball beside it is a prediction. Names the contributing station
 * and its distance, because a ward 8 km from the nearest monitor deserves less
 * confidence than one sitting on top of it - and hiding that would be the dishonest
 * choice.
 */
function LiveNowBlock({ zoneId }: { zoneId: string }) {
  const live = useQuery(liveQuery);
  const row: LiveNowWard | undefined = live.data?.available
    ? live.data.wards.find((w) => w.zone_id === zoneId)
    : undefined;

  if (!row) {
    return (
      <div className="mt-4 rounded-md border border-border bg-panel p-3">
        <div className="mono text-[11px] text-text-mute">MEASURED NOW</div>
        <div className="mt-1 text-[12px] text-text-dim">
          {live.data?.state === "warming"
            ? "fetching station readings…"
            : "no live station reading for this ward"}
        </div>
      </div>
    );
  }

  const pm25 = row.pollutants?.pm25;
  const pm10 = row.pollutants?.pm10;

  return (
    <div className="mt-4 rounded-md border border-border bg-panel p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="mono text-[11px] text-text-mute">MEASURED NOW</span>
        <span className="mono text-[10px] text-text-mute">{timeAgo(row.observed_at)}</span>
      </div>
      <div className="mt-2 flex items-center gap-2">
        <span
          className="mono rounded-md px-2 py-0.5 text-[13px] font-bold"
          style={{ background: row.color, color: aqiCategory(row.aqi).text }}
        >
          {row.aqi}
        </span>
        <span className="text-[12px] text-text-dim">
          {row.band_label} · driven by {row.dominant_pollutant}
        </span>
      </div>
      {(pm25 != null || pm10 != null) && (
        <div className="mono mt-2 text-[11px] text-text-mute">
          {pm25 != null && <>PM2.5 {pm25}</>}
          {pm25 != null && pm10 != null && " · "}
          {pm10 != null && <>PM10 {pm10}</>} µg/m³
        </div>
      )}
      <div className="mono mt-1 text-[11px] text-text-mute">
        {row.nearest_station} · {row.nearest_station_km} km
        {row.n_stations > 1 && <> · {row.n_stations} stations blended</>}
      </div>
    </div>
  );
}

function WardDetail({
  ward,
  horizon,
  onHorizon,
  deployRows,
}: {
  ward: LiveWard;
  horizon: HorizonSel;
  onHorizon: (h: HorizonSel) => void;
  deployRows: DeploymentRow[];
}) {
  // react-query dedupes this against the dashboard's own subscription, so reading the
  // measured layer here costs nothing and keeps the caption from claiming "measured
  // now" when the station feed is down.
  const liveAqi = useLiveAqiMap();
  const liveById = useLiveAqiMap();
  const aqiNow = aqiForSel(ward, horizon, liveById);
  // Now first, then the forecasts - the order a reader actually wants.
  const values = {
    ...(liveById.has(ward.zone_id) ? { now: liveById.get(ward.zone_id)! } : {}),
    ...Object.fromEntries(HORIZONS.map((h) => [h, wardAqiAt(ward, h)])),
  } as Partial<Record<HorizonSel, number>>;
  const mix = ward.sources
    ? Object.entries(ward.sources).map(([k, v]) => ({
        key: k,
        label: LIVE_SOURCE_META[k]?.label ?? k,
        color: LIVE_SOURCE_META[k]?.color ?? "var(--text-mute)",
        pct: v as number,
      }))
    : [];
  const wardNo = ward.zone_id.replace(/^W/, "");
  const deploy =
    deployRows.find((d) => d.ward_name?.toLowerCase() === ward.name.toLowerCase()) ??
    deployRows.find((d) => d.ward_no?.replace(/\.0$/, "") === wardNo);

  return (
    <div className={detailGrid}>
      {/* Identity */}
      <div className="bg-bg-secondary p-5">
        <div className="mono text-[11px] text-text-mute">Ward {wardNo} · live pipeline</div>
        <div className="mt-2 text-xl font-bold">{ward.name}</div>
        <div className="mt-4 flex items-center gap-3">
          <AqiBall aqi={aqiNow} size={64} />
          <div>
            <BandBadge aqi={aqiNow} />
            <div className="mono mt-1 text-[11px] text-text-mute">CPCB band · {isNow(horizon) ? (liveAqi.size > 0 ? "measured now" : "+24 h forecast · live feed down") : `+${horizon} h`}</div>
            <div className="mt-1"><DeltaTag now={aqiNow} base={values["24"] ?? aqiNow} /></div>
          </div>
        </div>
        <LiveNowBlock zoneId={ward.zone_id} />
        <div className="mt-4 border-t border-border pt-3 mono text-[11px] text-text-mute">
          {ward.lat?.toFixed(3)}°N {ward.lon?.toFixed(3)}°E
          {ward.confidence != null && <> · confidence {Math.round(ward.confidence * 100)}%</>}
        </div>
      </div>

      {/* Real 72-hour trajectory */}
      <div className="bg-bg-secondary p-5">
        <div className="mono text-[11px] text-text-mute">72-hour forecast · trained models</div>
        <div className="mt-2">
          <HorizonTriplet values={values} active={horizon} onSelect={onHorizon} height={64} />
        </div>
        <div className="mono mt-1 text-[11px] text-text-mute">Tap a bar to switch the whole view.</div>
      </div>

      {/* Attribution */}
      <div className="bg-bg-secondary p-5">
        <div className="mono text-[11px] text-text-mute">Likely source</div>
        <div className="mt-2 text-lg font-bold">{ward.dominant_source ?? "-"}</div>
        {ward.dominant_source_pct > 0 && (
          <div className="mono mt-0.5 text-[11px] text-text-mute">
            {Math.round(ward.dominant_source_pct)}% of this ward's load
          </div>
        )}
        {mix.length > 0 && (
          <div className="mt-4">
            <div className="mono mb-2 text-[11px] text-text-mute">Source mix</div>
            <SourceStrip mix={mix} height={12} />
            <MixLegend items={mix} />
          </div>
        )}
      </div>

      {/* Enforcement cross-link */}
      <div className="bg-bg-secondary p-5">
        <div className="mono text-[11px] text-text-mute">Deployment plan</div>
        {deploy ? (
          <div className="mt-2">
            <div className="text-[14px] font-bold">
              #{deploy.rank} in today's queue
            </div>
            <div className="mono mt-1 space-y-0.5 text-[11px] text-text-mute">
              <div>{deploy.hotspots} hotspot cells · score {Math.round(deploy.deployment_score ?? 0)}</div>
              <div>peak AQI {Math.round(deploy.max_aqi ?? 0)}</div>
            </div>
            {deploy.recommended_team && (
              <div className="mt-2 inline-block rounded-md bg-surface-1 px-2 py-1 text-[12.5px] text-text-dim">
                → {deploy.recommended_team}
              </div>
            )}
          </div>
        ) : (
          <p className="mt-2 text-[12.5px] text-text-dim">
            Not in the top-30 deployment queue at this run - inspection capacity goes to
            worse wards first.
          </p>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Cell detail - sample-scene cell with evidence story                 */
/* ------------------------------------------------------------------ */

function CellDetail({
  cell,
  horizon,
  onHorizon,
}: {
  cell: Cell | null;
  horizon: Horizon;
  onHorizon: (h: HorizonSel) => void;
}) {
  if (!cell) {
    return (
      <div className="border-t border-border bg-bg-secondary px-6 py-8 text-center">
        <div className="mono text-[11px] text-text-mute">
          Select a grid cell to see attribution evidence.
        </div>
      </div>
    );
  }
  const aqiNow = cellAqi(cell, horizon);
  const values = Object.fromEntries(HORIZONS.map((h) => [h, cellAqi(cell, h)])) as Record<Horizon, number>;
  const mix = (Object.keys(SOURCE_LABELS) as SourceKey[]).map((k) => ({
    key: k,
    label: SOURCE_LABELS[k],
    color: SOURCE_COLORS[k],
    pct: cell.attribution[k] * 100,
  }));

  return (
    <div className={detailGrid}>
      {/* AQI + ward */}
      <div className="bg-bg-secondary p-5">
        <div className="mono text-[11px] text-text-mute">
          {cell.wardCode} · Cell {cell.id} · sample scene
        </div>
        <div className="mt-2 text-xl font-bold">{cell.ward}</div>
        <div className="mt-4 flex items-center gap-3">
          <AqiBall aqi={aqiNow} size={64} />
          <div>
            <BandBadge aqi={aqiNow} />
            <div className="mono mt-1 text-[11px] text-text-mute">CPCB band · +{horizon} h</div>
            <div className="mt-1"><DeltaTag now={aqiNow} base={values["24"]} /></div>
          </div>
        </div>
        <div className="mt-4 border-t border-border pt-3 mono text-[11px] text-text-mute">
          Coord · 28.{600 + cell.y * 8}°N 77.{100 + cell.x * 6}°E
        </div>
      </div>

      {/* Trajectory */}
      <div className="bg-bg-secondary p-5">
        <div className="mono text-[11px] text-text-mute">72-hour forecast</div>
        <div className="mt-2">
          <HorizonTriplet values={values} active={horizon} onSelect={onHorizon} height={64} />
        </div>
        <div className="mono mt-1 text-[11px] text-text-mute">Tap a bar to switch the whole view.</div>
      </div>

      {/* Attribution */}
      <div className="bg-bg-secondary p-5">
        <div className="flex items-baseline justify-between gap-2">
          <div className="mono text-[11px] text-text-mute">Likely source</div>
          <div className="mono shrink-0 text-[11px] text-text-mute">
            Confidence · {Math.round(cell.confidence * 100)}%
          </div>
        </div>
        <div className="mt-2 flex items-center gap-3">
          <span className="h-3 w-3 shrink-0" style={{ background: SOURCE_COLORS[cell.dominantSource] }} />
          <span className="font-display text-lg">{SOURCE_LABELS[cell.dominantSource]}</span>
        </div>
        <p className="mt-3 max-w-md text-[12.5px] text-text-dim">
          {SOURCE_EVIDENCE[cell.dominantSource]}
        </p>
        <div className="mt-4">
          <div className="mono mb-2 text-[11px] text-text-mute">Source mix</div>
          <SourceStrip mix={mix} height={12} />
          <MixLegend items={mix} />
        </div>
      </div>

      {/* Enforcement / actions */}
      <div className="bg-bg-secondary p-5">
        <div className="mono text-[11px] text-text-mute">Nearby registered sources</div>
        {/* The named targets that used to render here ("Narela Phase-III Sites",
            "Bawana Cluster Kilns") were invented, along with their priorities and
            their claimed measurements. The real ranked queue lives on /enforcement,
            resolved to actual MCD wards, so this panel points there instead of
            fabricating a local one for a sample-scene cell. */}
        <div className="mono mt-3 text-[11px] text-text-mute">
          Ranked enforcement targets for real wards are on the Enforcement page.
        </div>
      </div>
    </div>
  );
}
