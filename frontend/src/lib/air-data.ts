// Mock grid data for the Delhi-NCR air intelligence platform.
// Cells are on a ~1 km pseudo-grid within a bounding box, each with a dominant
// source, confidence, and forecast AQI. This is intentionally deterministic so
// the UI renders the same "scene" every load — no real geo dependencies.

export type SourceKey = "traffic" | "industry" | "construction" | "burning";

export const SOURCE_LABELS: Record<SourceKey, string> = {
  traffic: "Traffic",
  industry: "Industry",
  construction: "Construction dust",
  burning: "Regional burning",
};

export const SOURCE_COLORS: Record<SourceKey, string> = {
  traffic: "var(--source-traffic)",
  industry: "var(--source-industry)",
  construction: "var(--source-construction)",
  burning: "var(--source-burning)",
};

/**
 * What each source signature is actually measured from.
 *
 * These replace four earlier strings that described a system we had not built —
 * they cited NO2 "signatures", "3 registered brick kilns", DPCC permits and MODIS
 * fire detections at a time when none of those existed in the backend. Every claim
 * below now maps to a real measurement: `advisory/fingerprints.py` for the pollutant
 * signals, `advisory/fire.py` for satellite fire transport.
 *
 * Kept as method descriptions rather than fabricated specifics. The live numbers are
 * fetched per render from /live, so the page states the method and the data supplies
 * the evidence.
 */
export const SOURCE_EVIDENCE: Record<SourceKey, string> = {
  traffic:
    "NO₂ measured at CPCB monitoring stations and compared with the citywide median at the same moment. Vehicles dominate Delhi's NO₂, and it is short-lived — so a raised reading points at a source close by rather than one drifting in.",
  industry:
    "SO₂ measured and compared with the citywide median. SO₂ comes from burning sulphur-bearing fuel — coal, heavy oil, kilns. Since BS-VI fuel, vehicles emit very little of it, which is what makes it a useful industrial marker.",
  construction:
    "The PM10 to PM2.5 ratio. Digging, crushing and road dust throw coarse particles; combustion makes fine ones. A coarse-heavy mix is the standard indicator for mechanically-generated dust.",
  burning:
    "NASA VIIRS satellite fire detections across Punjab and Haryana, counted only when the measured station wind is actually carrying them towards Delhi. Fires burning with the wind blowing elsewhere are not attributed here.",
};

/** What kinds of evidence each source genuinely draws on — the badge row under a
 *  source card. "Registered permits" was removed: no public real-time DPCC permit
 *  feed exists, so nothing backed it. */
export const SOURCE_BASIS: Record<SourceKey, string[]> = {
  traffic: ["Live NO₂", "Wind corridor", "Road network"],
  industry: ["Live SO₂", "Wind corridor", "Industrial land use"],
  construction: ["Live PM10/PM2.5", "Wind corridor", "Construction land use"],
  burning: ["Satellite fire", "Measured wind", "Transport distance"],
};

export type Cell = {
  id: string;
  x: number; // grid col
  y: number; // grid row
  aqi: number;
  dominantSource: SourceKey;
  confidence: number; // 0..1
  attribution: Record<SourceKey, number>; // sums ~1
  ward: string;
  wardCode: string;
};

// Grid: 24 wide x 14 tall
export const GRID_COLS = 24;
export const GRID_ROWS = 14;

const WARDS = [
  { name: "Rohini North", code: "MCD-014" },
  { name: "Karol Bagh", code: "MCD-072" },
  { name: "Anand Vihar", code: "EDMC-031" },
  { name: "Okhla Phase II", code: "SDMC-118" },
  { name: "Dwarka Sector 12", code: "SDMC-204" },
  { name: "Narela Industrial", code: "MCD-006" },
  { name: "Mayapuri", code: "SDMC-088" },
  { name: "Bawana", code: "MCD-002" },
  { name: "Wazirpur", code: "MCD-058" },
  { name: "Mundka", code: "MCD-041" },
];

