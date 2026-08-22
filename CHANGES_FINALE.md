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

### B5 · NEW — pollutant fingerprints (makes the traffic/industry/dust claims real)
**Date:** 22 Aug 2026 · **New file:** `advisory/fingerprints.py`

The site claimed NO₂, SO₂ and PM10/PM2.5 evidence it never computed — the notebook
listed these under *"Still a future enhancement"*. They are now measured, from the
same OpenAQ station feed, and attached to every ward on `/live`.

| Signature | Measured as | Why it works |
|---|---|---|
| **Traffic** | NO₂ vs citywide median | Vehicles dominate Delhi NO₂; NO₂ is short-lived so a high reading means a *nearby* source |
| **Industry** | SO₂ vs citywide median | SO₂ comes from sulphur-bearing fuel (coal, kilns); since BS-VI, vehicles emit almost none |
| **Construction** | PM10 ÷ PM2.5 | Mechanical processes throw coarse particles, combustion makes fine ones |

**A method correction the real data forced — worth knowing for the pitch.**
The intuitive approach is a twin-peak rush-hour curve. Seven days of hourly NO₂ for a
central Delhi station shows the opposite: **median 79 µg/m³ at midnight, 27 by late
morning — the peak is at night**, when nobody commutes. That is the nocturnal boundary
layer collapsing and concentrating everything, not traffic. A rush-versus-quiet ratio
would largely have measured the weather.

So normalisation is **spatial, at a single instant**: each station against the
citywide median *at the same moment*. All stations share the same mixing height then,
so meteorology cancels and local emission strength remains. Two consequences: the
method is sounder, and it needs **no history at all** — fingerprinting 59 stations
takes **0.00 s** instead of the 80 s a per-station baseline fetch required.

**One integration trap, caught before it shipped.** Run on the raw feed, the engine
reported the known-broken station as *"NO₂ 5.11× the Delhi median"* and a
*"PM10/PM2.5 ratio of 13.98 — coarse dust dominant"*. A faulty instrument becomes a
confident false claim about a real neighbourhood, and its values also drag the
citywide medians every other station is judged against. Fingerprints now run strictly
on the quality-filtered set, with the requirement documented at the call site.

**Verified across 209 wards:** construction 92 · traffic 46 · industry 30 · **no call
41**. Those 41 are deliberate — nothing reached "moderate", so we say nothing rather
than guess. Zero wards retain an impossible PM ratio.

Real examples: *Vishwash Nagar → industry (SO₂ 60 µg/m³, 2.95× median)*;
*Vikaspuri East → traffic (NO₂ 120 µg/m³, 2.49× median)*; *Dharampura → construction
(PM10/PM2.5 3.39)*. Every evidence string is a measured number a juror can check.

**Still honestly labelled as indicators, not apportionment** — real apportionment
needs filter sampling and receptor modelling (PMF/CMB). The module documents each
signal's limits: NO₂ also comes from gensets, SO₂ is not exclusive to industry, and a
coarse-heavy mix cannot separate a construction site from an unpaved road.

### B6 · NEW — the fourth source: regional biomass burning (NASA FIRMS)
**Date:** 22 Aug 2026 · **New file:** `advisory/fire.py`

The frontend advertised four sources; the backend had three. `burning` existed only
as a TypeScript type. It is now real, and it closes a genuine scientific hole: in
Oct–Nov, Punjab/Haryana stubble burning can dominate Delhi's PM2.5, and the
geospatial engine could never see it — it reasons about local land use, so a plume
from 200 km upwind was silently misattributed to whatever happened to sit nearby.

**Three measured facts, no assumptions:** where fires are burning now (VIIRS 375 m
detections via FIRMS), how intense they are (fire radiative power in MW), and whether
the **measured** wind is carrying them here. Wind comes from ~46 real station
anemometers, not a model — averaged as unit vectors, because naively averaging 350°
and 10° gives 180°, the exact opposite of the truth.

A fire only counts if it lies within 400 km and its bearing from Delhi is within 45°
of the wind direction. Fires burning with the wind blowing the other way are somebody
else's problem that day, and we say so rather than blaming them for a local dust event.

