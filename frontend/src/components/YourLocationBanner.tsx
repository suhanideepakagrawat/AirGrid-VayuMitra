// "Your location" - the dashboard's personal strip. Once geolocation resolves,
// the banner takes the ward's CPCB band color (Legible Band Rule text) and
// speaks like the advisory: your ward, its AQI, and what to do about it.
// Severity is the color scheme; the words carry it for color-blind users.

import { useState } from "react";
import { aqiCategory, asForecast, isNow, type HorizonSel } from "@/lib/air-data";
import { wardAqiAt } from "@/lib/api";
import type { MyWard } from "@/lib/locate";

// Band-level guidance for the general public (CPCB advisories, condensed).
// VayuMitra gives the persona-specific version - this is the headline.
function guidanceFor(aqi: number): { advice: string; acts: string[] } {
  if (aqi <= 100) return { advice: "Air is fine for outdoor activity today.", acts: [] };
  if (aqi <= 200)
    return {
      advice: "Sensitive groups (children, elderly, asthma) should limit prolonged outdoor exertion.",
      acts: ["Sensitive: shorten outdoor time"],
    };
  if (aqi <= 300)
    return {
      advice: "Limit outdoor time and wear an N95 outdoors.",
      acts: ["Limit outdoors", "N95 mask"],
    };
  if (aqi <= 400)
    return {
      advice: "Avoid outdoor exertion. N95 outdoors, keep windows closed.",
      acts: ["Avoid outdoors", "N95 mask", "Windows closed"],
    };
  return {
    advice: "Stay indoors, seal windows, use a purifier if available.",
    acts: ["Stay indoors", "Seal windows", "Purifier on"],
  };
}

function PinGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
      <path
        d="M7 1.2c2.3 0 4.1 1.8 4.1 4.1 0 3-4.1 7.5-4.1 7.5S2.9 8.3 2.9 5.3C2.9 3 4.7 1.2 7 1.2z"
        fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"
      />
      <circle cx="7" cy="5.3" r="1.6" fill="currentColor" />
    </svg>
  );
}

export function YourLocationBanner({ my, horizon }: { my: MyWard; horizon: HorizonSel }) {
  const [dismissed, setDismissed] = useState(false);
  if (dismissed || my.status === "idle" || my.status === "unsupported") return null;

  // Quiet one-liners for every state except "found".
  if (my.status !== "found") {
    const text =
      my.status === "locating"
        ? "Finding your ward from your location…"
        : my.status === "denied"
          ? "Location declined - use the ward search at left to see your area. Nothing is stored either way."
          : my.status === "outside"
            ? `You're outside Delhi${
                my.coords
                  ? ` (${my.coords.lat.toFixed(3)}°N, ${my.coords.lon.toFixed(3)}°E${
                      my.coords.accuracy_m ? `, ±${my.coords.accuracy_m} m` : ""
                    })`
                  : ""
              } - pick any ward to explore the forecast.`
            : "Couldn't get a location fix - use the ward search at left.";
    return (
      <div className="flex items-center justify-between gap-3 border-b border-border bg-surface-1 px-5 py-2">
        <span className="mono flex items-center gap-2 text-[11.5px] text-text-dim">
          <PinGlyph />
          {text}
        </span>
        {my.status !== "locating" && (
          <button
            onClick={() => setDismissed(true)}
            aria-label="Dismiss location message"
            className="mono text-[11.5px] text-text-mute hover:text-foreground"
          >
            ✕
          </button>
        )}
      </div>
    );
  }

  const zone = my.zone!;
  const aqi = wardAqiAt(zone, asForecast(horizon));
  const cat = aqiCategory(aqi);
  const g = guidanceFor(aqi);

  return (
    <div
      role="status"
      aria-label={`Your location: ${zone.name}, AQI ${aqi}, ${cat.label}`}
      className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-border px-5 py-3"
      style={{ background: cat.color, color: cat.text }}
    >
      <span className="flex items-center gap-2 font-bold">
        <PinGlyph />
        <span className="text-[14px]">Your location · {zone.name}</span>
      </span>

      <span className="mono flex items-baseline gap-1.5">
        <span className="text-xl font-bold leading-none">{aqi}</span>
        <span className="text-[11px] opacity-85">AQI · {isNow(horizon) ? "next 24 h" : `+${horizon} h`}</span>
      </span>

      <span
        className="mono rounded-full px-2.5 py-0.5 text-[12px] font-bold"
        style={{ background: cat.text, color: cat.color }}
      >
        {cat.label}
      </span>

      <span className="text-[12.5px] font-semibold">{g.advice}</span>

      {g.acts.map((a) => (
        <span
          key={a}
          className="mono rounded-full border px-2.5 py-0.5 text-[11px] font-bold"
          style={{ borderColor: cat.text, opacity: 0.9 }}
        >
          {a}
        </span>
      ))}

      {my.matched === "nearest" && (
        <span className="mono text-[11px] opacity-80">nearest forecast ward to you</span>
      )}

      <span className="ml-auto flex items-center gap-2">
        <button
          onClick={() => window.dispatchEvent(new CustomEvent("open-vayumitra"))}
          className="rounded-full px-3.5 py-1.5 text-[12px] font-bold transition-opacity hover:opacity-85"
          style={{ background: cat.text, color: cat.color }}
        >
          Personal advice - Ask VayuMitra
        </button>
        <button
          onClick={() => setDismissed(true)}
          aria-label="Dismiss your-location banner"
          className="mono px-1 text-[12.5px] font-bold opacity-75 hover:opacity-100"
        >
          ✕
        </button>
      </span>
    </div>
  );
}