// Deterministic PRNG so cells are stable
function mulberry32(a: number) {
  return function () {
    let t = (a += 0x6d2b79f5);
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pickSource(rnd: number, x: number, y: number): SourceKey {
  // Bias by geography: burning stronger north, industry west, traffic center, construction east
  const north = y < GRID_ROWS * 0.35;
  const west = x < GRID_COLS * 0.35;
  const east = x > GRID_COLS * 0.65;
  const center = !north && !west && !east;
  const roll = rnd;
  if (north && roll < 0.55) return "burning";
  if (west && roll < 0.55) return "industry";
  if (east && roll < 0.5) return "construction";
  if (center && roll < 0.55) return "traffic";
  if (roll < 0.7) return "traffic";
  if (roll < 0.85) return "construction";
  if (roll < 0.94) return "industry";
  return "burning";
}

function makeAttribution(dominant: SourceKey, rnd: () => number): Record<SourceKey, number> {
  const domShare = 0.45 + rnd() * 0.32;
  const rest: SourceKey[] = (["traffic", "industry", "construction", "burning"] as SourceKey[]).filter(
    (s) => s !== dominant,
  );
  const raw = rest.map(() => rnd());
  const sum = raw.reduce((a, b) => a + b, 0);
  const scale = (1 - domShare) / sum;
  const attr: Record<SourceKey, number> = { traffic: 0, industry: 0, construction: 0, burning: 0 };
  attr[dominant] = domShare;
  rest.forEach((s, i) => (attr[s] = raw[i] * scale));
  return attr;
}

export const CELLS: Cell[] = (() => {
  const rnd = mulberry32(42);
  const out: Cell[] = [];
  for (let y = 0; y < GRID_ROWS; y++) {
    for (let x = 0; x < GRID_COLS; x++) {
      const r = rnd();
      const dominant = pickSource(r, x, y);
      // AQI: higher in NW quadrant (burning + industry stack)
      const base =
        180 +
        (y < GRID_ROWS * 0.4 ? 90 : 0) +
        (x < GRID_COLS * 0.35 ? 60 : 0) +
        rnd() * 90;
      const aqi = Math.round(base);
      const confidence = 0.45 + rnd() * 0.5;
      const ward = WARDS[(x * 3 + y * 5) % WARDS.length];
      out.push({
        id: `c-${x}-${y}`,
        x,
        y,
        aqi,
        dominantSource: dominant,
        confidence,
        attribution: makeAttribution(dominant, rnd),
        ward: ward.name,
        wardCode: ward.code,
      });
    }
  }
  return out;
})();

// Per-horizon AQI for the sample scene. The scene tells one meteorological
// story: tonight's stagnation loads the NW burning/industry corridor at +48h,
// then a western disturbance flushes the basin at +72h — construction dust is
// the slowest to clear. Deterministic, so every load shows the same story.
export type Horizon = "24" | "48" | "72";
export const HORIZONS: Horizon[] = ["24", "48", "72"];

export function cellAqi(c: Cell, h: Horizon): number {
  if (h === "24") return c.aqi;
  const nw = c.y < GRID_ROWS * 0.4 || c.x < GRID_COLS * 0.35;
  if (h === "48") {
    const bump =
      c.dominantSource === "burning" ? 1.2 :
      c.dominantSource === "industry" ? 1.13 :
      nw ? 1.08 : 1.03;
    return Math.round(c.aqi * bump);
  }
  const drop = c.dominantSource === "construction" ? 0.9 : 0.7;
  return Math.round(c.aqi * drop);
}

// CPCB National AQI categories — a public standard, never recolored for taste.
// `text` follows the Legible Band Rule: ink on the light bands (Good, Satisfactory,
// Moderate), white only on Poor / Very Poor / Severe.
export function aqiCategory(aqi: number): { label: string; labelHi: string; color: string; text: string } {
  if (aqi <= 50) return { label: "Good", labelHi: "अच्छा", color: "#009966", text: "#0e1a1c" };
  if (aqi <= 100) return { label: "Satisfactory", labelHi: "संतोषजनक", color: "#84cf33", text: "#0e1a1c" };
  if (aqi <= 200) return { label: "Moderate", labelHi: "मध्यम", color: "#ffde33", text: "#0e1a1c" };
  if (aqi <= 300) return { label: "Poor", labelHi: "ख़राब", color: "#ff9933", text: "#0e1a1c" };
  if (aqi <= 400) return { label: "Very Poor", labelHi: "बहुत ख़राब", color: "#cc0033", text: "#ffffff" };
  return { label: "Severe", labelHi: "गंभीर", color: "#7e0023", text: "#ffffff" };
}

export const FIRE_HOTSPOTS = [
  { id: "f1", x: -0.15, y: -0.2, count: 42 },
  { id: "f2", x: -0.08, y: -0.28, count: 28 },
  { id: "f3", x: -0.22, y: -0.14, count: 61 },
  { id: "f4", x: 0.02, y: -0.32, count: 19 },
];

export const ENFORCEMENT_TARGETS = [
  { id: "e1", name: "Bawana Cluster Kilns", type: "Industry", priority: 94, ward: "Bawana", cellId: "c-3-2", action: "Issue closure notice — SO₂ 3.1× limit" },
  { id: "e2", name: "Narela Phase-III Sites", type: "Construction", priority: 88, ward: "Narela Industrial", cellId: "c-6-3", action: "Suspend dust permits — DPCC" },
  { id: "e3", name: "Wazirpur Rolling Mills", type: "Industry", priority: 81, ward: "Wazirpur", cellId: "c-9-5", action: "Emissions audit within 48h" },
  { id: "e4", name: "Anand Vihar ISBT Corridor", type: "Traffic", priority: 76, ward: "Anand Vihar", cellId: "c-19-7", action: "Deploy odd-even + BS-III curb" },
  { id: "e5", name: "Mundka Waste Depot", type: "Construction", priority: 72, ward: "Mundka", cellId: "c-2-6", action: "Inspect biomass burn reports" },
  { id: "e6", name: "Mayapuri Scrap Yards", type: "Industry", priority: 68, ward: "Mayapuri", cellId: "c-7-9", action: "Metal recovery emissions review" },
  { id: "e7", name: "Okhla Landfill Perimeter", type: "Construction", priority: 65, ward: "Okhla Phase II", cellId: "c-16-11", action: "Cover load monitoring" },
];

export const VULNERABLE_SITES = [
  { id: "v1", type: "School", name: "Rohini Model School", x: 5, y: 3, exposed: 420 },
  { id: "v2", type: "Hospital", name: "Sanjay Gandhi Memorial", x: 4, y: 4, exposed: 180 },
  { id: "v3", type: "Elderly care", name: "Sewa Ashram Karol Bagh", x: 10, y: 6, exposed: 96 },
  { id: "v4", type: "School", name: "DPS Dwarka", x: 3, y: 10, exposed: 610 },
  { id: "v5", type: "Hospital", name: "Max Anand Vihar", x: 19, y: 7, exposed: 220 },
  { id: "v6", type: "School", name: "Mount Carmel Okhla", x: 16, y: 11, exposed: 380 },
];

// Wind vector for the "current run" — pointing SE from NW (typical winter transport)
export const WIND = { dxCell: 1, dyCell: 0.55, speedKmh: 12, dirLabel: "NW → SE" };
