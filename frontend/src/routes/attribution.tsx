import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { SourceStrip, BandBadge, HorizonSwitch } from "@/components/charts";
import { MethodPanel } from "@/components/HowItWorks";
import {
  aqiCategory,
  asForecast,
  CELLS,
  isNow,
  SOURCE_COLORS,
  SOURCE_LABELS,
  type HorizonSel,
  type SourceKey,
} from "@/lib/air-data";
import { DelhiWardMap } from "@/components/DelhiWardMap";
import { liveQuery, wardAqiAt, wardsQuery, type LiveWard } from "@/lib/api";

export const Route = createFileRoute("/attribution")({
  head: () => ({
    meta: [
      { title: "Attribution - AirGrid NCR" },
      { name: "description", content: "City-wide source attribution across Delhi wards: who is polluting where." },
    ],
  }),
  component: Attribution,
});

const LIVE_SOURCE_META: Record<string, { label: string; color: string }> = {
  traffic: { label: "Traffic", color: "var(--source-traffic)" },
  industry: { label: "Industry", color: "var(--source-industry)" },
  construction: { label: "Construction dust", color: "var(--source-construction)" },
};

type MixRow = { key: string; label: string; color: string; pct: number };

function liveMix(wards: LiveWard[]): MixRow[] {
  // AQI-weighted mean of each ward's source split - loaded wards count more.
  const acc: Record<string, number> = { traffic: 0, industry: 0, construction: 0 };
  let weight = 0;
  for (const w of wards) {
    if (!w.sources) continue;
    for (const k of Object.keys(acc)) acc[k] += (w.sources as any)[k] * w.aqi;
    weight += w.aqi;
  }
  const total = Object.values(acc).reduce((a, b) => a + b, 0) || 1;
  return Object.entries(acc).map(([k, v]) => ({
    key: k,
    label: LIVE_SOURCE_META[k].label,
    color: LIVE_SOURCE_META[k].color,
    pct: (v / total) * 100,
  }));
}

function sampleMix(): MixRow[] {
  const totals: Record<SourceKey, number> = { traffic: 0, industry: 0, construction: 0, burning: 0 };
  CELLS.forEach((c) => {
    (Object.keys(totals) as SourceKey[]).forEach((k) => (totals[k] += c.attribution[k] * c.aqi));
  });
  const sum = Object.values(totals).reduce((a, b) => a + b, 0);
  return (Object.keys(totals) as SourceKey[]).map((k) => ({
    key: k,
    label: SOURCE_LABELS[k],
    color: SOURCE_COLORS[k],
    pct: (totals[k] / sum) * 100,
  }));
}

/** Mean source mix of wards grouped by CPCB band - the "who pollutes the worst
 *  wards" chart. Only bands with enough wards to mean something are shown. */
function mixByBand(
  wards: LiveWard[],
  aqiAt: (w: LiveWard) => number,
): { band: string; color: string; text: string; count: number; mix: MixRow[] }[] {
  const groups = new Map<string, LiveWard[]>();
  for (const w of wards) {
    if (!w.sources) continue;
    const label = aqiCategory(aqiAt(w)).label;
    groups.set(label, [...(groups.get(label) ?? []), w]);
  }
  const order = ["Good", "Satisfactory", "Moderate", "Poor", "Very Poor", "Severe"];
  return order
    .filter((b) => (groups.get(b)?.length ?? 0) >= 3)
    .map((b) => {
      const ws = groups.get(b)!;
      const acc: Record<string, number> = { traffic: 0, industry: 0, construction: 0 };
      for (const w of ws) for (const k of Object.keys(acc)) acc[k] += (w.sources as any)[k];
      const total = Object.values(acc).reduce((x, y) => x + y, 0) || 1;
      const sampleAqi = { Good: 40, Satisfactory: 80, Moderate: 150, Poor: 250, "Very Poor": 350, Severe: 450 }[b]!;
      const cat = aqiCategory(sampleAqi);
      return {
        band: b,
        color: cat.color,
        text: cat.text,
        count: ws.length,
        mix: Object.entries(acc).map(([k, v]) => ({
          key: k,
          label: LIVE_SOURCE_META[k].label,
          color: LIVE_SOURCE_META[k].color,
          pct: (v / total) * 100,
        })),
      };
    });
}

