// The real Delhi ward map - actual MCD boundaries from the pipeline's
// shapefile, pre-projected to SVG paths at build time (src/data/delhi-wards.json).
// Wards are filled with their CPCB band color at the active horizon, so
// switching +24/+48/+72 recolors the actual city. Click a ward to focus it.

import { useMemo, useState } from "react";
import WARD_GEO from "@/data/delhi-wards.json";
import {
  aqiCategory,
  asForecast,
  isNow,
  type HorizonSel,
} from "@/lib/air-data";
import { wardAqiAt, type LiveWard } from "@/lib/api";

type WardShape = { id: string; name: string; d: string; cx: number; cy: number };

const GEO = WARD_GEO as { w: number; h: number; wards: WardShape[] };

export function DelhiWardMap({
  liveWards,
  horizon,
  liveAqi,
  urgentIds,
  selectedId,
  hereId,
  onPick,
  badges,
  ambient = false,
  className = "",
}: {
  liveWards: LiveWard[] | null;
  horizon: HorizonSel;
  /** Measured AQI per ward. When horizon is "now" the map paints these instead of
   *  the forecast, so the control actually changes what the city looks like. */
  liveAqi?: Map<string, number>;
  /** Wards to mark as "go here first" - painted solid red so they carry across a
   *  room, which an outline does not.
   *
   *  This is a RANK overlay, and it deliberately overrides the CPCB band colour for
   *  these wards. That is only safe because the ranking and the fill now read the
   *  same number: whatever horizon the map is painting, these ARE its highest wards,
   *  so red never lands on a ward that is cleaner than a yellow one beside it. The
   *  band is still one hover away, and the hover card says "worst 10". */
  urgentIds?: string[];
  selectedId?: string | null;
  /** The user's own ward (from geolocation) - gets a "you are here" pin. */
  hereId?: string | null;
  onPick?: (w: LiveWard) => void;
  /** Small numbered markers, e.g. deployment ranks: [{id: "W133", label: "1"}] */
  badges?: { id: string; label: string }[];
  ambient?: boolean;
  className?: string;
}) {
  /** The number this map paints for a ward: measured when "Now" is selected and a
   *  live reading exists, otherwise the forecast for the chosen horizon. */
  const aqiOf = (w: LiveWard): number => {
    if (isNow(horizon)) {
      const v = liveAqi?.get(w.zone_id);
      if (typeof v === "number") return v;
    }
    return wardAqiAt(w, asForecast(horizon));
  };
  const urgent = new Set(urgentIds ?? []);

  // Zoom: the map renders large on a laptop and swallowed the panel below it.
  // A plain scale on the viewBox keeps every coordinate in map space, so the
  // hover card and badges stay pinned without extra maths.
  //
  // At 1x the shape used to run edge to edge, which read as "oversized" and left
  // the panel below it fighting for room. The viewBox is padded so the default
  // view has margin around Delhi, and zoom now goes BELOW 1 as well - useful on a
  // wide monitor where the map would otherwise dominate the column.
  const [zoom, setZoom] = useState(1);
  const FIT_PAD = 1.14;                     // breathing room at 1x
  const vbW = (GEO.w * FIT_PAD) / zoom;
  const vbH = (GEO.h * FIT_PAD) / zoom;
  const vbX = (GEO.w - vbW) / 2;
  const vbY = (GEO.h - vbH) / 2;

  const [hoverId, setHoverId] = useState<string | null>(null);

  const byId = useMemo(() => {
    const m = new Map<string, LiveWard>();
    for (const w of liveWards ?? []) m.set(String(w.zone_id), w);
    return m;
  }, [liveWards]);

  const badgeById = useMemo(() => {
    const m = new Map<string, string>();
    for (const b of badges ?? []) m.set(b.id, b.label);
    return m;
  }, [badges]);

  // The shapefile carries all 287 MCD wards; the pipeline models 209. The other 78
  // used to render grey, which read as a hole in the map. They are filled by inverse
  // distance weighting over their three nearest modelled neighbours - the same k=3,
  // power-2 IDW the live layer already uses to carry stations onto ward centroids,
  // so it is one documented method rather than a second invented one.
  //
  // Centroids are in SVG space, not degrees. The projection is affine over an area
  // this small, so "nearest in SVG space" and "nearest on the ground" agree; only
  // the ordering matters here, never the distance in kilometres.
  const estimated = useMemo(() => {
    const out = new Map<string, number>();
    const known = GEO.wards
      .map((s) => ({ s, w: byId.get(s.id) }))
      .filter((x): x is { s: WardShape; w: LiveWard } => Boolean(x.w));
    if (known.length < 3) return out;
    for (const s of GEO.wards) {
      if (byId.has(s.id)) continue;
      const near = known
        .map(({ s: k, w }) => ({ d: Math.hypot(k.cx - s.cx, k.cy - s.cy), w }))
        .sort((a, b) => a.d - b.d)
        .slice(0, 3);
      let num = 0;
      let den = 0;
      for (const { d, w } of near) {
        const weight = 1 / Math.max(d, 1) ** 2;
        num += weight * aqiOf(w);
        den += weight;
      }
      if (den > 0) out.set(s.id, Math.round(num / den));
    }
    return out;
  }, [byId, horizon, liveAqi]);

  const hovered = hoverId ? byId.get(hoverId) : null;
  const hoveredEst = hoverId && !hovered ? estimated.get(hoverId) : undefined;
  const hoveredShape = hoverId ? GEO.wards.find((s) => s.id === hoverId) : null;

  return (
    // The frame takes Delhi's own aspect ratio (980x1052) rather than the full
    // column width. With preserveAspectRatio the SVG fits by height anyway, so a
    // full-width box just wrapped the shape in a wide empty band; matching the
    // aspect keeps the zoom controls and hover cards close to the map they belong to.
    <div
      className={`relative mx-auto h-full w-auto max-w-full ${className}`}
      style={{ aspectRatio: `${GEO.w} / ${GEO.h}` }}
    >
      <svg
        viewBox={`${vbX} ${vbY} ${vbW} ${vbH}`}
        preserveAspectRatio="xMidYMid meet"
        className="h-full w-full"
        role={ambient ? "img" : "group"}
        aria-label="Delhi ward map, colored by forecast AQI"
      >
        {GEO.wards.map((s) => {
          const live = byId.get(s.id);
          const est = live ? undefined : estimated.get(s.id);
          const isSel = selectedId === s.id;
          const isHover = hoverId === s.id;
          const cat = live ? aqiCategory(aqiOf(live)) : est !== undefined ? aqiCategory(est) : null;
          return (
            <path
              key={s.id + s.name}
              d={s.d}
              // Band colour everywhere, except the ranked wards, which are filled
              // red so "where do we go first" survives a projector at the back of
              // the room. Safe only because rank and fill agree on the number.
              fill={
                urgent.has(s.id) ? "var(--aqi-very-poor)"
                  : cat ? cat.color
                  : "var(--surface-2)"
              }
              fillOpacity={
                urgent.has(s.id) ? (ambient ? 0.75 : 1)
                  : cat ? (ambient ? 0.5 : isHover || isSel ? 0.98 : 0.78)
                  : 0.45
              }
              stroke={
                isSel ? "var(--accent)"
                  : urgent.has(s.id) ? "#111827"
                  : isHover ? "var(--accent-dim)"
                  : "var(--panel)"
              }
              strokeWidth={isSel ? 2.5 : urgent.has(s.id) ? 2.4 : isHover ? 1.8 : 0.7}
              style={{ transition: "fill 0.3s ease-out, fill-opacity 0.2s ease-out", cursor: !ambient && live && onPick ? "pointer" : "default" }}
              onMouseEnter={() => !ambient && setHoverId(s.id)}
              onMouseLeave={() => !ambient && setHoverId(null)}
              onClick={() => !ambient && live && onPick?.(live)}
            >
              {!ambient && <title>{live ? `${live.name} · AQI ${aqiOf(live)}` : `${s.name} · outside the forecast set`}</title>}
            </path>
          );
        })}

        {/* Selected ward re-drawn on top so its outline is never underlapped */}
        {selectedId &&
          (() => {
            const s = GEO.wards.find((x) => x.id === selectedId);
            if (!s) return null;
            return <path d={s.d} fill="none" stroke="var(--accent)" strokeWidth="2.5" />;
          })()}

        {/* "You are here" - pin at the geolocated ward's centroid */}
        {hereId &&
          (() => {
            const s = GEO.wards.find((x) => x.id === hereId);
            if (!s) return null;
            return (
              <g pointerEvents="none">
                <circle cx={s.cx} cy={s.cy} r="8.5" fill="var(--panel)" stroke="var(--accent)" strokeWidth="2" />
                <circle cx={s.cx} cy={s.cy} r="3.6" fill="var(--accent)" />
                <text x={s.cx} y={s.cy - 14} textAnchor="middle" fontSize="12.5" fontWeight="700" fill="var(--accent)" className="mono">
                  You
                </text>
              </g>
            );
          })()}

        {/* Numbered markers (deployment ranks) */}
        {(badges ?? []).map((b) => {
          const s = GEO.wards.find((x) => x.id === b.id);
          if (!s) return null;
          return (
            <g key={`badge-${b.id}`} pointerEvents="none">
              <circle cx={s.cx} cy={s.cy} r="11" fill="var(--accent)" stroke="var(--panel)" strokeWidth="1.5" />
              <text x={s.cx} y={s.cy + 3.5} textAnchor="middle" fontSize="11" fontWeight="700" fill="#ffffff" className="mono">
                {b.label}
              </text>
            </g>
          );
        })}
      </svg>

      {!ambient && (
        <div className="absolute bottom-3 right-3 z-10 flex flex-col overflow-hidden rounded-md border border-border bg-panel shadow-[0_2px_8px_rgba(9,20,28,0.12)]">
          <button
            onClick={() => setZoom((z) => Math.min(4, +(z + (z < 1 ? 0.2 : 0.4)).toFixed(2)))}
            aria-label="Zoom in"
            className="px-2.5 py-1.5 text-[15px] leading-none text-text-dim hover:bg-surface-1 hover:text-foreground"
          >
            +
          </button>
          <button
            onClick={() => setZoom((z) => Math.max(0.6, +(z - (z <= 1 ? 0.2 : 0.4)).toFixed(2)))}
            aria-label="Zoom out"
            disabled={zoom <= 0.6}
            className="border-t border-border px-2.5 py-1.5 text-[15px] leading-none text-text-dim hover:bg-surface-1 hover:text-foreground disabled:opacity-40"
          >
            &minus;
          </button>
          {zoom !== 1 && (
            <button
              onClick={() => setZoom(1)}
              aria-label="Reset zoom"
              className="mono border-t border-border px-1.5 py-1 text-[9px] text-text-mute hover:bg-surface-1"
            >
              {zoom.toFixed(1)}x
            </button>
          )}
        </div>
      )}

      {/* Hover card - name, AQI, band, dominant source */}
      {!ambient && (hovered || hoveredEst !== undefined) && hoveredShape && (
        <div
          className="pointer-events-none absolute z-10 -translate-x-1/2 rounded-md border border-border bg-panel px-3 py-2 shadow-[0_2px_8px_rgba(9,20,28,0.12)]"
          style={{
            left: `${(hoveredShape.cx / GEO.w) * 100}%`,
            top: `calc(${(hoveredShape.cy / GEO.h) * 100}% - 56px)`,
          }}
        >
          {(() => {
            // One card for both kinds of ward. An interpolated ward says so on its
            // own card rather than in a legend, so the qualifier travels with the
            // number instead of sitting somewhere the reader has to remember.
            const value = hovered ? aqiOf(hovered) : (hoveredEst as number);
            const cat = aqiCategory(value);
            return (
              <>
                <div className="flex items-center gap-2 whitespace-nowrap">
                  <span className="text-[12.5px] font-bold">
                    {hovered ? hovered.name : hoveredShape.name}
                  </span>
                  <span
                    className="mono rounded-md px-1.5 py-0.5 text-[11px] font-bold"
                    style={{ background: cat.color, color: cat.text }}
                  >
                    {value}
                  </span>
                </div>
                <div className="mono mt-0.5 whitespace-nowrap text-[11px] text-text-mute">
                  {cat.label}
                  {hoverId && urgent.has(hoverId) ? " · worst 10" : ""}
                  {hovered
                    ? hovered.dominant_source
                      ? ` · ${hovered.dominant_source}`
                      : ""
                    : " · estimated from neighbouring wards"}
                </div>
              </>
            );
          })()}
        </div>
      )}
    </div>
  );
}
