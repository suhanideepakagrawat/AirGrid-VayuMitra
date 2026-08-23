// Chart primitives for the Bulletin system. Flat SVG/DOM, CPCB band colors,
// tabular figures, hairline structure - no chart library, no gradients.
// Every chart re-renders from the active horizon, so switching +24/+48/+72
// visibly moves every number and bar on the page.

import {
  aqiCategory,
  HORIZONS,
  HORIZON_SEL,
  type Horizon,
  type HorizonSel,
} from "@/lib/air-data";

export const HORIZON_LABEL: Record<Horizon, string> = {
  "24": "Tomorrow",
  "48": "Day after",
  "72": "Day 3",
};

/* ------------------------------------------------------------------ */
/* Horizon segmented control - THE control of the product              */
/* ------------------------------------------------------------------ */

export function HorizonSwitch({
  value,
  onChange,
  compact = false,
}: {
  value: HorizonSel;
  onChange: (h: HorizonSel) => void;
  compact?: boolean;
}) {
  return (
    <div
      role="tablist"
      aria-label="Forecast horizon"
      className="inline-flex overflow-hidden rounded-full border border-border bg-panel p-0.5"
    >
      {HORIZON_SEL.map((h) => {
        const active = h === value;
        return (
          <button
            key={h}
            role="tab"
            aria-selected={active}
            onClick={() => onChange(h)}
            className={`rounded-full transition-colors ${compact ? "px-3 py-1" : "px-4 py-1.5"} ${
              active ? "bg-accent text-white" : "text-text-dim hover:bg-surface-1 hover:text-foreground"
            }`}
          >
            <span className={`mono block font-bold leading-tight ${compact ? "text-[12px]" : "text-[12.5px]"}`}>
              {h === "now" ? "Now" : `+${h} h`}
            </span>
            {!compact && (
              <span className={`block text-[10.5px] leading-tight ${active ? "text-white/85" : "text-text-mute"}`}>
                {h === "now" ? "Measured" : HORIZON_LABEL[h as Horizon]}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* AQI indicator - the signature severity carrier                      */
/* ------------------------------------------------------------------ */

export function AqiBall({ aqi, size = 64 }: { aqi: number; size?: number }) {
  const cat = aqiCategory(aqi);
  return (
    <div
      className="mono grid shrink-0 place-items-center rounded-full text-center leading-none"
      style={{ background: cat.color, color: cat.text, width: size, height: size }}
    >
      <div>
        <div style={{ fontSize: size * 0.3 }} className="font-bold">{aqi}</div>
        <div style={{ fontSize: Math.max(9, size * 0.14) }} className="opacity-85">AQI</div>
      </div>
    </div>
  );
}

export function BandBadge({ aqi }: { aqi: number }) {
  const cat = aqiCategory(aqi);
  return (
    <span
      className="mono inline-block rounded-md px-2 py-0.5 text-[12px] font-bold"
      style={{ background: cat.color, color: cat.text }}
    >
      {cat.label}
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Three-horizon forecast columns for one ward / cell                  */
/* ------------------------------------------------------------------ */

export function HorizonTriplet({
  values,
  active,
  onSelect,
  height = 96,
}: {
  values: Partial<Record<HorizonSel, number>>;
  active: HorizonSel;
  onSelect?: (h: HorizonSel) => void;
  height?: number;
}) {
  // Only bars we have a number for. "now" leads when live data is present.
  const keys = HORIZON_SEL.filter((h) => typeof values[h] === "number");
  const max = Math.max(...keys.map((h) => values[h] as number), 1);
  return (
    <div className="flex items-end gap-3" style={{ height: height + 38 }}>
      {keys.map((h) => {
        const v = values[h] as number;
        const cat = aqiCategory(v);
        const isActive = h === active;
        const barH = Math.max(8, (v / max) * height);
        return (
          <button
            key={h}
            onClick={() => onSelect?.(h)}
            disabled={!onSelect}
            aria-label={`+${h} hours: AQI ${v}, ${cat.label}`}
            aria-pressed={isActive}
            className={`group flex flex-1 flex-col items-center justify-end gap-1 ${onSelect ? "cursor-pointer" : "cursor-default"}`}
          >
            <span className={`mono text-[12px] font-bold ${isActive ? "text-foreground" : "text-text-mute"}`}>
              {v}
            </span>
            <div
              className="w-full rounded-t-[3px] transition-all duration-200"
              style={{
                height: barH,
                background: cat.color,
                opacity: isActive ? 1 : 0.45,
                outline: isActive ? "1.5px solid var(--accent-dim)" : "none",
                outlineOffset: 1,
              }}
            />
            <span className={`mono text-[10.5px] ${isActive ? "font-bold text-accent" : "text-text-mute"}`}>
              +{h}h
            </span>
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Band distribution - how many wards sit in each CPCB band            */
/* ------------------------------------------------------------------ */

const BAND_ORDER = [
  { label: "Good", max: 50 },
  { label: "Satisfactory", max: 100 },
  { label: "Moderate", max: 200 },
  { label: "Poor", max: 300 },
  { label: "Very Poor", max: 400 },
  { label: "Severe", max: Infinity },
] as const;

export function bandCounts(aqis: number[]): { label: string; color: string; text: string; count: number }[] {
  return BAND_ORDER.map((b) => {
    const sample = b.max === Infinity ? 450 : b.max;
    const cat = aqiCategory(sample);
    return {
      label: b.label,
      color: cat.color,
      text: cat.text,
      count: aqis.filter((a) => aqiCategory(a).label === b.label).length,
    };
  });
}

export function BandDistribution({ aqis, caption }: { aqis: number[]; caption?: string }) {
  const counts = bandCounts(aqis);
  const total = aqis.length || 1;
  return (
    <div>
      <div className="flex h-5 overflow-hidden rounded-[4px] border border-border">
        {counts.filter((c) => c.count > 0).map((c) => (
          <div
            key={c.label}
            title={`${c.label} · ${c.count} wards`}
            className="transition-all duration-300"
            style={{ width: `${(c.count / total) * 100}%`, background: c.color }}
          />
        ))}
      </div>
      <div className="mono mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
        {counts.filter((c) => c.count > 0).map((c) => (
          <span key={c.label} className="flex items-center gap-1.5 text-text-dim">
            <span className="h-2 w-2 rounded-[2px]" style={{ background: c.color }} />
            {c.label} <b className="text-foreground">{c.count}</b>
          </span>
        ))}
      </div>
      {caption && <div className="mono mt-1.5 text-[11px] text-text-mute">{caption}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Ranked horizontal bars - worst wards first                          */
/* ------------------------------------------------------------------ */

export function RankBars({
  rows,
  onPick,
}: {
  rows: { id: string; name: string; value: number; sub?: string }[];
  onPick?: (id: string) => void;
}) {
  const max = Math.max(...rows.map((r) => r.value), 1);
  return (
    <ul className="space-y-1.5">
      {rows.map((r, i) => {
        const cat = aqiCategory(r.value);
        const inner = (
          <>
            <span className="mono w-5 shrink-0 text-right text-[11px] text-text-mute">{i + 1}</span>
            <span className="w-36 shrink-0 truncate text-left text-[12.5px] text-foreground">{r.name}</span>
            <span className="relative h-4 flex-1 overflow-hidden rounded-[3px] bg-surface-1">
              <span
                className="absolute inset-y-0 left-0 rounded-[3px] transition-all duration-300"
                style={{ width: `${(r.value / max) * 100}%`, background: cat.color }}
              />
            </span>
            <span className="mono w-10 shrink-0 text-right text-[12.5px] font-bold text-foreground">
              {r.value}
            </span>
            {r.sub && <span className="mono hidden w-24 shrink-0 truncate text-right text-[11px] text-text-mute sm:block">{r.sub}</span>}
          </>
        );
        return (
          <li key={r.id}>
            {onPick ? (
              <button
                onClick={() => onPick(r.id)}
                className="flex w-full items-center gap-2 rounded-[4px] px-1 py-0.5 hover:bg-surface-1"
              >
                {inner}
              </button>
            ) : (
              <div className="flex items-center gap-2 px-1 py-0.5">{inner}</div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/* ------------------------------------------------------------------ */
/* City trajectory - mean AQI across the three horizons                */
/* ------------------------------------------------------------------ */

export function CityTrend({
  values,
  active,
}: {
  values: Partial<Record<HorizonSel, number>>;
  active: HorizonSel;
}) {
  const W = 260;
  const H = 120;
  const PADX = 26;
  const PADY = 22;
  // Only plot the points we actually have. "now" is absent until the live layer
  // resolves, and drawing it as zero would invent a cliff in the trend line.
  const keys = HORIZON_SEL.filter((h) => typeof values[h] === "number");
  const nums = keys.map((h) => values[h] as number);
  const max = Math.max(...nums, 1);
  const min = Math.min(...nums, 0);
  const span = Math.max(max - min, 20);
  const x = (i: number) => PADX + (keys.length > 1 ? i / (keys.length - 1) : 0.5) * (W - PADX * 2);
  const y = (v: number) => H - PADY - ((v - min) / span) * (H - PADY * 2);
  const pts = keys.map((h, i) => `${x(i)},${y(values[h] as number)}`).join(" ");

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="City average AQI across horizons">
      <polyline points={pts} fill="none" stroke="var(--accent-dim)" strokeWidth="1.5" />
      {keys.map((h, i) => {
        const v = values[h] as number;
        const cat = aqiCategory(v);
        const isActive = h === active;
        return (
          <g key={h}>
            <circle cx={x(i)} cy={y(v)} r={isActive ? 6 : 4} fill={cat.color} stroke={isActive ? "var(--accent)" : "var(--panel)"} strokeWidth="1.5" />
            <text x={x(i)} y={y(v) - 10} textAnchor="middle" fontSize="11" fontWeight={isActive ? 700 : 400} fill="var(--text)" className="mono">
              {Math.round(v)}
            </text>
            <text x={x(i)} y={H - 6} textAnchor="middle" fontSize="10" fill={isActive ? "var(--accent)" : "var(--text-mute)"} fontWeight={isActive ? 700 : 400} className="mono">
              {h === "now" ? "Now" : `+${h}h`}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

/* ------------------------------------------------------------------ */
/* Change vs tomorrow - the "is it getting better or worse" tag        */
/* ------------------------------------------------------------------ */

export function DeltaTag({ now, base }: { now: number; base: number }) {
  const d = now - base;
  if (Math.abs(d) < 3) {
    return <span className="mono text-[11.5px] text-text-mute">≈ steady vs tomorrow</span>;
  }
  const worse = d > 0;
  return (
    <span
      className="mono text-[11.5px] font-bold"
      style={{ color: worse ? "var(--aqi-very-poor)" : "var(--aqi-good)" }}
    >
      {worse ? "▲" : "▼"} {Math.abs(d)} {worse ? "worse" : "better"} vs tomorrow
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Source mix - 100% stacked strip + legend                            */
/* ------------------------------------------------------------------ */

export function SourceStrip({
  mix,
  height = 14,
}: {
  mix: { key: string; label: string; color: string; pct: number }[];
  height?: number;
}) {
  return (
    <div className="flex overflow-hidden rounded-[4px] border border-border" style={{ height }}>
      {mix.filter((m) => m.pct > 0).map((m) => (
        <div
          key={m.key}
          title={`${m.label} · ${Math.round(m.pct)}%`}
          className="transition-all duration-300"
          style={{ width: `${m.pct}%`, background: m.color }}
        />
      ))}
    </div>
  );
}