**Verified live:** **10 real detections** in the stubble belt, measured wind 0.3 m/s
from 178° (southerly). Correct verdict: **`not_transported`** — *"10 fires detected in
Punjab–Haryana, but the wind (178°) is not carrying them towards Delhi."*

**This is the honest demo, and worth showing deliberately.** Late August is not
burning season, so the engine reports almost nothing — that is it working, not
failing. Showing the reasoning ("real fires, wrong wind, therefore not us") is more
convincing than a suspiciously busy August fire map.

Attached to `/live` as a **city-level** signal, not per-ward: a plume covering all of
Delhi cannot honestly be pinned to individual wards.

**Limits documented in the module:** satellites see fires, not smoke — whether a plume
reaches ground level depends on injection height and mixing, which we do not model;
cloud cover hides fires; and we report transport *plausibility*, never a percentage
contribution.

### B7 · Fabrication removed from every page — the frontend now says what we measure
**Date:** 22 Aug 2026

**Changed:** `lib/air-data.ts`, `lib/api.ts`, `routes/index.tsx`,
`routes/dashboard.tsx`, `routes/enforcement.tsx`, `routes/health.tsx`,
`backend/advisory_api.py`

| Was rendering | Now |
|---|---|
| *"Peak-hour NO₂ signature… 400 m upwind"* | The real method, plus **live counts**: "46 of 209 wards are showing this signature right now" |
| *"3 registered brick kilns 6.2 km upwind; SO₂ elevated"* | SO₂ vs the citywide median, measured |
| *"PM10/PM2.5 ratio ≥ 3.1 with active DPCC permits"* | The measured PM10/PM2.5 ratio — **permits clause gone** |
| *"MODIS/VIIRS fire detections…"* | Real FIRMS detections with the live transport verdict |
| **"Registered permits"** evidence badge | Replaced by `SOURCE_BASIS` — what each source genuinely draws on. **No public real-time DPCC permit feed exists**, so nothing could back it |
| *"Bawana Cluster Kilns · Issue closure notice, SO₂ 3.1× limit"*, *"Wazirpur Rolling Mills"*, *"Narela Phase-III Sites"* | The pipeline's real ranked targets, resolved to MCD wards: **rank 1 NANAK PURA, rank 2 GHAROLI, rank 3 VISHWASH NAGAR** |
| *"DPS Dwarka — 610 people exposed"*, *"Sanjay Gandhi Memorial — 180"* | 209 real wards ranked by measured risk, with a persona selector |

**Why the enforcement swap mattered more than it looked.** The invented targets were
not a fallback nobody saw — they were the *visible content* of `/enforcement` on every
server-rendered first paint, because the live query had not resolved yet. `/enforcement/top`
now resolves every target to its real ward (**20 of 20 map successfully**).

**The health page rail** was the least defensible thing on the site: real named
schools and hospitals carrying invented exposure counts, ranked on synthetic AQI. It
is now real wards, live readings, and the same six personas VayuMitra uses
(`advisory/personas.py`).

**A bug caught by testing rather than reasoning.** The persona selector initially
appeared to do nothing. It was not the click handler — guidance collapsed to one
shared sentence below AQI 100, and Delhi was reading **94** that day. A control that
visibly does nothing reads as broken, so every persona now gets distinct wording in
every band. Verified: *child* → "Fine for outdoor play. Prefer parks over roadsides";
*outdoor worker* → "Safe for a full shift. Keep water handy"; *elderly* → "Good
conditions for a walk. Early morning is cleanest."

**Verified — fabrication scan across all five routes, server-rendered with scripts
stripped, and again client-side under Playwright:**

| Route | Result |
|---|---|
| `/` `/dashboard` `/attribution` `/enforcement` `/health` | **CLEAN — 0 fabricated strings** |
| Real enforcement wards rendering | ✅ NANAK PURA, GHAROLI, VISHWASH NAGAR |
| Persona switching | ✅ changes guidance |
| 390 px viewport | ✅ no overflow |
| JS console errors | ✅ none |
| Backend tests | ✅ 20/20 |

