// The real Delhi ward map - actual MCD boundaries from the pipeline's
// shapefile, pre-projected to SVG paths at build time (src/data/delhi-wards.json).
// Wards are filled with their CPCB band color at the active horizon, so
// switching +24/+48/+72 recolors the actual city. Click a ward to focus it.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  const MIN_ZOOM = 0.6;
  const MAX_ZOOM = 8;

  // The pannable world: Delhi plus the 1x padding. Panning is expressed as the map
  // point at the centre of the viewport, which keeps the clamp trivial and survives
  // any zoom change without a second source of truth.
  const WORLD_W = GEO.w * FIT_PAD;
  const WORLD_H = GEO.h * FIT_PAD;
  const WORLD_X = (GEO.w - WORLD_W) / 2;
  const WORLD_Y = (GEO.h - WORLD_H) / 2;

  const [center, setCenter] = useState({ x: GEO.w / 2, y: GEO.h / 2 });

  const clampCenter = useCallback(
    (c: { x: number; y: number }, w: number, h: number) => ({
      x: w >= WORLD_W
        ? GEO.w / 2
        : Math.min(Math.max(c.x, WORLD_X + w / 2), WORLD_X + WORLD_W - w / 2),
      y: h >= WORLD_H
        ? GEO.h / 2
        : Math.min(Math.max(c.y, WORLD_Y + h / 2), WORLD_Y + WORLD_H - h / 2),
    }),
    [WORLD_W, WORLD_H, WORLD_X, WORLD_Y],
  );

  const vbW = WORLD_W / zoom;
  const vbH = WORLD_H / zoom;
  const safe = clampCenter(center, vbW, vbH);
  const vbX = safe.x - vbW / 2;
  const vbY = safe.y - vbH / 2;

  const frameRef = useRef<HTMLDivElement | null>(null);
  const [hint, setHint] = useState(false);
  const hintTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** Screen point -> map point, using the viewBox actually on screen. */
  const toMap = useCallback(
    (clientX: number, clientY: number) => {
      const r = frameRef.current?.getBoundingClientRect();
      if (!r || !r.width || !r.height) return null;
      // preserveAspectRatio="xMidYMid meet": the viewBox is letterboxed inside the
      // frame, so the drawn area is the largest box of the viewBox's aspect that
      // fits. Ignoring that offset made the cursor anchor drift.
      const scale = Math.min(r.width / vbW, r.height / vbH);
      const drawnW = vbW * scale;
      const drawnH = vbH * scale;
      const offX = (r.width - drawnW) / 2;
      const offY = (r.height - drawnH) / 2;
      return {
        x: vbX + (clientX - r.left - offX) / scale,
        y: vbY + (clientY - r.top - offY) / scale,
        scale,
      };
    },
    [vbW, vbH, vbX, vbY],
  );

  /** Zoom about a fixed map point, so whatever is under the cursor stays put. */
  const zoomAt = useCallback(
    (factor: number, anchor?: { x: number; y: number }) => {
      setZoom((z) => {
        const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z * factor));
        if (next === z) return z;
        if (anchor) {
          const k = z / next;
          setCenter((c) => ({
            x: anchor.x + (c.x - anchor.x) * k,
            y: anchor.y + (c.y - anchor.y) * k,
          }));
        }
        return next;
      });
    },
    [],
  );

  const resetView = useCallback(() => {
    setZoom(1);
    setCenter({ x: GEO.w / 2, y: GEO.h / 2 });
  }, []);

  // Wheel zoom. Registered natively because a React wheel handler is passive and
  // cannot preventDefault.
  //
  // A map that eats the wheel is a trap on a page the reader still needs to scroll,
  // so plain wheel only zooms where the page has nothing left to scroll - the
  // viewport-locked dashboard and enforcement panes. Everywhere else it needs
  // Ctrl/Cmd, and a plain wheel says so once instead of silently doing nothing.
  useEffect(() => {
    const el = frameRef.current;
    if (!el || ambient) return;
    const onWheel = (e: WheelEvent) => {
      const doc = document.documentElement;
      const pageScrolls = doc.scrollHeight > doc.clientHeight + 1;
      if (pageScrolls && !e.ctrlKey && !e.metaKey) {
        setHint(true);
        if (hintTimer.current) clearTimeout(hintTimer.current);
        hintTimer.current = setTimeout(() => setHint(false), 1600);
        return;                                   // let the page scroll
      }
      e.preventDefault();
      const at = toMap(e.clientX, e.clientY);
      zoomAt(Math.exp(-e.deltaY * 0.0015), at ?? undefined);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [ambient, toMap, zoomAt]);

  useEffect(() => () => {
    if (hintTimer.current) clearTimeout(hintTimer.current);
  }, []);

  // Drag to pan, and pinch to zoom on touch. Zooming without panning strands the
  // reader as soon as their target leaves the frame.
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinchRef = useRef<{ dist: number; mid: { x: number; y: number } } | null>(null);
  const dragRef = useRef<{ x: number; y: number } | null>(null);
  /** Set once a gesture moves far enough to be a drag, so it does not also fire a
   *  ward selection on pointer-up. */
  const movedRef = useRef(false);

  const onPointerDown = (e: React.PointerEvent) => {
    if (ambient) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    movedRef.current = false;
    if (pointers.current.size === 1) {
      dragRef.current = { x: e.clientX, y: e.clientY };
    } else if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()];
      pinchRef.current = {
        dist: Math.hypot(a.x - b.x, a.y - b.y),
        mid: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 },
      };
      dragRef.current = null;
    }
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (ambient || !pointers.current.has(e.pointerId)) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });

    if (pointers.current.size >= 2 && pinchRef.current) {
      const [a, b] = [...pointers.current.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      if (pinchRef.current.dist > 0) {
        movedRef.current = true;
        const at = toMap(mid.x, mid.y);
        zoomAt(dist / pinchRef.current.dist, at ?? undefined);
      }
      pinchRef.current = { dist, mid };
      return;
    }

    const start = dragRef.current;
    if (!start) return;
    const dx = e.clientX - start.x;
    const dy = e.clientY - start.y;
    if (!movedRef.current && Math.hypot(dx, dy) < 4) return;   // still a click
    movedRef.current = true;
    const r = frameRef.current?.getBoundingClientRect();
    if (!r || !r.width) return;
    const scale = Math.min(r.width / vbW, r.height / vbH);
    dragRef.current = { x: e.clientX, y: e.clientY };
    setCenter((c) => clampCenter({ x: c.x - dx / scale, y: c.y - dy / scale }, vbW, vbH));
  };

  const endPointer = (e: React.PointerEvent) => {
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinchRef.current = null;
    if (pointers.current.size === 0) dragRef.current = null;
  };

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
      ref={frameRef}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endPointer}
      onPointerCancel={endPointer}
      onPointerLeave={endPointer}
      onDoubleClick={(e) => {
        if (ambient) return;
        const at = toMap(e.clientX, e.clientY);
        zoomAt(1.8, at ?? undefined);
      }}
      // touch-action matters more than it looks. "none" is what lets us own drag and
      // pinch, but on a phone the map fills the column, so owning touch at rest
      // means a swipe over it cannot scroll the page. At 1x the page keeps the
      // gesture (pan-y); once the reader has zoomed in they clearly want to move
      // around the map, so we take it.
      className={`relative mx-auto h-full w-auto max-w-full ${
        ambient ? "" : zoom > 1 ? "cursor-grab touch-none active:cursor-grabbing" : "touch-pan-y"
      } ${className}`}
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
              onClick={() => !ambient && !movedRef.current && live && onPick?.(live)}
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
            onClick={() => zoomAt(1.35)}
            aria-label="Zoom in"
            className="px-2.5 py-1.5 text-[15px] leading-none text-text-dim hover:bg-surface-1 hover:text-foreground"
          >
            +
          </button>
          <button
            onClick={() => zoomAt(1 / 1.35)}
            aria-label="Zoom out"
            disabled={zoom <= MIN_ZOOM}
            className="border-t border-border px-2.5 py-1.5 text-[15px] leading-none text-text-dim hover:bg-surface-1 hover:text-foreground disabled:opacity-40"
          >
            &minus;
          </button>
          {(zoom !== 1 || safe.x !== GEO.w / 2 || safe.y !== GEO.h / 2) && (
            <button
              onClick={resetView}
              aria-label="Reset zoom and position"
              className="mono border-t border-border px-1.5 py-1 text-[9px] text-text-mute hover:bg-surface-1"
            >
              {zoom.toFixed(1)}x
            </button>
          )}
        </div>
      )}

      {/* Says why a plain wheel did nothing, rather than leaving the reader to
          guess. Only ever appears on pages that still have somewhere to scroll. */}
      {hint && !ambient && (
        <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center">
          <span className="mono rounded-full bg-[rgba(9,20,28,0.82)] px-3.5 py-1.5 text-[12px] font-semibold text-white">
            Hold Ctrl (⌘ on Mac) and scroll to zoom · or drag to pan
          </span>
        </div>
      )}

      {/* Hover card - name, AQI, band, dominant source */}
      {!ambient && (hovered || hoveredEst !== undefined) && hoveredShape && (
        <div
          className="pointer-events-none absolute z-10 -translate-x-1/2 rounded-md border border-border bg-panel px-3 py-2 shadow-[0_2px_8px_rgba(9,20,28,0.12)]"
          style={{
            left: `${((hoveredShape.cx - vbX) / vbW) * 100}%`,
            top: `calc(${((hoveredShape.cy - vbY) / vbH) * 100}% - 56px)`,
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
