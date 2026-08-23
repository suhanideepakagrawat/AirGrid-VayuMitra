// The "find my ward" affordance - one pill that walks through the whole
// geolocation story: ask → locating → found (with live AQI + a warning when
// the user's own air is bad) → or an honest denied/outside message.

import { aqiCategory, asForecast, type HorizonSel } from "@/lib/air-data";
import { wardAqiAt } from "@/lib/api";
import type { MyWard } from "@/lib/locate";

function PinGlyph({ className = "" }: { className?: string }) {
  return (
    <svg width="13" height="13" viewBox="0 0 14 14" aria-hidden className={className}>
      <path
        d="M7 1.2c2.3 0 4.1 1.8 4.1 4.1 0 3-4.1 7.5-4.1 7.5S2.9 8.3 2.9 5.3C2.9 3 4.7 1.2 7 1.2z"
        fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"
      />
      <circle cx="7" cy="5.3" r="1.6" fill="currentColor" />
    </svg>
  );
}

export function MyWardChip({
  my,
  horizon,
  onGo,
}: {
  my: MyWard;
  horizon: HorizonSel;
  onGo?: () => void;
}) {
  if (my.status === "unsupported") return null;

  if (my.status === "idle" || my.status === "error") {
    return (
      <button
        onClick={my.request}
        className="inline-flex items-center gap-1.5 rounded-full border-[1.5px] border-accent-dim px-3.5 py-1.5 text-[12px] font-semibold text-accent transition-colors hover:bg-accent hover:text-white"
        title="Uses your browser location once to find your MCD ward - nothing is stored."
      >
        <PinGlyph />
        {my.status === "error" ? "Retry my location" : "Use my location"}
      </button>
    );
  }

  if (my.status === "locating") {
    return (
      <span className="mono inline-flex items-center gap-1.5 rounded-full border border-border px-3.5 py-1.5 text-[12px] text-text-dim">
        <PinGlyph className="animate-pulse" />
        Locating…
      </span>
    );
  }

  if (my.status === "denied") {
    return (
      <span className="mono text-[11.5px] text-text-mute" title="Enable location for this site in your browser to use this.">
        Location blocked - use the ward search instead.
      </span>
    );
  }

  if (my.status === "outside") {
    return (
      <span className="mono text-[11.5px] text-text-mute">
        You're outside Delhi{my.coords ? ` (${my.coords.lat.toFixed(3)}°N, ${my.coords.lon.toFixed(3)}°E${my.coords.accuracy_m ? `, ±${my.coords.accuracy_m} m` : ""})` : ""} - pick any ward to explore.
      </span>
    );
  }

  // found
  const zone = my.zone!;
  const aqi = wardAqiAt(zone, asForecast(horizon));
  const cat = aqiCategory(aqi);
  const risky = aqi > 200;
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <button
        onClick={onGo}
        className="inline-flex items-center gap-1.5 rounded-full bg-accent py-1.5 pl-3 pr-2 text-[12px] font-semibold text-white transition-colors hover:bg-[#064a42]"
        title={my.matched === "nearest" ? `Nearest forecast ward to your location` : `Your ward, from your location`}
      >
        <PinGlyph />
        {zone.name}
        <span
          className="mono rounded-full px-1.5 py-0.5 text-[11px] font-bold"
          style={{ background: cat.color, color: cat.text }}
        >
          {aqi}
        </span>
      </button>
      {my.matched === "nearest" && (
        <span className="mono text-[11px] text-text-mute">nearest forecast ward</span>
      )}
      {risky && (
        <span className="mono text-[11.5px] font-bold" style={{ color: "var(--aqi-very-poor)" }}>
          {cat.label} in your ward - limit outdoor time
        </span>
      )}
    </span>
  );
}
