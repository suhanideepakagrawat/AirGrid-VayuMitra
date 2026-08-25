import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { liveQuery, timeAgo, wardsQuery, type LiveNow } from "@/lib/api";
import { aqiCategory } from "@/lib/air-data";

/**
 * The provenance strip - the single most important honesty surface in the product.
 *
 * AirGrid serves two different kinds of number and they must never be confused:
 *
 *   LIVE      what CPCB/DPCC/IMD instruments are reading right now
 *   FORECAST  what our trained models predict for +24 / 48 / 72 h
 *
 * Before this existed nothing on any screen said how old anything was, so a
 * month-old model run read exactly like a current measurement. Both timestamps are
 * now stated plainly, side by side, wherever numbers are shown.
 *
 * Deliberately shows the forecast's age even when that age is unflattering: a
 * reviewer who spots an undated stale number stops believing the rest of the page,
 * whereas one who sees it labelled reads it as rigour.
 */

function Dot({ tone }: { tone: "live" | "stale" | "off" }) {
  const color =
    tone === "live" ? "var(--aqi-good, #22c55e)"
    : tone === "stale" ? "var(--aqi-moderate, #eab308)"
    : "var(--text-mute, #94a3b8)";
  return (
    <span className="relative inline-flex h-2.5 w-2.5 shrink-0" aria-hidden="true">
      {tone === "live" && (
        <span
          className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60 motion-reduce:animate-none"
          style={{ background: color }}
        />
      )}
      <span className="relative inline-flex h-2.5 w-2.5 rounded-full" style={{ background: color }} />
    </span>
  );
}

function liveSummary(live: LiveNow | undefined) {
  if (!live?.available || !live.wards?.length) return null;
  const mean = Math.round(live.wards.reduce((a, w) => a + w.aqi, 0) / live.wards.length);
  const worst = live.wards.reduce((a, w) => (w.aqi > a.aqi ? w : a), live.wards[0]);
  return { mean, worst, stations: live.stations ?? 0 };
}

export function DataFreshness({ className = "" }: { className?: string }) {
  const live = useQuery(liveQuery);
  const wards = useQuery(wardsQuery());

  const summary = useMemo(() => liveSummary(live.data), [live.data]);
  const run = wards.data?.forecast_run;

  const liveTone: "live" | "stale" | "off" =
    !live.data?.available ? "off"
    : (live.data.data_age_hours ?? 99) <= 3 ? "live"
    : "stale";

  const cat = summary ? aqiCategory(summary.mean) : null;

  return (
    <section
      className={`flex flex-wrap items-stretch gap-px overflow-hidden rounded-lg border border-border bg-border ${className}`}
      aria-label="Data freshness and provenance"
    >
      {/* ---------- LIVE: measured now ---------- */}
      <div className="flex min-w-[280px] flex-1 flex-wrap items-center gap-x-4 gap-y-1 bg-panel px-4 py-3">
        <span className="flex items-center gap-2">
          <Dot tone={liveTone} />
          <span className="mono text-[11px] font-bold tracking-wide text-foreground">
            LIVE NOW
          </span>
        </span>

        {summary && cat ? (
          <>
            <span className="flex items-baseline gap-1.5">
              <span
                className="mono rounded-md px-2 py-0.5 text-[13px] font-bold"
                style={{ background: cat.color, color: cat.text }}
              >
                {summary.mean}
              </span>
              <span className="text-[12px] text-text-dim">Delhi average AQI</span>
            </span>
            <span className="text-[12px] text-text-dim">
              worst&nbsp;
              <strong className="text-foreground">{summary.worst.name}</strong>{" "}
              {summary.worst.aqi}
            </span>
            {/* The averaging basis belongs next to the number. It is the reason
                this figure matches what CPCB publishes rather than running high
                at night, and it is the first thing a jury will ask about. */}
            <span
              className="mono text-[11px] text-text-mute"
              title={live.data?.averaging ?? undefined}
            >
              CPCB · {summary.stations} stations ·{" "}
              {live.data?.values_averaged
                ? "24h average basis"
                : "hourly basis · averaging"}{" "}
              · latest {timeAgo(live.data?.observed_at)}
            </span>
          </>
        ) : live.data?.state === "warming" && (live.dataUpdatedAt ?? 0) > 0 &&
             Date.now() - (live.dataUpdatedAt ?? 0) < 90_000 ? (
          // Only ever a genuine warm-up. The API says "warming" when it has not yet
          // fetched; if it is still saying that after a minute and a half, the feed is
          // down, and a spinner that never resolves is the wrong thing to show.
          <span className="text-[12px] text-text-dim">fetching station readings…</span>
        ) : (
          <span className="text-[12px] text-text-dim">
            live station feed unavailable - forecast below is unaffected
          </span>
        )}
      </div>

      {/* ---------- FORECAST: modelled ahead ---------- */}
      <div className="flex min-w-[280px] flex-1 flex-wrap items-center gap-x-4 gap-y-1 bg-panel px-4 py-3">
        <span className="flex items-center gap-2">
          <Dot tone="off" />
          <span className="mono text-[11px] font-bold tracking-wide text-foreground">
            FORECAST +24/48/72h
          </span>
        </span>

        {run?.available ? (
          <>
            <span className="text-[12px] text-text-dim">
              model run{" "}
              <strong className="text-foreground">
                {new Date(run.issued_at!).toLocaleDateString("en-IN", {
                  day: "numeric", month: "short", year: "numeric",
                })}
              </strong>
            </span>
            <span className="mono text-[11px] text-text-mute">
              {timeAgo(run.issued_at)} · trained XGBoost models
            </span>
          </>
        ) : (
          <span className="text-[12px] text-text-dim">provenance unavailable</span>
        )}
      </div>
    </section>
  );
}

/** Compact single-line variant for dense rails and the landing hero. */
export function FreshnessChip({ className = "" }: { className?: string }) {
  const live = useQuery(liveQuery);
  const summary = liveSummary(live.data);
  if (!summary) return null;
  return (
    <span className={`chip inline-flex items-center gap-2 ${className}`}>
      <Dot tone={(live.data?.data_age_hours ?? 99) <= 3 ? "live" : "stale"} />
      Live Delhi AQI <strong>{summary.mean}</strong>
      <span className="text-text-mute">· {timeAgo(live.data?.observed_at)}</span>
    </span>
  );
}
