import { createFileRoute, Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { DelhiWardMap } from "@/components/DelhiWardMap";
import { LogoMark } from "@/components/AppShell";
import {
  BandDistribution,
  CityTrend,
  HorizonSwitch,
  RankBars,
  AqiBall,
  BandBadge,
  DeltaTag,
} from "@/components/charts";
import { MethodPanel } from "@/components/HowItWorks";
import { MyWardChip } from "@/components/MyWardChip";
import { useMyWard, type MyWard } from "@/lib/locate";
import {
  CELLS,
  cellAqi,
  HORIZONS,
  SOURCE_COLORS,
  SOURCE_EVIDENCE,
  SOURCE_BASIS,
  SOURCE_LABELS,
  type Horizon,
  type SourceKey,
} from "@/lib/air-data";
import {
  CITIZEN_APP_URL,
  liveQuery,
  wardAqiAt,
  wardsQuery,
  type LiveWard,
} from "@/lib/api";

export const Route = createFileRoute("/")({
  component: Landing,
});

function Landing() {
  const live = useQuery(wardsQuery());
  const heroWards = live.isSuccess && live.data.wards.length > 0 ? live.data.wards : null;
  const my = useMyWard();

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* Top bar */}
      <header className="relative z-20 flex items-center justify-between px-6 py-5">
        <div className="flex items-center gap-2">
          <LogoMark />
          <span className="font-display text-sm font-semibold">
            AirGrid<span className="text-accent">·</span>NCR
          </span>
        </div>
        <nav className="mono hidden items-center gap-6 text-[11px] text-text-dim md:flex">
          <a href="#forecast" className="hover:text-foreground">Forecast</a>
          <a href="#attribution" className="hover:text-foreground">Sources</a>
          <a href="#vayumitra" className="hover:text-foreground">VayuMitra</a>
          <a href="#audiences" className="hover:text-foreground">For operators</a>
        </nav>
        <Link
          to="/dashboard"
          className="inline-flex items-center gap-1.5 rounded-full bg-accent px-4 py-2 text-[12px] font-semibold text-white hover:bg-[#064a42]"
        >
          Open dashboard <span aria-hidden>→</span>
        </Link>
      </header>

      {/* Hero — the claim on the left, the real city on the right */}
      <section className="border-b border-border">
        <div className="mx-auto grid max-w-6xl items-center gap-12 px-6 pb-20 pt-10 md:pt-16 lg:grid-cols-[minmax(0,11fr)_minmax(0,9fr)]">
          <div>
            <div className="chip mb-6" style={{ color: "var(--accent)", borderColor: "var(--accent-dim)" }}>
              <span className="h-1.5 w-1.5 rounded-full bg-accent cell-pulse" />
              Delhi pilot · 209 wards live · English + हिन्दी
            </div>
            <h1 className="text-4xl font-bold leading-[1.08] tracking-tight md:text-5xl">
              Delhi's air, 72 hours ahead.
              <br />
              <span className="text-accent">Ward by ward. Source by source.</span>
            </h1>
            <p className="mt-6 max-w-xl text-base text-text-dim md:text-lg">
              AirGrid turns the city's 60-plus government monitors into an intelligence layer:
              a three-day forecast for every ward, the dominant polluter named with
              evidence and confidence, deployment orders for inspection teams — and
              health advice any family can act on, in English and हिन्दी.
            </p>

            <div className="mt-9 flex flex-wrap items-center gap-3">
              <a
                href="#forecast"
                className="inline-flex items-center gap-2 rounded-full bg-accent px-6 py-3 text-sm font-semibold text-white hover:bg-[#064a42]"
              >
                Explore the live forecast <span aria-hidden>↓</span>
              </a>
              <a
                href="#vayumitra"
                className="inline-flex items-center gap-2 rounded-full border-[1.5px] border-accent-dim px-6 py-3 text-sm font-semibold text-accent hover:bg-accent hover:text-white"
              >
                Talk to VayuMitra
              </a>
              <Link
                to="/dashboard"
                className="inline-flex items-center gap-2 px-2 py-3 text-sm font-semibold text-text-dim hover:text-foreground"
              >
                Operator dashboard →
              </Link>
            </div>

            {/* Coordinate readout */}
            <div className="mono mt-14 grid max-w-xl grid-cols-2 gap-x-8 gap-y-3 border-t border-border pt-6 text-[11px] text-text-mute sm:grid-cols-4">
              <Readout k="Grid" v="1,600 × 1 km²" />
              <Readout k="Horizon" v="24 / 48 / 72 h" />
              <Readout k="Wards" v="209 named (MCD)" />
              <Readout k="Models" v="3 trained XGBoost" />
            </div>
          </div>

          {/* The real MCD ward map, colored by the live forecast */}
          <div>
            <div className="h-[380px] md:h-[480px]">
              <DelhiWardMap
                liveWards={heroWards}
                horizon="24"
                hereId={my.status === "found" ? my.zone?.zone_id ?? null : null}
                ambient
              />
            </div>
            <p className="mono mt-3 text-center text-[11px] text-text-mute">
              {heroWards
                ? "Real MCD ward boundaries · colored by tomorrow's live forecast"
                : "Real MCD ward boundaries · live colors load with the pipeline"}
            </p>
          </div>
        </div>
      </section>

      {/* Forecast explorer — the prediction, interactive */}
      <ForecastExplorer my={my} />

      {/* Attribution section */}
      <section id="attribution" className="border-t border-border">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <div className="mb-12 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
            <div>
              <h2 className="max-w-2xl font-display text-3xl font-semibold md:text-4xl">
                Four sources. Each detectable from independent physical evidence.
              </h2>
            </div>
            <p className="max-w-md text-sm text-text-dim">
              The engine does not average a national blend. It separates traffic, industry,
              construction dust, and regional biomass burning per cell — and states why.
            </p>
          </div>

          <div className="grid gap-px overflow-hidden border border-border bg-border md:grid-cols-2">
            {(Object.keys(SOURCE_LABELS) as SourceKey[]).map((k) => (
              <SourceCard key={k} k={k} />
            ))}
          </div>

          <div className="mt-8">
            <MethodPanel method="attribution" />
          </div>
        </div>
      </section>

      {/* VayuMitra — the citizen product, live on the page */}
      <VayuMitraSection />

      {/* Dual audience */}
      <section id="audiences" className="border-t border-border">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <div className="mb-10">
            <h2 className="max-w-3xl font-display text-3xl font-semibold md:text-4xl">
              Built for operators making decisions. Legible enough for the citizens they serve.
            </h2>
          </div>
          <div className="grid gap-6 md:grid-cols-2">
            <AudiencePanel
              tag="Enforcement"
              title="A ranked action list, not a wall of numbers."
              body="Deployment scores fuse forecast severity, source attribution, and persistence across all 209 wards. Every entry names the team to send and links to the evidence that justifies it."
              stat="209"
              statLabel="Wards ranked for deployment"
              cta={{ to: "/enforcement", label: "Enforcement queue" }}
            />
            <AudiencePanel
              tag="Citizen"
              title="Exposure at your address, in plain language."
              body="Instead of a single color, you get: what tomorrow looks like, which source is driving it, and — if you're in a vulnerable group — what to actually do about it."
              stat="5"
              statLabel="Personas with tailored guidance"
              cta={{ to: "/health", label: "Health advisory" }}
            />
          </div>
        </div>
      </section>

      {/* Method / CTA */}
      <section id="method" className="border-t border-border bg-bg-secondary">
        <div className="mx-auto max-w-6xl px-6 py-24 text-center">
          <h2 className="mx-auto max-w-3xl font-display text-3xl font-semibold md:text-5xl">
            Stop asking <span className="text-text-mute">"how bad is it?"</span>
            <br />Start asking <span className="text-accent">"what do we do about it?"</span>
          </h2>
          <div className="mt-10 flex flex-wrap justify-center gap-3">
            <Link
              to="/dashboard"
              className="inline-flex items-center gap-2 rounded-full bg-accent px-6 py-3 text-sm font-semibold text-white hover:bg-[#064a42]"
            >
              Open the dashboard <span aria-hidden>→</span>
            </Link>
            <Link
              to="/attribution"
              className="inline-flex items-center gap-2 rounded-full border-[1.5px] border-accent-dim px-6 py-3 text-sm font-semibold text-accent hover:bg-accent hover:text-white"
            >
              City-wide trends
            </Link>
          </div>
        </div>
      </section>

      <footer className="border-t border-border">
        <div className="mono mx-auto flex max-w-6xl flex-col items-start gap-4 px-6 py-8 text-[11px] text-text-mute md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-2">
            <LogoMark />
            <span>AirGrid · NCR pipeline v0.4</span>
          </div>
          <div className="flex gap-6">
            <span>Data: CPCB · IMD · MODIS/VIIRS · DPCC</span>
            <span>Not an official government product</span>
          </div>
        </div>
      </footer>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Forecast explorer — click a horizon, watch every number move        */
/* ------------------------------------------------------------------ */

function ForecastExplorer({ my }: { my: MyWard }) {
  const [horizon, setHorizon] = useState<Horizon>("24");
  const live = useQuery(wardsQuery());
  const wards: LiveWard[] | null =
    live.isSuccess && live.data.wards.length > 0 ? live.data.wards : null;

  const view = useMemo(() => {
    const mean = (xs: number[]) => Math.round(xs.reduce((a, b) => a + b, 0) / (xs.length || 1));
    if (wards) {
      const perH = Object.fromEntries(
        HORIZONS.map((h) => [h, wards.map((w) => wardAqiAt(w, h))]),
      ) as Record<Horizon, number[]>;
      const trend = Object.fromEntries(HORIZONS.map((h) => [h, mean(perH[h])])) as Record<Horizon, number>;
      const ranked = [...wards]
        .map((w) => ({
          id: w.zone_id,
          name: w.name,
          value: wardAqiAt(w, horizon),
          sub: w.dominant_source ?? undefined,
        }))
        .sort((a, b) => b.value - a.value)
        .slice(0, 8);
      return {
        real: live.data!.data_kind === "real",
        unitLabel: `${wards.length} named Delhi wards`,
        aqis: perH[horizon],
        trend,
        ranked,
      };
    }
    const perH = Object.fromEntries(
      HORIZONS.map((h) => [h, CELLS.map((c) => cellAqi(c, h))]),
    ) as Record<Horizon, number[]>;
    const trend = Object.fromEntries(HORIZONS.map((h) => [h, mean(perH[h])])) as Record<Horizon, number>;
    const ranked = [...CELLS]
      .map((c) => ({ id: c.id, name: c.ward, value: cellAqi(c, horizon), sub: SOURCE_LABELS[c.dominantSource] }))
      .sort((a, b) => b.value - a.value)
      .slice(0, 8);
    return { real: false, unitLabel: "sample scene (API warming up)", aqis: perH[horizon], trend, ranked };
  }, [wards, horizon, live.data]);

  const avg = view.trend[horizon];

  return (
    <section id="forecast" className="border-t border-border bg-bg-secondary">
      <div className="mx-auto max-w-6xl px-6 py-24">
        <div className="mb-10 flex flex-col gap-6 md:flex-row md:items-end md:justify-between">
          <div>
            <h2 className="max-w-2xl font-display text-3xl font-semibold md:text-4xl">
              Click a horizon. Watch the whole city move.
            </h2>
            <p className="mt-4 max-w-2xl text-sm text-text-dim">
              {view.real
                ? "These are the actual trained-model forecasts for every named Delhi ward — not an illustration. Switch the horizon and every chart re-ranks."
                : "Live pipeline output for every Delhi ward. If the API is still waking, a labeled sample scene stands in — the interaction is identical."}
            </p>
          </div>
          <div className="flex flex-col items-start gap-3 md:items-end">
            <HorizonSwitch value={horizon} onChange={setHorizon} />
            <MyWardChip my={my} horizon={horizon} />
          </div>
        </div>

        <div className="grid gap-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
          <div className="panel p-6">
            <div className="flex items-center gap-4">
              <AqiBall aqi={avg} size={64} />
              <div>
                <div className="text-lg font-bold">Delhi average · +{horizon} h</div>
                <div className="mt-1 flex items-center gap-2">
                  <BandBadge aqi={avg} />
                  <DeltaTag now={avg} base={view.trend["24"]} />
                </div>
              </div>
            </div>
            <div className="mt-6 border-t border-border pt-4">
              <div className="mono mb-1 text-[11px] text-text-mute">City trajectory · mean AQI</div>
              <CityTrend values={view.trend} active={horizon} />
            </div>
            <div className="mt-4 border-t border-border pt-4">
              <div className="mono mb-2 text-[11px] text-text-mute">
                Where the wards sit · CPCB bands
              </div>
              <BandDistribution aqis={view.aqis} caption={view.unitLabel} />
            </div>
          </div>

          <div className="panel p-6">
            <div className="mb-4 flex items-baseline justify-between">
              <h3 className="text-[14px] font-bold">Worst first · +{horizon} h</h3>
              <span className="mono text-[11px] text-text-mute">
                {view.real ? "live pipeline" : "sample"} · dominant source at right
              </span>
            </div>
            <RankBars rows={view.ranked} />
            <div className="mt-4 border-t border-border pt-3">
              <Link to="/dashboard" className="mono text-[12px] font-semibold text-accent hover:underline">
                Explore all wards on the map →
              </Link>
            </div>
          </div>
        </div>

        <div className="mt-8">
          <MethodPanel method="forecast" />
        </div>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------ */
/* VayuMitra — the citizen assistant, embedded live                    */
/* ------------------------------------------------------------------ */

function VayuMitraSection() {
  return (
    <section id="vayumitra" className="border-t border-border bg-bg-secondary">
      <div className="mx-auto max-w-6xl px-6 py-24">
        <div className="grid items-center gap-12 lg:grid-cols-[minmax(0,6fr)_minmax(0,5fr)]">
          <div>
            <h2 className="max-w-xl font-display text-3xl font-semibold md:text-4xl">
              Meet VayuMitra — the same forecast, speaking your language.
            </h2>
            <p className="mt-4 max-w-xl text-sm text-text-dim md:text-base">
              Dashboards serve operators. VayuMitra serves everyone else: ask about your ward
              in English or हिन्दी, by typing or talking, and get advice tuned to who you are —
              a parent, an elderly person, an asthmatic, an outdoor worker.
            </p>
            <ul className="mt-6 space-y-3 text-sm text-text-dim">
              {[
                ["Persona-aware", "“Can my child play outside?” answers differently than “can I go for a run?”"],
                ["Every answer cited", "CPCB National AQI · SAFAR · WHO 2021 · the active GRAP stage — tappable sources, no invented thresholds."],
                ["Voice in and out", "Neural speech with pause and stop, mic input — built for low-literacy users, not just app-natives."],
                ["Never breaks", "No key → deterministic templates. No data → labeled sample. No network → browser voice."],
              ].map(([t, b]) => (
                <li key={t} className="flex gap-3">
                  <span aria-hidden className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-accent text-[11px] font-bold text-white">✓</span>
                  <span><b className="text-foreground">{t}.</b> {b}</span>
                </li>
              ))}
            </ul>
            <div className="mt-8 flex flex-wrap gap-3">
              <a
                href={CITIZEN_APP_URL}
                target="_blank"
                rel="noopener"
                className="inline-flex items-center gap-2 rounded-full bg-accent px-6 py-3 text-sm font-semibold text-white hover:bg-[#064a42]"
              >
                Open VayuMitra full-screen ↗
              </a>
              <Link
                to="/health"
                className="inline-flex items-center gap-2 rounded-full border-[1.5px] border-accent-dim px-6 py-3 text-sm font-semibold text-accent hover:bg-accent hover:text-white"
              >
                Operator view of the advisory
              </Link>
            </div>
          </div>

          {/* Live embed in a phone frame — this is the real deployed product */}
          <div className="mx-auto w-full max-w-[380px]">
            <div className="overflow-hidden rounded-[28px] border border-border bg-panel shadow-[0_8px_28px_rgba(9,20,28,0.18)]">
              <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
                <span className="text-[12px] font-bold">VayuMitra · live</span>
                <span className="mono flex items-center gap-1.5 text-[10.5px] text-text-mute">
                  <span className="h-1.5 w-1.5 rounded-full bg-[#009966]" /> deployed service
                </span>
              </div>
              <iframe
                src={CITIZEN_APP_URL}
                title="VayuMitra citizen advisory (live)"
                className="h-[560px] w-full border-0 bg-white"
                loading="lazy"
                allow="microphone"
              />
            </div>
            <p className="mono mt-3 text-center text-[11px] text-text-mute">
              This is the deployed app, not a mockup — try it right here.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}

function Readout({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-text-mute">{k}</span>
      <span className="text-foreground">{v}</span>
    </div>
  );
}

/** How many wards are currently showing each signature, straight from /live. Turns a
 *  static method description into a live claim a reader can check against the map. */
function useLiveSourceCounts() {
  const live = useQuery(liveQuery);
  return useMemo(() => {
    if (!live.data?.available) return null;
    const counts: Record<string, number> = { traffic: 0, industry: 0, construction: 0 };
    for (const w of live.data.wards) {
      const d = w.fingerprint?.dominant;
      if (d && d in counts) counts[d] += 1;
    }
    return { counts, burning: live.data.regional_burning, total: live.data.wards.length };
  }, [live.data]);
}

function SourceCard({ k }: { k: SourceKey }) {
  const live = useLiveSourceCounts();

  // What the live data says about this source right now.
  let reading: string | null = null;
  if (live) {
    if (k === "burning") {
      const rb = live.burning;
      reading = rb?.available ? (rb.evidence ?? null) : null;
    } else {
      const n = live.counts[k] ?? 0;
      reading =
        n > 0
          ? `${n} of ${live.total} wards are showing this signature right now`
          : "no ward is showing this signature right now";
    }
  }

  return (
    <div className="bg-bg-primary p-6">
      <div className="mb-4 flex items-center gap-3">
        <span
          className="h-2.5 w-2.5"
          style={{ background: SOURCE_COLORS[k] }}
          aria-hidden
        />
        <span className="mono text-[11px] text-text-mute">Source</span>
        <span className="font-display text-lg text-foreground">{SOURCE_LABELS[k]}</span>
      </div>
      <p className="text-sm text-text-dim">{SOURCE_EVIDENCE[k]}</p>
      {reading && (
        <p className="mono mt-3 border-l-2 border-accent pl-3 text-[12px] text-foreground">
          {reading}
        </p>
      )}
      <div className="mono mt-4 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-text-mute">
        <span>Evidence</span>
        {SOURCE_BASIS[k].map((b) => (
          <span key={b}>· {b}</span>
        ))}
      </div>
    </div>
  );
}

function AudiencePanel({
  tag,
  title,
  body,
  stat,
  statLabel,
  cta,
}: {
  tag: string;
  title: string;
  body: string;
  stat: string;
  statLabel: string;
  cta: { to: "/enforcement" | "/health"; label: string };
}) {
  return (
    <div className="panel flex flex-col p-6">
      <div className="chip w-fit" style={{ color: "var(--accent)", borderColor: "var(--accent-dim)" }}>{tag}</div>
      <h3 className="mt-4 font-display text-xl">{title}</h3>
      <p className="mt-3 flex-1 text-sm text-text-dim">{body}</p>
      <div className="mt-6 flex items-end justify-between border-t border-border pt-4">
        <div>
          <div className="mono text-3xl text-accent">{stat}</div>
          <div className="mono text-[11px] text-text-mute">{statLabel}</div>
        </div>
        <Link
          to={cta.to}
          className="mono border border-border px-3 py-1.5 text-[11px] text-text-dim hover:border-accent hover:text-accent"
        >
          {cta.label} →
        </Link>
      </div>
    </div>
  );
}
