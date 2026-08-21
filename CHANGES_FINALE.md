# Changes for the Finale Round (25 Aug 2026)

Everything changed between the Phase-2 submission (21 Jul 2026) and the offline
finale at T-Hub Hyderabad. Newest entries at the bottom of each section.

**Branch:** `finale/hardening` · **Baseline being protected:** 20/20 tests, all
endpoints 200, both Render services stable.

---

## A. Infrastructure changes (already live on `main`)

### A1 · LLM provider — Groq model migration
**Date:** 21 Aug 2026 · **Commit:** `a19c796`

Groq **decommissioned the llama-3.x line**. `llama-3.3-70b-versatile` and
`llama-3.1-8b-instant` now return 404. Because `advisory/llm.py` swallows every
error by design, `/advisory` kept returning HTTP 200 with `llm_used=false` — the
advisory had silently been running on deterministic templates with nobody noticing.

| | Before | After |
|---|---|---|
| Compose model | `llama-3.3-70b-versatile` ❌ | `openai/gpt-oss-120b` ✅ |
| Translate model | `llama-3.1-8b-instant` ❌ | `openai/gpt-oss-20b` ✅ |

Two traps found and handled:

1. **gpt-oss are reasoning models** — hidden reasoning tokens bill against
   `max_tokens`. Measured at `max_tokens=700`: `reasoning_effort:"medium"` spent
   **698 tokens on reasoning and returned an empty message**; `"low"` spends ~6,
   answers in full, and halves latency (2.0s → 0.9s). Pinned to `"low"`.
2. **gpt-oss emits markdown** far more than llama did. The citizen UI escapes bot
   text, so `**not a diagnosis**` would render literal asterisks *and* be read aloud
   by the TTS voice. Added `strip_markdown()` over every LLM reply.

*Avoid `qwen/qwen3.6-27b` for citizen copy — it leaks `<think>` blocks.*

**Files:** `advisory/config.py`, `advisory/llm.py`, `advisory/advisory_engine.py`,
`.env`, `.env.example`, `render.yaml`, `README.md`

### A2 · Render account migration + new URLs
**Date:** 21 Aug 2026 · **Commits:** `9f537c1`, `0c6b257`

The original workspace was suspended — **"Free Tier Usage Exceeded", 17 Aug**. Root
cause: the keep-alive kept *both* services awake 24/7, roughly **1,440 instance-hours
a month against a ~750-hour free allowance**. It died almost exactly four weeks after
launch.

| | Old (suspended) | New (live) |
|---|---|---|
| Account | bindpratapsingh@gmail.com | **bs563@snu.edu.in** |
| Advisory | `vayumitra-advisory.onrender.com` | **`vayumitra-advisory-u007.onrender.com`** |
| Dashboard | `airgrid-dashboard.onrender.com` | **`airgrid-dashboard-47xp.onrender.com`** |

**Why the URLs changed:** the old services were deleted to free the names, but Render
holds a recently-deleted subdomain in cooldown, so recreating claimed suffixed names.
Confirmed it is cooldown and not policy — a probe service with a never-used name got
a clean URL immediately. The old URLs now 404; every reference in the repo was
updated.

**Two Render lessons worth keeping:**

1. **Never set `healthCheckPath` on a free web service.** With `/health` configured,
   Render's edge intermittently dropped the instance from routing — **20–37% of
   requests returned 404 with `x-render-routing: no-server`**, and those never
   reached uvicorn (the app log showed only successes, its own probes passing every
   5s throughout). Clearing it took the failure rate to **0/50**.
2. **Keep-alive is not free.** It is the reason the first account was suspended.
   `KEEPALIVE_URLS` now self-pings via `RENDER_EXTERNAL_URL` rather than hardcoded
   URLs, so a renamed service warms itself instead of a dead one.

`render.yaml` is now a **two-service blueprint** wired by `fromService`, so a fresh
account is New → Blueprint → Apply.

### A3 · Repo rename
`github.com/suhanideepakagrawat/ET_AI_Hackathon` →
**`github.com/suhanideepakagrawat/AirGrid-VayuMitra`** (old URL 301-redirects).

---

## B. Finale hardening (branch `finale/hardening`)

### B1 · New data sources wired in
**Date:** 22 Aug 2026

Added two free API keys to the git-ignored `.env` (documented by name in
`.env.example`, never committed):

| Key | Purpose | Verified |
|---|---|---|
| `OPENAQ_API_KEY` | Live CPCB/DPCC station readings — powers `/live` and the pollutant fingerprints | ✅ 90 Delhi locations, **54 reporting within 6 h** |
| `FIRMS_MAP_KEY` | NASA VIIRS/MODIS fire detections — the regional-burning source | ✅ returned 8 real detections over Punjab–Haryana |

**Live sensor coverage confirmed** across the 54 fresh stations — this is what makes
per-ward fingerprints possible rather than city-level:

| Pollutant | Sensors |
|---|---|
| PM2.5 | 91 |
| NO₂ | 87 |
| PM10 | 87 |
| O₃ | 87 |
| CO | 86 |
| SO₂ | 73 |

**Note:** 31 of the 90 OpenAQ Delhi locations are stale by >30 days (some last
reported in 2018). All live code must filter on `datetimeLast` recency.

**Files:** `.env`, `.env.example`

### B2 · NEW — live ward AQI from real government stations (`GET /live`)
**Date:** 22 Aug 2026 · **Objective:** answer "is this live?" with real instrument data

The product had **no live data at all** — the only outbound call in the deployed app
was the Render keep-alive ping. It now serves current AQI for all 209 wards from the
same CPCB/DPCC/IMD stations the models train on, so our numbers agree with the CPCB
app by construction rather than by luck.

