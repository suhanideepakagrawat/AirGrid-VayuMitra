import { useMemo, useState } from "react";
import {
  CELLS,
  cellAqi,
  GRID_COLS,
  GRID_ROWS,
  SOURCE_COLORS,
  FIRE_HOTSPOTS,
  WIND,
  VULNERABLE_SITES,
  ENFORCEMENT_TARGETS,
  type Cell,
  type Horizon,
  type SourceKey,
} from "@/lib/air-data";

type LayerToggles = {
  windCorridor: boolean;
  fires: boolean;
  wards: boolean;
  enforcement: boolean;
  vulnerable: boolean;
};

type Props = {
  selectedId?: string | null;
  onSelect?: (cell: Cell) => void;
  layers?: Partial<LayerToggles>;
  sourceFilter?: SourceKey | "all";
  horizon?: Horizon; // cell intensity follows this horizon's forecast AQI
  ambient?: boolean; // landing hero mode - no interactions
  height?: number | string;
};

const CELL_W = 32;
const CELL_H = 32;
const PAD = 40;

export function MapView({
  selectedId,
  onSelect,
  layers,
  sourceFilter = "all",
  horizon = "24",
  ambient = false,
  height = "100%",
}: Props) {
  const [hoverId, setHoverId] = useState<string | null>(null);

  const L: LayerToggles = {
    windCorridor: true,
    fires: true,
    wards: false,
    enforcement: false,
    vulnerable: false,
    ...layers,
  };

  const width = GRID_COLS * CELL_W + PAD * 2;
  const heightPx = GRID_ROWS * CELL_H + PAD * 2;

  const selected = useMemo(
    () => CELLS.find((c) => c.id === selectedId) ?? null,
    [selectedId],
  );

  // Enforcement markers mapped to cell coords
  const enforcementMarkers = useMemo(() => {
    return ENFORCEMENT_TARGETS.map((t) => {
      const c = CELLS.find((cc) => cc.id === t.cellId);
      return c ? { ...t, cx: PAD + c.x * CELL_W + CELL_W / 2, cy: PAD + c.y * CELL_H + CELL_H / 2 } : null;
    }).filter(Boolean) as Array<(typeof ENFORCEMENT_TARGETS)[number] & { cx: number; cy: number }>;
  }, []);

  return (
    <div className="relative h-full w-full overflow-hidden bg-bg-secondary" style={{ height }}>
      {/* Regional zone indicator */}
      <div className="pointer-events-none absolute inset-0 bg-gradient-to-b from-[color:var(--surface-1)]/40 to-transparent" />

      <svg
        viewBox={`0 0 ${width} ${heightPx}`}
        preserveAspectRatio="xMidYMid meet"
        className="h-full w-full"
      >
        <defs>
          <pattern id="dotgrid" width="8" height="8" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="0.6" fill="rgba(7,94,84,0.10)" />
          </pattern>
          <radialGradient id="fireGrad">
            <stop offset="0%" stopColor="var(--source-burning)" stopOpacity="0.9" />
            <stop offset="60%" stopColor="var(--source-burning)" stopOpacity="0.25" />
            <stop offset="100%" stopColor="var(--source-burning)" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="cellHover">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.35" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
          </radialGradient>
          <filter id="soft" x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="2" />
          </filter>
        </defs>

        {/* Background dot-grid */}
        <rect width={width} height={heightPx} fill="url(#dotgrid)" />

        {/* Regional/local divider - a subtle boundary at top */}
        <line
          x1={0}
          y1={PAD - 8}
          x2={width}
          y2={PAD - 8}
          stroke="var(--accent-dim)"
          strokeOpacity="0.25"
          strokeDasharray="2 6"
        />
        <text
          x={PAD}
          y={PAD - 14}
          className="mono"
          fill="var(--text-mute)"
          fontSize="9"
          
        >
          Regional transport zone ↑ · Delhi-NCR grid ↓
        </text>

        {/* Fire hotspots (outside grid extent, above the divider) */}
        {L.fires &&
          FIRE_HOTSPOTS.map((f) => {
            const cx = PAD + (0.15 + f.x) * GRID_COLS * CELL_W;
            const cy = PAD + (0.05 + f.y) * GRID_ROWS * CELL_H;
            return (
              <g key={f.id}>
                <circle cx={cx} cy={cy} r={22} fill="url(#fireGrad)" className="fire-glow" />
                <circle cx={cx} cy={cy} r={3} fill="var(--source-burning)" />
                {/* Transport corridor from fire into a NW cell */}
                <line
                  x1={cx}
                  y1={cy}
                  x2={PAD + 5 * CELL_W + CELL_W / 2}
                  y2={PAD + 2 * CELL_H + CELL_H / 2}
                  stroke="var(--source-burning)"
                  strokeOpacity="0.4"
                  strokeWidth="1"
                  strokeDasharray="4 6"
                  className="wind-flow"
                />
              </g>
            );
          })}

        {/* Grid cells - intensity tracks the active horizon's forecast AQI,
            so switching +24/+48/+72 visibly re-lights the map. */}
        {CELLS.map((c) => {
          const cx = PAD + c.x * CELL_W;
          const cy = PAD + c.y * CELL_H;
          const dim = sourceFilter !== "all" && c.dominantSource !== sourceFilter;
          const isSelected = selectedId === c.id;
          const isHover = hoverId === c.id;
          const load = Math.min(cellAqi(c, horizon) / 420, 1);
          const opacity = dim ? 0.06 : 0.06 + load * (0.35 + c.confidence * 0.55);
          const stableEnough = c.confidence > 0.75 && !dim;
          return (
            <g
              key={c.id}
              onMouseEnter={() => !ambient && setHoverId(c.id)}
              onMouseLeave={() => !ambient && setHoverId(null)}
              onClick={() => !ambient && onSelect?.(c)}
              style={{ cursor: ambient ? "default" : "pointer" }}
            >
              <rect
                x={cx + 1}
                y={cy + 1}
                width={CELL_W - 2}
                height={CELL_H - 2}
                fill={SOURCE_COLORS[c.dominantSource]}
                fillOpacity={opacity}
                style={{ transition: "fill-opacity 0.3s ease-out" }}
                className={stableEnough ? "cell-pulse" : ""}
              />
              {isSelected && (
                <rect
                  x={cx}
                  y={cy}
                  width={CELL_W}
                  height={CELL_H}
                  fill="none"
                  stroke="var(--accent)"
                  strokeWidth="1.5"
                />
              )}
              {isHover && !isSelected && (
                <rect
                  x={cx}
                  y={cy}
                  width={CELL_W}
                  height={CELL_H}
                  fill="none"
                  stroke="var(--accent-dim)"
                  strokeWidth="1"
                />
              )}
            </g>
          );
        })}

        {/* Ward overlay - abstract polygon lines */}
        {L.wards && (
          <g stroke="var(--accent-dim)" strokeOpacity="0.35" fill="none" strokeWidth="1">
            {[
              "M40,40 L200,40 L260,160 L120,220 Z",
              "M260,40 L500,40 L560,180 L300,200 Z",
              "M40,220 L300,200 L340,360 L60,380 Z",
              "M340,220 L620,220 L680,400 L360,420 Z",
              "M620,40 L820,60 L780,240 L560,180 Z",
            ].map((d, i) => (
              <path key={i} d={d} strokeDasharray="1 3" />
            ))}
          </g>
        )}

        {/* Wind corridor overlay for selected cell */}
        {L.windCorridor && selected && (
          <g>
            {(() => {
              const cx = PAD + selected.x * CELL_W + CELL_W / 2;
              const cy = PAD + selected.y * CELL_H + CELL_H / 2;
              // upwind = opposite of wind direction
              const ux = cx - WIND.dxCell * CELL_W * 8;
              const uy = cy - WIND.dyCell * CELL_H * 8;
              return (
                <>
                  <line
                    x1={ux}
                    y1={uy}
                    x2={cx}
                    y2={cy}
                    stroke="var(--accent)"
                    strokeWidth="1.5"
                    strokeDasharray="6 6"
                    className="wind-flow"
                    opacity="0.85"
                  />
                  <circle cx={ux} cy={uy} r={4} fill="var(--accent)" opacity="0.7" />
                  <text
                    x={ux + 6}
                    y={uy - 6}
                    fontSize="9"
                    fill="var(--accent)"
                    className="mono"
                  >
                    upwind
                  </text>
                </>
              );
            })()}
          </g>
        )}

        {/* Vulnerable population overlay */}
        {L.vulnerable &&
          VULNERABLE_SITES.map((v) => {
            const cx = PAD + v.x * CELL_W + CELL_W / 2;
            const cy = PAD + v.y * CELL_H + CELL_H / 2;
            return (
              <g key={v.id}>
                <rect
                  x={cx - 5}
                  y={cy - 5}
                  width={10}
                  height={10}
                  fill="none"
                  stroke="var(--accent)"
                  strokeWidth="1.2"
                  transform={`rotate(45 ${cx} ${cy})`}
                />
              </g>
            );
          })}

        {/* Enforcement markers - triangles, distinct from AQI hues */}
        {L.enforcement &&
          enforcementMarkers.map((m) => {
            const size = 4 + (m.priority - 60) / 4;
            return (
              <g key={m.id}>
                <polygon
                  points={`${m.cx},${m.cy - size} ${m.cx - size},${m.cy + size} ${m.cx + size},${m.cy + size}`}
                  fill="var(--accent)"
                  stroke="var(--bg-secondary)"
                  strokeWidth="1"
                />
              </g>
            );
          })}

        {/* Ambient wind sweep for landing */}
        {ambient && (
          <g opacity="0.5">
            {Array.from({ length: 5 }).map((_, i) => (
              <line
                key={i}
                x1={0}
                y1={100 + i * 70}
                x2={width}
                y2={100 + i * 70 + 50}
                stroke="var(--accent)"
                strokeOpacity="0.15"
                strokeWidth="1"
                strokeDasharray="10 14"
                className="wind-flow"
                style={{ animationDuration: `${3 + i * 0.4}s` }}
              />
            ))}
          </g>
        )}

        {/* Compass / scale */}
        <g transform={`translate(${width - 90}, ${heightPx - 40})`}>
          <text x="0" y="0" fontSize="9" fill="var(--text-mute)" className="mono" >
            1 km
          </text>
          <line x1="0" y1="8" x2="32" y2="8" stroke="var(--accent-dim)" strokeWidth="1" />
          <line x1="0" y1="5" x2="0" y2="11" stroke="var(--accent-dim)" />
          <line x1="32" y1="5" x2="32" y2="11" stroke="var(--accent-dim)" />
          <text x="50" y="10" fontSize="9" fill="var(--text-mute)" className="mono">N↖ {WIND.speedKmh} km/h</text>
        </g>
      </svg>
    </div>
  );
}
