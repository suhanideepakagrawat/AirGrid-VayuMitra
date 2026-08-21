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

export const fetchHealth = () => get<{ status: string; llm: string; voice: string }>("/health", 5000);
export const fetchWards = (city?: string) =>
  get<WardsResponse>(`/wards${city ? `?city=${encodeURIComponent(city)}` : ""}`);
export const fetchCompare = () => get<{ cities: CitySummary[]; note: string }>("/compare");
export const fetchDeployment = () =>
  get<{ available: boolean; items: DeploymentRow[] }>("/deployment?limit=30");
export const fetchTopTargets = () =>
  get<{ available: boolean; items: TopTarget[] }>("/enforcement/top");

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
