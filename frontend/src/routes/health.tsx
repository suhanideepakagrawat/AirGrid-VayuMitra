import { createFileRoute } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { MethodPanel } from "@/components/HowItWorks";
import { aqiCategory } from "@/lib/air-data";
import { CITIZEN_APP_URL, liveQuery, timeAgo, wardsQuery } from "@/lib/api";

export const Route = createFileRoute("/health")({
  head: () => ({
    meta: [
      { title: "Citizen advisory — AirGrid NCR" },
      { name: "description", content: "Ward-level risk for sensitive groups and the multilingual VayuMitra citizen advisory, live from the deployed pipeline." },
    ],
  }),
  component: Health,
});

/**
 * The personas VayuMitra itself supports (advisory/personas.py), so this panel and
 * the assistant beside it give the same answer to the same question.
 */
const PERSONAS = [
  { key: "child", label: "Child" },
  { key: "elderly", label: "Elderly" },
  { key: "respiratory", label: "Asthma / heart" },
  { key: "outdoor_worker", label: "Outdoor worker" },
  { key: "pregnant", label: "Pregnant" },
  { key: "general", label: "General public" },
] as const;

type PersonaKey = (typeof PERSONAS)[number]["key"];

/**
 * Guidance for one persona at one AQI.
 *
 * Every persona gets distinct wording in EVERY band, including clean air. The first
 * version collapsed to a single shared sentence below AQI 100, which meant that on a
 * good day — like the 94 Delhi was reading when this was tested — the persona
 * selector appeared to do nothing at all. A control that visibly does nothing reads
 * as broken, and the differences are real regardless: an outdoor worker spends eight
 * hours in it, a child breathes faster per kilo of body weight, and someone with
 * asthma reacts further down the scale than a healthy adult.
 *
 * Thresholds follow the CPCB bands, whose own notes flag lung, asthma and heart
 * patients from "Moderate" (101–200) upward while healthy adults are largely
 * unaffected until "Poor".
 */
function guidanceFor(persona: PersonaKey, aqi: number): string {
  const band =
    aqi > 300 ? "severe" : aqi > 200 ? "verypoor" : aqi > 100 ? "moderate" : "ok";

  const table: Record<PersonaKey, Record<string, string>> = {
    child: {
      severe: "Keep children indoors. No outdoor sport or PE. An N95 is needed for any unavoidable trip.",
      verypoor: "No outdoor games today. Move PE indoors and keep classroom windows shut.",
      moderate: "Shorten outdoor play and keep it away from main roads.",
      ok: "Fine for outdoor play. Prefer parks over roadsides where you can.",
    },
    elderly: {
      severe: "Remain indoors. Postpone walks and errands until the air improves.",
      verypoor: "Avoid morning and evening walks; step out only if you must.",
      moderate: "Walk later in the morning once traffic thins, and keep it gentle.",
      ok: "Good conditions for a walk. Early morning is cleanest.",
    },
    respiratory: {
      severe: "Stay indoors with windows shut. Keep reliever medication to hand and seek help early if breathing worsens.",
      verypoor: "Avoid going out. Carry your inhaler if you must.",
      moderate: "You may notice mild breathing discomfort — keep exertion light and carry your inhaler.",
      ok: "Comfortable for you today. Carry your inhaler as usual.",
    },
    outdoor_worker: {
      severe: "Rotate to indoor tasks where possible. An N95 is essential, with breaks away from roadsides.",
      verypoor: "Wear an N95 and take a ten-minute indoor break every hour.",
      moderate: "Wear a mask near traffic and take a short break each hour.",
      ok: "Safe for a full shift. Keep water handy and take normal breaks.",
    },
    pregnant: {
      severe: "Stay indoors and avoid all exertion outdoors.",
      verypoor: "Limit time outdoors and avoid busy roads.",
      moderate: "Keep outings short and away from heavy traffic.",
      ok: "Fine to be out. Quieter streets are still the better choice.",
    },
    general: {
      severe: "Avoid outdoor exertion. Keep windows closed.",
      verypoor: "Limit prolonged outdoor exertion.",
      moderate: "Acceptable for most people. Reduce long, heavy outdoor exertion.",
      ok: "Air is acceptable. Normal activity is fine.",
    },
  };

  return table[persona][band];
}

function advisoryHi(aqi: number): string {
  if (aqi > 300) return "बाहरी गतिविधियाँ रोकें · मास्क अनिवार्य";
  if (aqi > 200) return "बाहरी समय सीमित करें";
  if (aqi > 100) return "बाहरी समय थोड़ा कम रखें";
  return "सामान्य गतिविधि ठीक है";
}