**New files:** `advisory/openaq.py` (station client), `advisory/live.py` (CPCB AQI +
ward interpolation)
**Changed:** `backend/advisory_api.py` (`/live`, background refresher, `/meta`,
`/wards`), `advisory/data.py` (`forecast_provenance()`)

**Verified live:** 209 wards from **59–63 reporting stations**, data age **~1 hour**,
Delhi mean AQI **100**, range **55–153**.

**Four real problems found and fixed while building this — each would have shown up
in the demo:**

1. **Dead stations.** 31 of ~104 OpenAQ Delhi locations last reported over 30 days
   ago — R K Puram's legacy sensors stopped in **February 2018**. Filtered on
   `datetimeLast` recency.
2. **Live NO₂/SO₂ were being silently dropped.** Each station carries two sensor
   generations: legacy µg/m³ sensors (dead) and current sensors reporting in **ppb**.
   Accepting only metric ids discarded exactly the live traffic and industry signals.
   Now both are accepted, with ppb→µg/m³ conversion (NO₂ ×1.88, SO₂ ×2.62).
   Coverage went from 0 to **58 NO₂ and 48 SO₂** stations.
3. **A broken sensor was corrupting a whole ward.** One station reported NO₂ at
   **238 µg/m³** against a citywide median of 48, *and* PM2.5 5.0 with PM10 69.9 — a
   **14:1 ratio that is physically impossible**. It pushed a ward to AQI 259. Added
   three-stage quality filtering (plausibility bounds, PM10/PM2.5 consistency, and
   median-absolute-deviation outlier rejection), which catches ~13 bad readings per
   cycle. Rejections are **published in the response** rather than hidden.
4. **A 20-second cold response.** Fetching 63 stations takes ~15 s, which on a free
   instance risks a proxy timeout for the first visitor. `/live` is now **strictly
   non-blocking** — a background thread refreshes every 10 min and requests are served
   from cache in **~6 ms**, returning a labelled `warming` state if the cache is
   not yet filled.

**Sanity checks performed:**
- Wards within 1 km of a station match that station to **0.3 AQI** on average.
- CPCB formula verified by hand: Anand Vihar PM2.5 77 µg/m³ → sub-index 157.1,
  correctly taken as the max across pollutants.
- Result plausibility: mean AQI 100 in monsoon season is right for Delhi.

**Design decision — IDW rather than the trained spatial estimator.** `xgboost` is
deliberately not a dependency of the advisory service, and adding a large wheel to a
free-tier build buys ~8% (the model's own LOSO: 85.78 vs IDW 93.28). Every ward
therefore ships `nearest_station_km` and `n_stations` so trust is visible. The trained
models remain the differentiator where they matter — the 24/48/72 h forecast.

### B3 · Forecast provenance — dating every number
**Date:** 22 Aug 2026

Nothing in the API or UI exposed how old the forecast was. `forecast_provenance()`
now reads the pipeline output's own `source_timestamp` (not file mtime, so it
survives a checkout) and serves it on `/meta` and `/wards`.

**Correction to an earlier finding:** the served ward forecast is dated
**2026-07-12**, not 4 July — `future_aqi_forecast_ward.csv` is a newer run than the
raw `future_aqi_forecast.csv`. Age at time of writing: **40.2 days**, targeting
13–15 July.

### B4 · Frontend — prominent freshness strip + per-ward live AQI
**Date:** 22 Aug 2026

**New:** `frontend/src/components/DataFreshness.tsx`
**Changed:** `frontend/src/lib/api.ts` (`LiveNow` types, `fetchLive`, `liveQuery`,
`timeAgo`), `frontend/src/routes/dashboard.tsx` (freshness strip + `LiveNowBlock`)

Nothing on any screen previously said how old anything was, so a 40-day-old model
run read exactly like a current measurement. The dashboard now opens with a
provenance strip stating both layers side by side, and they are never conflated:

- **LIVE NOW** — Delhi average AQI, worst ward, station count, "measured 1h ago",
  with a pulsing indicator that turns amber if readings exceed 3 hours old.
- **FORECAST +24/48/72h** — "model run 12 Jul 2026 · 40d ago · trained XGBoost models".

Ward detail gained a **MEASURED NOW** block: live AQI, dominant pollutant, PM2.5 and
PM10 in µg/m³, the contributing station and its distance, and how many stations were
blended. Distance is shown deliberately — a ward 8 km from the nearest monitor
deserves less confidence than one sitting on top of it.

**Verified with Playwright (headless Chromium) against the live API:**

| Check | Result |
|---|---|
| Freshness strip renders | ✅ all five elements |
| **Horizon switching changes data** | ✅ +24h → **99**, +48h → **95**, +72h → **105** |
| Per-ward live AQI | ✅ Narela: 137 Moderate, PM2.5 60.4 / PM10 156.4 µg/m³, station 5.47 km, 3 blended |
| Mobile 390px | ✅ scrollWidth 390, no overflow |
| JS console errors | ✅ none |

**Sanity check of the ward number, by hand:** PM10 156.4 µg/m³ falls in the CPCB
100–250 band → 101 + 56.4 × 99/150 = **138.2**, against the 137 shown after IDW
blending. PM2.5 60.4 → 102.3, correctly *not* selected as the driver. The
"driven by PM10" label is right.

**Also confirmed:** the synthetic fallback scene (Delhi average 287, "Wazirpur 417")
no longer reaches the browser — it is replaced on hydration by real values (99,
worst ward Vishwash Nagar 171). Server-rendered HTML still contains the sample scene
for the pre-hydration frame; replacing that is tracked separately.

**Tooling note:** Playwright + Chromium installed for UI verification. Launch with
`channel="chromium"` — the default headless-shell build is not present.

*(Further entries appended as each objective lands.)*