### B8 · The forecast is no longer stale — regenerated from live data
**Date:** 22 Aug 2026 · **New file:** `scripts/refresh_forecast.py`

The served forecast was a single frozen run from **12 July**, 40 days old, presented
as current. It is now regenerated from today's station readings.

| | Before | After |
|---|---|---|
| Issued | 2026-07-12 | **2026-08-22** (~5 h old) |
| Targets | 13–15 July (all past) | **23–25 Aug** — the finale day is inside the window |
| Mean AQI | 98.3 | 78.9 |
| Coverage | 1,600 cells / 209 wards | **identical: 1,600 / 209** |

**This is inference, not retraining.** The same `spatial_estimator` and
`forecaster_{24,48,72}h` models run unchanged, so the published validation numbers
still describe the models actually in use.

**Three audit findings made this cheap.** Inference needs only enough history to fill
the 7-day rolling feature, not the years the training pipeline pulls — so ten days
suffices. The five land-use features and two satellite features are unused by every
model (0 splits), so they are passed as NaN, reproducing training conditions exactly.
And weather was already city-level.

**Three problems found and fixed during the run:**

1. **`XGBRegressor` refuses to construct without scikit-learn**, even for pure
   inference. Switched to the native `Booster` API — identical predictions, no extra
   dependency.
2. **Over-strict source-hour selection cost 16 wards.** Requiring non-null lags
   dropped 62 cells and took the map from 209 wards to 193 — a visible regression
   against the "209 wards" stated throughout the product. XGBoost learned default
   directions for missing values during training, so filtering those rows is stricter
   than the model ever expected. Now matches `predict_future_aqi.py`, selecting on
   cell coverage alone: **1,600 cells, 209 wards, zero lost.**
3. **The refresh initially changed nothing users could see.** `/wards` reads
   `source_attribution.csv` (`config/city.yaml` → `data_file`), not the forecast file
   — which only supplies ward names for the join. The metadata would have read
   "issued 2 hours ago" over July's numbers, which is worse than saying nothing. The
   promote step now carries the new AQI into that file too (**4,800/4,800 rows**),
   recomputing `aqi_severity` and the low-AQI branch of `attribution_status`.

**Safety.** Writes to a `.NEW.csv` and never touches live data until seven sanity
gates pass — coverage collapse, horizon set, implausible mean, NaNs, near-constant
output, missing wards, and a staleness check on the source hour. `--promote` keeps
timestamped backups, and the July run also remains recoverable from git
(`git show HEAD:data/source_attribution.csv`).

**Verified end-to-end:**

| Check | Result |
|---|---|
| Forecast provenance | issued 22 Aug, **5 h old**, targets 23–25 Aug |
| Ward coverage | 209, none lost |
| Horizon switching in the UI | ✅ **92 / 82 / 86** — genuinely recomputes |
| Live vs +24 h coherence | 209 wards matched, live mean 78.6 vs 91.7, mean abs diff 22.9 |
| Ward detail | Narela: live 67, PM2.5 27.4 / PM10 64.1, station 5.47 km |
| Freshness strip | *"model run 22 Aug 2026 · 5h ago"* |
| Tests / JS errors / 390 px | 20/20 · none · no overflow |

**Known limitation, stated rather than hidden:** the source-split percentages
(`dominant_source`, the shares, `confidence`) still come from July's attribution run,
because regenerating them means re-running the Colab notebook against OSM extracts.
Local geography dominates that score at low wind, so the ranking is broadly stable,
but the upwind-corridor component is not refreshed. **The live pollutant fingerprints
in `advisory/fingerprints.py` are the current source evidence.**

**Re-run before the finale** so the demo shows hours, not days:

    python scripts/refresh_forecast.py --promote

### B9 · Copy corrected — we were understating ourselves
The landing hero and method panel said Delhi has *"~40 monitors"*. We use **63 live
stations** today (64 in the training set). Corrected to "60-plus government monitors"
and "Around 60 CPCB, DPCC and IMD monitors report live".

*(Further entries appended as each objective lands.)*