function Health() {
  const forecast = useQuery(wardsQuery());
  const liveNow = useQuery(liveQuery);
  const [showMethod, setShowMethod] = useState(false);
  const [persona, setPersona] = useState<PersonaKey>("child");

  /**
   * Real wards ranked by measured risk, worst first.
   *
   * This replaces a list of invented institutions — "DPS Dwarka · 610 people
   * exposed", "Sanjay Gandhi Memorial · 180" — real school and hospital names
   * carrying fabricated exposure counts, ranked on synthetic cell AQI. Naming a real
   * school and inventing how many children it exposes was the least defensible thing
   * on the site. The question the panel answers is unchanged; it is now answered with
   * the 209 real wards and live station readings.
   */
  const rows = useMemo(() => {
    const src = liveNow.data?.available ? liveNow.data.wards : [];
    return [...src]
      .sort((a, b) => b.aqi - a.aqi)
      .slice(0, 25)
      .map((w) => ({
        id: w.zone_id,
        name: w.name,
        aqi: w.aqi,
        cat: aqiCategory(w.aqi),
        pollutant: w.dominant_pollutant,
        station: w.nearest_station,
        km: w.nearest_station_km,
      }));
  }, [liveNow.data]);

  return (
    <AppShell>
      {/* Viewport-locked only from md up; a phone scrolls the page so the ward
          risk list under the assistant is reachable. */}
      <div className="grid grid-cols-1 md:[@media(min-height:820px)]:h-[calc(100vh-57px)] md:grid-cols-[1fr_440px]">
        {/* VayuMitra — the real deployed citizen product, embedded live */}
        <section className="relative h-[70vh] min-h-[420px] overflow-hidden border-b border-border bg-surface-1 md:h-auto md:min-h-0 md:border-b-0 md:border-r">
          <div className="flex items-center justify-between border-b border-border bg-panel px-5 py-3">
            <div>
              <h1 className="text-base font-bold">VayuMitra — citizen advisory</h1>
              <p className="text-[12px] text-text-dim">
                The live multilingual assistant (English · हिन्दी, voice-enabled), embedded from the deployed service.
              </p>
            </div>
            <a
              href={CITIZEN_APP_URL}
              target="_blank"
              rel="noopener"
              className="shrink-0 rounded-full bg-accent px-4 py-2 text-[12px] font-semibold text-white hover:bg-[#064a42]"
            >
              Open full app →
            </a>
          </div>
          <iframe
            src={CITIZEN_APP_URL}
            title="VayuMitra citizen advisory (live)"
            className="h-[calc(100%-61px)] w-full border-0 bg-white"
            loading="lazy"
            allow="microphone; autoplay"
          />
        </section>

        {/* Ward risk for sensitive groups, from live station readings */}
        <aside className="bg-panel md:[@media(min-height:820px)]:overflow-y-auto">
          <div className="border-b border-border p-5">
            <div className="chip mb-3">Highest risk right now</div>
            <h2 className="text-lg font-bold">Where sensitive groups are most at risk</h2>
            <p className="mt-2 text-sm text-text-dim">
              Delhi's wards ranked by air measured in the last hour, worst first. Choose who you
              are asking for and the guidance changes with them — the same CPCB and WHO persona
              rules VayuMitra uses, not a generic public warning.
            </p>
            {liveNow.data?.available && (
              <p className="mono mt-2 text-[11px] text-text-mute">
                {liveNow.data.stations} CPCB stations · measured {timeAgo(liveNow.data.observed_at)}
                {forecast.isSuccess && <> · {forecast.data.count} wards</>}
              </p>
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
              {showMethod ? "Hide the method" : "How is the advice made?"}
            </button>
            {showMethod && (
              <div className="mt-4 border-t border-border pt-4">
                <MethodPanel method="advisory" compact />
              </div>
            )}
          </div>

          <div className="flex flex-wrap gap-2 border-b border-border px-5 py-3">
            {PERSONAS.map((p) => (
              <button
                key={p.key}
                onClick={() => setPersona(p.key)}
                aria-pressed={persona === p.key}
                className={`rounded-full border px-3 py-1.5 text-[12px] font-semibold transition-colors ${
                  persona === p.key
                    ? "border-accent bg-accent text-white"
                    : "border-border text-text-dim hover:border-accent-dim hover:text-accent"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>

          {rows.length === 0 ? (
            <p className="p-5 text-sm text-text-dim">
              {liveNow.data?.state === "warming"
                ? "Fetching live station readings…"
                : "Live station feed unavailable — VayuMitra beside this panel still answers from the forecast."}
            </p>
          ) : (
            <ul>
              {rows.map((r) => (
                <li key={r.id} className="border-b border-border p-5">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-sm font-semibold">{r.name}</span>
                    <span
                      className="mono shrink-0 rounded-md px-2 py-0.5 text-[12px] font-bold"
                      style={{ background: r.cat.color, color: r.cat.text }}
                    >
                      {r.aqi}
                    </span>
                  </div>
                  <div className="mono mt-1 flex flex-wrap items-center gap-x-3 text-[11px] text-text-mute">
                    <span>{r.cat.label}</span>
                    <span>·</span>
                    <span>driven by {r.pollutant}</span>
                    <span>·</span>
                    <span>
                      {r.station} ({r.km} km)
                    </span>
                  </div>
                  <div className="mt-3 rounded-md bg-surface-1 px-3 py-2 text-[14px] text-text-dim">
                    {guidanceFor(persona, r.aqi)}
                  </div>
                  <div className="mono mt-2 text-[11px] text-text-mute">
                    हिंदी: {advisoryHi(r.aqi)}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </aside>
      </div>
    </AppShell>
  );
}
