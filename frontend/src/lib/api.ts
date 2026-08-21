// Live-data bridge to the deployed PS5 backend (FastAPI on Render).
// Every fetcher degrades gracefully: if the API is unreachable the UI keeps
// rendering the bundled sample scene and says so honestly ("sample" badge),
// so the demo never breaks (RULE 1 of the implementation plan).

// Accepts either a full URL ("https://host") or a bare hostname ("host") so a
// Render blueprint can wire this straight from the sibling service (fromService
// yields a scheme-less host). Falls back to the public advisory service.
const RAW_API_URL: string = ((import.meta as any).env?.VITE_API_URL ?? "").trim();

export const API_BASE: string = RAW_API_URL
  ? (/^https?:\/\//.test(RAW_API_URL) ? RAW_API_URL : `https://${RAW_API_URL}`).replace(/\/$/, "")
  : "https://vayumitra-advisory-u007.onrender.com";

export const CITIZEN_APP_URL = API_BASE; // the VayuMitra chat is served at "/"

async function get<T>(path: string, timeoutMs = 8000): Promise<T> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(`${API_BASE}${path}`, { signal: ctrl.signal });
    if (!r.ok) throw new Error(`${path} -> ${r.status}`);
    return (await r.json()) as T;
  } finally {
    clearTimeout(t);
  }
}

export type HorizonKey = "24" | "48" | "72";

export type LiveWard = {
  zone_id: string;
  name: string;
  lat: number;
  lon: number;
  aqi: number;
  band: string;
  band_label: string;
  color: string;
  forecast?: Partial<Record<HorizonKey, number>>;
  sources?: { traffic: number; industry: number; construction: number };
  dominant_source: string | null;
  dominant_source_pct: number;
  confidence: number | null;
};

/** AQI of a ward at a horizon; falls back to the current AQI when the API
 *  predates the forecast field (older deploy) so the UI never breaks. */
export function wardAqiAt(w: LiveWard, h: HorizonKey): number {
  return Math.round(w.forecast?.[h] ?? w.aqi);
}

export type WardsResponse = {
  city: string;
  data_kind: "real" | "mock";
  count: number;
  /** When this forecast run was produced. Optional so an older API still parses. */
  forecast_run?: ForecastRun;
  wards: LiveWard[];
};

export type CitySummary = {
  city: string;
  name: string;
  zones: number;
  avg_aqi: number;
  max_aqi: number;
  worst_zone: { name: string; aqi: number };
  source_mix: { traffic: number; industry: number; construction: number };
  dominant_source: string | null;
  intervention: { avg_aqi_before: number; avg_aqi_after: number; reduction_pct: number; note: string };
  data_kind: "real" | "mock";
};

export type DeploymentRow = {
  rank: number;
  ward_no: string;
  ward_name: string;
  hotspots: number;
  max_aqi: number;
  avg_aqi: number;
  deployment_score: number;
  dominant_source: string | null;
  recommended_team: string | null;
};

export type TopTarget = {
  cell_id: number;
  lat: number;
  lon: number;
  max_priority: number;
  max_aqi: number;
  dominant_source: string;
  action: string;
  evidence: string;
  rank: number;
};

/** Provenance for the forecast layer — lets any screen date its numbers instead of
 *  implying they are current. Served on /wards and /meta. */
export type ForecastRun = {
  available: boolean;
  issued_at?: string;
  age_hours?: number;
  age_days?: number;
  targets?: string[];
  source?: string;
};

/** One ward in the LIVE layer: measured now by real CPCB/DPCC/IMD instruments,
 *  as opposed to LiveWard's model forecast for +24/48/72 h. */
export type LiveNowWard = {
  zone_id: string;
  name: string;
  lat: number;
  lon: number;
  aqi: number;
  band: string;
  band_label: string;
  color: string;
  dominant_pollutant: string;
  pollutants: Record<string, number>;
  nearest_station: string;
  nearest_station_km: number;
  n_stations: number;
  observed_at: string | null;
};

export type LiveNow = {
  available: boolean;
  state?: "warming";
  reason?: string;
  source?: string;
  method?: string;
  fetched_at?: string;
  observed_at?: string | null;
  data_age_hours?: number | null;
  stations?: number;
  stations_fetched?: number;
  quality_filtered?: string[];
  wards: LiveNowWard[];
};

export const fetchHealth = () => get<{ status: string; llm: string; voice: string }>("/health", 5000);
export const fetchWards = (city?: string) =>
  get<WardsResponse>(`/wards${city ? `?city=${encodeURIComponent(city)}` : ""}`);
export const fetchCompare = () => get<{ cities: CitySummary[]; note: string }>("/compare");
export const fetchDeployment = () =>
  get<{ available: boolean; items: DeploymentRow[] }>("/deployment?limit=30");
export const fetchTopTargets = () =>
  get<{ available: boolean; items: TopTarget[] }>("/enforcement/top");
export const fetchLive = () => get<LiveNow>("/live", 12000);

// Query configs shared by routes (react-query is already in the root context).
export const wardsQuery = (city?: string) => ({
  queryKey: ["wards", city ?? "default"],
  queryFn: () => fetchWards(city),
  staleTime: 5 * 60_000,
  retry: 1,
});

export const healthQuery = {
  queryKey: ["api-health"],
  queryFn: fetchHealth,
  staleTime: 60_000,
  retry: 0,
};

export const compareQuery = {
  queryKey: ["compare"],
  queryFn: fetchCompare,
  staleTime: 5 * 60_000,
  retry: 1,
};

export const deploymentQuery = {
  queryKey: ["deployment"],
  queryFn: fetchDeployment,
  staleTime: 5 * 60_000,
  retry: 1,
};

export const topTargetsQuery = {
  queryKey: ["topTargets"],
  queryFn: fetchTopTargets,
  staleTime: 5 * 60_000,
  retry: 1,
};

/** The live layer refreshes server-side every ~10 min, so poll a little faster than
 *  that to keep the "updated Xm ago" badge honest without hammering the API. */
export const liveQuery = {
  queryKey: ["live-now"],
  queryFn: fetchLive,
  staleTime: 2 * 60_000,
  refetchInterval: 5 * 60_000,
  retry: 1,
};

/** "3m ago" / "2h ago" / "40d ago" — one shared formatter so every freshness badge
 *  in the product reads identically. */
export function timeAgo(iso?: string | null): string {
  if (!iso) return "unknown";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "unknown";
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 48) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}
