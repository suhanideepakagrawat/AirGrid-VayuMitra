// "How this works, in plain words" — every feature page explains its own
// method in language a non-engineer can follow. The steps are real sequences
// (data flows in this order through the pipeline), so the numbers carry
// information, not decoration.

type Step = { title: string; body: string };

type Method = {
  heading: string;
  steps: Step[];
  honest: string; // the honesty line — what this is NOT
};

export const METHODS = {
  forecast: {
    heading: "How we predict the air, in plain words",
    steps: [
      {
        title: "Learn from the past",
        body: "Three XGBoost models study months of CPCB sensor readings alongside weather — wind, temperature, humidity, season.",
      },
      {
        title: "Fill the gaps",
        body: "Around 60 CPCB, DPCC and IMD monitors report live across 1,500 km². A spatial model estimates the air in every 1-km square between them.",
      },
      {
        title: "Look ahead",
        body: "Separate models predict each square for tomorrow (+24 h), the day after (+48 h) and day 3 (+72 h).",
      },
      {
        title: "Say how sure we are",
        body: "Every number ships with a confidence score, and every screen states whether it is a live measurement or a model forecast, with its age.",
      },
    ],
    honest:
      "Validated, not vibes (Leave-One-Station-Out): spatial estimates beat IDW interpolation by 8% and nearest-station by 23%. Forecasts beat persistence at +48h/+72h; at +24h persistence stays competitive — a known property of day-ahead AQI — and our error (~43 AQI ≈ half a CPCB band) remains actionable.",
  },
  attribution: {
    heading: "How we name the source, in plain words",
    steps: [
      {
        title: "Check the wind",
        body: "For each square we ask: where did this air come from in the last few hours?",
      },
      {
        title: "Look upwind",
        body: "What sits along that path — 958,000 mapped road segments, 342 industrial sites, 175 construction sites, and live NASA satellite fire detections across Punjab and Haryana.",
      },
      {
        title: "Read the chemistry",
        body: "Live CPCB station readings separate the sources: NO₂ marks traffic, SO₂ marks industry, and a high PM10-to-PM2.5 ratio marks construction dust. Each is compared against the citywide level at that same moment.",
      },
      {
        title: "State the share",
        body: "Two independent lines of evidence — what sits upwind, and what the air is actually made of — combine into a source split with a confidence label per square.",
      },
    ],
    honest: "Directional evidence with stated confidence, cross-checked against live pollutant chemistry.",
  },
  enforcement: {
    heading: "How the ranking works, in plain words",
    steps: [
      {
        title: "How bad is it?",
        body: "Severity — the ward's peak forecast AQI over the next 72 hours.",
      },
      {
        title: "Who is causing it?",
        body: "Attribution — the dominant source in that ward, so the right team is sent (traffic police ≠ dust control).",
      },
      {
        title: "Does it stay bad?",
        body: "Persistence — wards that stay polluted across all three horizons outrank one-day spikes.",
      },
      {
        title: "Name the actual source",
        body: "Beyond the ward, we rank the individual sources behind it — a named road, a specific industrial site, a junction — by how much they contribute, how bad the areas they reach already are, and how many squares they touch.",
      },
      {
        title: "Rank and assign",
        body: "Each target carries a ward, coordinates, the team to send and the action to take. The queue is balanced across source types and spread across the city, so one van is never sent to four adjacent corners.",
      },
    ],
    honest: "Every target carries its evidence and confidence — a prioritisation aid for limited inspection capacity.",
  },
  advisory: {
    heading: "How the advice is made, in plain words",
    steps: [
      {
        title: "Start from your ward",
        body: "VayuMitra takes the same 72-hour forecast for the ward you name — one of 209 real Delhi wards.",
      },
      {
        title: "Apply the health rules",
        body: "CPCB band thresholds plus WHO guidance decide what's safe — for you specifically: child, elderly, asthma, pregnancy, outdoor work.",
      },
      {
        title: "Say it in your language",
        body: "The answer is composed in English or हिन्दी and can be spoken aloud — built for households, not dashboards.",
      },
      {
        title: "Cite every claim",
        body: "Each answer carries its authorities — CPCB · SAFAR · WHO · GRAP — tappable, with publisher and year.",
      },
    ],
    honest: "Guidance, not diagnosis — the model can only phrase what the cited health standards establish.",
  },
} satisfies Record<string, Method>;

export type MethodKey = keyof typeof METHODS;

export function MethodPanel({
  method,
  compact = false,
  className = "",
}: {
  method: MethodKey;
  /** Vertical steps for narrow rails/sidebars instead of the 4-up grid. */
  compact?: boolean;
  className?: string;
}) {
  const m = METHODS[method];
  return (
    <section
      className={`${compact ? "" : "panel p-6"} ${className}`}
      aria-label={m.heading}
    >
      <h2 className={compact ? "text-[14px] font-bold" : "text-lg font-bold"}>{m.heading}</h2>
      <ol className={compact ? "mt-4 space-y-4" : "mt-5 grid gap-x-8 gap-y-5 sm:grid-cols-2 lg:grid-cols-4"}>
        {m.steps.map((s, i) => (
          <li key={s.title} className="flex gap-3">
            <span
              aria-hidden
              className="mono grid h-7 w-7 shrink-0 place-items-center rounded-full bg-accent text-[12.5px] font-bold text-white"
            >
              {i + 1}
            </span>
            <div>
              <h3 className="text-[14px] font-bold leading-tight">{s.title}</h3>
              <p className="mt-1 text-[12.5px] leading-relaxed text-text-dim">{s.body}</p>
            </div>
          </li>
        ))}
      </ol>
      <p className="mono mt-5 border-t border-border pt-3 text-[11px] text-text-mute">
        {m.honest}
      </p>
    </section>
  );
}
