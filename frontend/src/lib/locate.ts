// "Find my ward" - browser geolocation resolved to a real MCD ward by the
// backend (point-in-polygon over the actual boundaries, nearest-forecast-ward
// fallback). The coordinate is used once per lookup and never stored.

import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE, type LiveWard } from "@/lib/api";

export type LocateResponse = {
  in_delhi: boolean;
  matched: "boundary" | "nearest" | "none";
  ward_id: string | null;
  ward_name: string | null;
  zone: LiveWard | null;
  nearest_km: number | null;
};

export type GeoStatus =
  | "idle"        // not asked yet - show the button
  | "locating"
  | "found"       // zone is set
  | "outside"     // real GPS fix, but not in Delhi
  | "denied"      // permission refused
  | "error"       // timeout / no fix / API down
  | "unsupported";

export type MyWard = {
  status: GeoStatus;
  zone: LiveWard | null;
  wardName: string | null;
  matched: LocateResponse["matched"] | null;
  /** The device's own fix. Reported even when it falls outside Delhi, so a user
   *  can see the location feature actually worked rather than just being told
   *  "outside" with nothing to check it against. */
  coords: { lat: number; lon: number; accuracy_m: number | null } | null;
  request: () => void;
};

export function useMyWard(
  onFound?: (zone: LiveWard) => void,
  opts?: { auto?: "always" | "granted" },
): MyWard {
  const [status, setStatus] = useState<GeoStatus>("idle");
  const [zone, setZone] = useState<LiveWard | null>(null);
  const [wardName, setWardName] = useState<string | null>(null);
  const [matched, setMatched] = useState<LocateResponse["matched"] | null>(null);
  const [coords, setCoords] = useState<MyWard["coords"]>(null);
  const onFoundRef = useRef(onFound);
  onFoundRef.current = onFound;

  const request = useCallback(() => {
    if (typeof navigator === "undefined" || !navigator.geolocation) {
      setStatus("unsupported");
      return;
    }
    setStatus("locating");
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        try {
          const { latitude, longitude, accuracy } = pos.coords;
          setCoords({
            lat: latitude,
            lon: longitude,
            accuracy_m: typeof accuracy === "number" ? Math.round(accuracy) : null,
          });
          const r = await fetch(`${API_BASE}/locate?lat=${latitude}&lon=${longitude}`);
          const data = (await r.json()) as LocateResponse;
          if (data.zone) {
            setZone(data.zone);
            setWardName(data.ward_name ?? data.zone.name);
            setMatched(data.matched);
            setStatus("found");
            onFoundRef.current?.(data.zone);
          } else {
            setStatus("outside");
          }
        } catch {
          setStatus("error");
        }
      },
      (err) => setStatus(err.code === err.PERMISSION_DENIED ? "denied" : "error"),
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 120000 },
    );
  }, []);

  // auto:"always" (the dashboard) asks for location on load - the permission
  // prompt itself is the ask. auto:"granted" (default) only resolves silently
  // when a previous visit already granted it - no prompt fires.
  const auto = opts?.auto ?? "granted";
  useEffect(() => {
    if (typeof navigator === "undefined") return;
    if (auto === "always") {
      request();
      return;
    }
    if (!navigator.permissions?.query) return;
    let cancelled = false;
    navigator.permissions
      .query({ name: "geolocation" as PermissionName })
      .then((p) => {
        if (!cancelled && p.state === "granted") request();
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [request, auto]);

  return { status, zone, wardName, matched, coords, request };
}