function Attribution() {
  const live = useQuery(wardsQuery());
  const nowQ = useQuery(liveQuery);
  // The page opened on the +24 h forecast without ever saying so, so a reader
  // comparing it with the dashboard's "Now" was comparing two different instants.
  // It now carries the same control the dashboard does, and defaults to Now.
  const [horizon, setHorizon] = useState<HorizonSel>("now");

  const liveAqi = useMemo(() => {
    const m = new Map<string, number>();
    if (nowQ.data?.available) for (const w of nowQ.data.wards) m.set(w.zone_id, w.aqi);
    return m;
  }, [nowQ.data]);

  const wards: LiveWard[] | null =
    live.isSuccess && live.data.wards.some((w) => w.sources) ? live.data.wards : null;

  const aqiAt = useMemo(() => {
    return (w: LiveWard): number => {
      if (isNow(horizon)) {
        const v = liveAqi.get(w.zone_id);
        if (typeof v === "number") return Math.round(v);
      }
      return Math.round(wardAqiAt(w, asForecast(horizon)));
    };
  }, [horizon, liveAqi]);

  const view = useMemo(() => {
    if (wards) {
      return {
        real: live.data!.data_kind === "real",
        mix: liveMix(wards),
        byBand: mixByBand(wards, aqiAt),
        top: [...wards].sort((a, b) => aqiAt(b) - aqiAt(a)).slice(0, 10).map((w) => ({
          id: w.zone_id,
          name: w.name,
          aqi: aqiAt(w),
          dominant: w.dominant_source ?? "-",
          confidence: w.confidence,
          mix: w.sources
            ? Object.entries(w.sources).map(([k, v]) => ({
                key: k,
                label: LIVE_SOURCE_META[k].label,
                color: LIVE_SOURCE_META[k].color,
                pct: v as number,
              }))
            : [],
        })),
        unit: `${wards.length} named Delhi wards`,
      };
    }
    return {
      real: false,
      mix: sampleMix(),
      byBand: [],
      top: [...CELLS].sort((a, b) => b.aqi - a.aqi).slice(0, 10).map((c) => ({
        id: c.id,
        name: c.ward,
        aqi: c.aqi,
        dominant: SOURCE_LABELS[c.dominantSource],
        confidence: c.confidence,
        mix: (Object.keys(c.attribution) as SourceKey[]).map((k) => ({
          key: k,
          label: SOURCE_LABELS[k],
          color: SOURCE_COLORS[k],
          pct: c.attribution[k] * 100,
        })),
      })),
      unit: "sample scene",
    };
  }, [wards, live.data, aqiAt]);

  return (
    <AppShell>
      <div className="mx-auto max-w-6xl px-6 py-8">
        <h1 className="font-display text-3xl">Who is polluting Delhi, ward by ward</h1>
        <p className="mt-2 max-w-2xl text-sm text-text-dim">
          {view.real
            ? "Live attribution from the trained pipeline - each ward's split of traffic, industry and construction, weighted by how loaded the ward is."
            : "Aggregated share of forecast AQI by source. Live ward attribution appears here as soon as the API answers."}
        </p>
        <p className="mono mt-1 text-[11px] text-text-mute">{view.unit} · AQI-weighted</p>

        <div className="mt-5 flex flex-wrap items-center gap-3">
          <HorizonSwitch value={horizon} onChange={setHorizon} compact />
          <span className="mono text-[11px] text-text-mute">
            {isNow(horizon)
              ? "AQI measured now (CPCB 24 h basis) · source mix from the trained pipeline"
              : `AQI forecast +${horizon} h · source mix from the trained pipeline`}
          </span>
        </div>

        <div className="mt-8 grid gap-6 md:grid-cols-2">
          <div className="panel p-6">
            <div className="mono text-[11px] text-text-mute">City-wide contribution mix</div>
            <div className="mt-4">
              <SourceStrip mix={view.mix} height={18} />
            </div>
            <ul className="mono mt-6 space-y-2 text-sm">
              {view.mix.map((s) => (
                <li key={s.key} className="flex items-center justify-between border-b border-border pb-2 last:border-0">
                  <span className="flex items-center gap-2 text-text-dim">
                    <span className="h-2 w-2" style={{ background: s.color }} />
                    {s.label}
                  </span>
                  <span className="text-foreground">{s.pct.toFixed(1)}%</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="panel p-6">
            <div className="mono text-[11px] text-text-mute">
              {view.byBand.length ? "Source mix by severity - who runs the worst wards" : "Reading this page"}
            </div>
            {view.byBand.length ? (
              <ul className="mt-4 space-y-4">
                {view.byBand.map((b) => (
                  <li key={b.band}>
                    <div className="mb-1.5 flex items-center justify-between">
                      <span
                        className="mono rounded-md px-2 py-0.5 text-[11px] font-bold"
                        style={{ background: b.color, color: b.text }}
                      >
                        {b.band}
                      </span>
                      <span className="mono text-[11px] text-text-mute">{b.count} wards</span>
                    </div>
                    <SourceStrip mix={b.mix} height={14} />
                  </li>
                ))}
                <li className="mono pt-1 text-[11px] text-text-mute">
                  Read down the strips: as wards get worse, watch which color grows - that's the
                  source enforcement should chase first.
                </li>
              </ul>
            ) : (
              <p className="mt-4 text-sm text-text-dim">
                Each ward's forecast is split into source shares.
                The strips group wards by CPCB band so you can see which source dominates
                as air gets worse - the signal the enforcement queue is built on.
              </p>
            )}
          </div>
        </div>

        {/* The ten worst wards on the real map, ringed red, beside the same ten in
            the table. A ranked list answers "which"; the map answers "where", and
            enforcement needs both. */}
        <div className="panel mt-6 overflow-hidden p-0">
          <div className="grid gap-px bg-border lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
            <div className="bg-panel p-4">
              <div className="mono mb-2 text-[11px] text-text-mute">
                Worst 10 wards · outlined{isNow(horizon) ? " · measured now" : ` · +${horizon} h`}
              </div>
              <div className="h-[300px] md:h-[380px]">
                <DelhiWardMap
                  liveWards={wards}
                  horizon={horizon}
                  liveAqi={liveAqi}
                  urgentIds={view.top.map((r) => r.id)}
                  selectedId={null}
                />
              </div>
              <p className="mono mt-2 text-[11px] text-text-mute">
                Hover a ward for its name, AQI and dominant source. Use + / &minus; to zoom.
              </p>
            </div>
            <div className="bg-panel p-6">
          <div className="mono text-[11px] text-text-mute">
            Worst 10 wards · {isNow(horizon) ? "measured now" : `forecast +${horizon} h`}
          </div>
          <div className="overflow-x-auto">
            <table className="mono mt-4 w-full min-w-[560px] text-sm">
              <thead className="mono text-[11px] text-text-mute">
                <tr className="border-b border-border">
                  <th className="py-2 text-left">Ward</th>
                  <th className="text-right">AQI</th>
                  <th className="pl-4 text-left">Band</th>
                  <th className="pl-6 text-left">Source mix</th>
                  <th className="pl-4 text-left">Dominant source</th>
                </tr>
              </thead>
              <tbody>
                {view.top.map((r) => (
                  <tr key={r.id} className="border-b border-border/50 last:border-0">
                    <td className="py-2.5 text-foreground">{r.name}</td>
                    <td className="text-right font-bold text-foreground">{Math.round(r.aqi)}</td>
                    <td className="pl-4"><BandBadge aqi={r.aqi} /></td>
                    <td className="pl-6"><div className="w-36"><SourceStrip mix={r.mix} height={10} /></div></td>
                    <td className="pl-4 text-text-dim">{r.dominant}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
            </div>
          </div>
        </div>

        <div className="mt-8">
          <MethodPanel method="attribution" />
        </div>
      </div>
    </AppShell>
  );
}
