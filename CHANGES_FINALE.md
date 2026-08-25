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

### B10 · DEPLOYED — everything above is now live
**Date:** 22 Aug 2026 · `finale/hardening` merged fast-forward into `main` (8 commits)

Pushed to GitHub and redeployed both Render services with a cache clear.

**New env vars set on `vayumitra-advisory`:**

| Key | Value |
|---|---|
| `OPENAQ_API_KEY` | (secret) — without it `/live` reports `available:false` |
| `FIRMS_MAP_KEY` | (secret) — without it regional burning is unavailable |
| `FORECAST_REFRESH_HOURS` | `6` |
| `FORECAST_REFRESH_DELAY_MIN` | `15` |
| `LIVE_REFRESH_SECONDS` | `600` |

**Verified in production, not locally:**

| Check | Result |
|---|---|
| Advisory endpoints | **15/15 → 200** |
| Dashboard routes | **5/5 → 200** |
| Live layer | **60 stations · 209 wards · 0.69 h old · mean AQI 91.3** |
| Quality filter | 14 bad readings rejected this cycle |
| Fingerprints | industry 66 · construction 58 · traffic 41 · **no call 44** |
| Regional burning | *"1 fire detected in Punjab–Haryana, but the wind (236°) is not carrying them towards Delhi"* |
| Forecast provenance | issued 22 Aug, **5.2 h old**, targets **23–25 Aug** |
| Horizon switching | **96 / 100 / 106** — genuinely recomputes |
| Ward detail | Narela: measured 76, PM2.5 47.1 / PM10 74.1, station 5.47 km |
| LLM | `llm_used=true`, Hindi, zero markdown |
| Voice | Hindi via **ElevenLabs** neural |
| Fabrication scan | **0 fabricated strings across all 5 routes** |
| Stability | **40/40 on both services** |
| `/live` latency | 0.2–0.5 s (cache-served) |
| 390 px / JS errors | no overflow · none |

**Live URLs**
- Citizen app + API — https://vayumitra-advisory-u007.onrender.com
- Operator dashboard — https://airgrid-dashboard-47xp.onrender.com

**Two operational notes for the 25th:**

1. **The 6-hour schedule counts uptime, not wall-clock.** It restarts on every deploy
   or container recycle, so it is really "15 min after boot, then every 6 h".
2. **Refreshed CSVs are ephemeral.** Render rebuilds the container from git on each
   deploy, so a redeploy reverts to the committed run and re-refreshes 15 min later.
   **Run `python scripts/refresh_forecast.py --promote` and commit the result before
   the finale**, so the deployed container starts from fresh data rather than waiting
   to earn it.

### B11 · Merged Krishna's retrained models and live pipeline
**Date:** 23 Aug 2026 · merged `origin/main` (15 commits, no conflicts)

**The spatial estimator is meaningfully better after retraining:**

| | Before | After |
|---|---|---|
| LOSO RMSE | 85.78 | **72.79** |
| IDW baseline | 93.28 | 85.39 |
| Nearest-station | 111.85 | 100.45 |
| Improvement vs IDW | +8.0% | **+14.8%** |
| Improvement vs nearest | +23.3% | **+27.5%** |
| Stations evaluated | not stated | **66** |

Forecast metrics are unchanged (24 h still −4.22% vs persistence); only the spatial
estimator was retrained.

**This also strengthens the CAMS benchmark**: our estimator now scores **72.79 against
Copernicus CAMS's 106.7** on the same Delhi stations — **~32% better**, up from ~20%.

**Verified before merging:**
- Feature contract unchanged — 26 and 30 features, identical names, so
  `scripts/refresh_forecast.py` works against the new models untouched.
- No merge conflicts.
- His `source_attribution.csv` turned out to be **our own data** — he had merged main
  into his branch — so nothing of ours was lost; the refresher re-promoted fresher
  values immediately after.
- Regenerated with the new model: 1,600 cells, 209 wards, all gates passed.
- 20/20 tests still green.

**A blocker he fixed in the same push:** an earlier commit had deleted all four model
files, which would have broken both our refresh pipeline and his own production
predictor (it loads `models/spatial_estimator.json`). The merge restores them and
adds the `residual_spatial_model.json` his predictor needs.

**Also merged, not yet wired in:** `fetch_live_station_data.py`,
`production_spatial_predictor.py` (XGBoost + IDW 60/40 with residual correction and a
regime safety gate), `run_live_aqi.py`, `update_live_cell_aqi.py`, and ~1,400 lines of
tests. These overlap with the deployed live layer and are **deliberately not swapped
in before the finale**.

**Consequence worth acting on afterwards.** The live layer uses IDW rather than the
trained estimator, justified on two grounds that have both now weakened: the gap was
8% (now **14.8%**) and xgboost was not a dependency (it now is, for the refresh
subprocess). It stays on IDW for the finale only because that path is deployed and
verified. Switching is the first post-finale task.

### B12 · Parth's attribution engine — reviewed, not integrated
**Date:** 23 Aug 2026 · `Bind's Workspace/Parth's Work/` (local only)

Methodologically the strongest work on the team, and unusually honest: Gaussian
angular wind alignment, distance decay, dispersion by mixing height, temporal decay —
closed-form and configurable, no black-box ML. It ranks **individual sources**
(`ROAD_41`, `INDUSTRY_0`) rather than category percentages, which is far more
actionable for an inspector. Every emission factor is tagged
`PLACEHOLDER_NOT_FOR_REPORTING`, and his README states plainly that industry and
construction "emissions" are a presence index, not a mass rate.

**Not integrable before the finale, for three reasons:**

1. Output covers **64 cells**, not 1,600.
2. `cell_id` is `NCR_00000` — no join key to our integer grid.
3. It ran on **synthetic AQI**: median jump between adjacent 1 km cells is **97 AQI**,
   range 80–413. Real fields are far smoother.

It also needs a HERE key for the traffic half. **This is the roadmap answer** to "how
would you improve attribution?" — real code, not a promise.

*(Further entries appended as each objective lands.)*

---

## B13 - Dashboard map sizing and the worst-ward rings (24 Aug 2026)

**Problem.** At 1x zoom the map dominated the dashboard and pushed the ward detail
below the fold. Capping its height alone made it worse: Delhi is a portrait shape in a
landscape slot, so the SVG always fits by *height* - a wide, short box just wrapped a
small map in empty bands.

**Fix - two-pane from `xl` up.** The detail panels move from a row under the map into
a right rail (460px at `xl`, 680px at `2xl`). Because the map is height-bound at every
width, width handed to the rail costs the map nothing, and the map gains the full
column height. Below `xl` the stacked layout is unchanged.

| Viewport | Map before | Map after | Detail panels |
|---|---|---|---|
| 1920x1080 | 392x422 | **777x836** | all four on screen |
| 1440x820 | 283x305 | **535x576** | all four on screen |
| 1280x720 | 401x431 (page overflowed 13px) | **523x562** | rail scrolls, no overflow |
| 390x844 | 376x424 | 376x424 | flows, unchanged |

**Four defects found and fixed on the way:**

1. **Horizontal overflow at 1280.** The detail row's fixed track list
   (`280px 230px minmax(240px,1fr) 280px`) demanded 1030px inside a 1020px column.
   Removed - in a rail the widths come from the rail.
2. **Rail clipped mid-panel at 1440.** A compound arbitrary media variant
   (`md:[@media(min-height:820px)]:max-h-[52vh]`) sorts *after* `xl:` in the generated
   sheet, so the stacked cap still beat the two-pane cap. Bound it to `max-width:1279px`.
3. **Map ballooned to 1231px tall** on short-but-wide windows: the 100vh lock is off
   below 820px height, so the row took its height from the rail's natural content and
   `h-full` stretched the map to match. Both panes now share an `80vh` cap - inert when
   the lock is on, decisive when it is off.
4. **"Construction dust" truncated to "Constru...".** The mix legend used a *viewport*
   breakpoint (`sm:grid-cols-2`) inside a ~198px panel. Now a container query
   (`@[248px]:grid-cols-2`), so it goes two-up only when the panel can hold it.

**Also: the "ringed in red" wards were orange.** The stroke read
`var(--aqi-poor, #ff5a4e)` - but `--aqi-poor` *is* defined (`#ff9933`), so the intended
red fallback never applied and the rings were near-invisible against yellow wards. Now
`var(--aqi-very-poor)` (`#cc0033`) at 2.6px. Verified: 10 ringed on `/attribution`,
worst wards ringed on `/dashboard`, all `rgb(204,0,51)`.

**Verification.** 20/20 tests pass. Frontend builds clean. Seven viewports from
1920x1080 to 390x844: zero JS errors, zero horizontal overflow, zero clipped text.
`/`, `/attribution`, `/enforcement`, `/health` all render unchanged.

**Files.** `frontend/src/routes/dashboard.tsx`, `frontend/src/components/DelhiWardMap.tsx`.

---

## B14 - Red means dispatch; the Live API says so; README caught up (24 Aug 2026)

**1. Worst wards are now FILLED red, not outlined.** `urgentIds` on `DelhiWardMap`
painted a 2.6px stroke; at map scale that read as a hairline. It now paints the ward
solid `--aqi-very-poor` with a darker `--aqi-severe` edge. This deliberately overrides
the CPCB band colour, so **both maps carry a legend saying so** - "Worst 10 wards now -
rank, not band" on the dashboard, "N wards with a ranked source - dispatch first" on
enforcement. Without that label the map would imply an AQI band it does not mean.

**2. Enforcement is one queue, not two.** Removed `WARD DEPLOYMENT PLAN · coverage`
and the ward list under it. It ranked *wards*, which nobody can be dispatched to, and
it duplicated the map beside it. What survives is `RANKED SOURCES · dispatch first` -
a named junction, a specific site - plus the grid-cell fallback for when the source
feed is unavailable. Knock-on changes:

- the map now paints the **wards holding a ranked source** and numbers them by
  dispatch rank (best rank per ward, so two sources never stack on one centroid);
- search filters the source queue by name, ward or type instead of the ward plan;
- the team-mix chips are computed from the source queue;
- `/deployment` is no longer called by this page (the dashboard still uses it);
- `--aqi-poor` (orange) was replaced by `--aqi-very-poor` throughout, so the DISPATCH
  tag, the row border and the map now agree on what red means.

**3. The Live API badge is a badge.** It was a grey 11px caption with a 6px dot -
the single most load-bearing claim on the page, styled as a footnote. It is now a
solid green pill with a pulsing dot that **links to the running Swagger docs**, so a
judge can click it and watch the endpoints answer.

**4. README.** Verified all four live links return 200, then corrected what had gone
stale:

| Was | Now |
|---|---|
| "40+ air-quality sensors" | "~60 government air-quality instruments" |
| two features both numbered 5 | 6 features, and the endpoint table's feature column re-aligned |
| "18 offline tests" | 20 (matches `pytest`) |
| `/live` and `/enforcement/sources` undocumented | both in the endpoint table |
| Feature 3 = "ranked ward-deployment plan" | "20 dispatchable sources", matching the page |
| screenshots from 21 Jul (old UI) | regenerated 24 Aug, captions rewritten |
| "❌ no live-API operation" | ✅ the live layer, with the proxy-vs-modelled caveat stated |
| keys section listed Groq + voice only | `OPENAQ_API_KEY` and `FIRMS_MAP_KEY` documented, with what breaks without them |

**5. `render.yaml` was missing the live-layer keys.** `OPENAQ_API_KEY` and
`FIRMS_MAP_KEY` were never declared, so a fresh Blueprint deploy would never prompt
for them and would come up with `/live` reporting `available: false`. Both added as
`sync: false`. Production was unaffected (they were set by hand) - this was a
reproducibility bug, found by reading the file rather than by anything failing.

**Verification.** 20/20 tests. Clean build. Five routes × three viewports: zero JS
errors, zero horizontal overflow, and the removed section absent everywhere. Red fills
confirmed by computed style: 10 wards on `/dashboard`, 10 on `/attribution`, 15 on
`/enforcement` (20 sources across 15 wards). Production `/live` checked before the
push: 63 stations, 209 wards.

**Files.** `README.md`, `render.yaml`, `frontend/src/components/AppShell.tsx`,
`frontend/src/components/DelhiWardMap.tsx`, `frontend/src/routes/dashboard.tsx`,
`frontend/src/routes/enforcement.tsx`, `docs/screenshots/*`.

---

## B15 - Panel scrolling, and the "+nowh" label (24 Aug 2026)

**1. `+nowh`.** The forecast triplet built every tick label from the template
`+{h}h`. When "Now" joined the horizon list as a `HorizonSel`, it rendered as
`+nowh`. Now is a measurement, not an offset, so it gets its own label - and its own
aria-label ("Measured now: AQI …" rather than "+now hours: …").

**2. The blank band below the dashboard.** Reported from a real laptop: scrolling
slid the map and the rail up and out, leaving an empty half-screen while the sidebar
carried on.

The frame was locked to the viewport only when the window was **820 CSS px tall**.
That gate almost never fires on real hardware - a 1080p laptop at Windows 125%
scaling reports ~756 CSS px, at 150% ~630 - so the lock stayed off, the whole
document scrolled, and the short panes slid away above the tall sidebar.

The gate is gone. From `md` up the frame is locked at **any** height and each column
owns its scrollbar: sidebar, map, detail rail. Nothing is unreachable, because
nothing depends on the document scrolling; and nothing scrolls into emptiness,
because the document does not scroll at all. Below `md` the page still flows
normally - pinning a phone to 100vh is what made the ward detail unreachable in the
first place.

Dropped with the gate: the `80vh` pane caps and the `62vh` map cap. Those existed to
stop the map ballooning when the lock was off; with the lock always on they only
shrank the panes and left a gap at the bottom of tall screens.

**Verification.** `docScroll = 0` on `/dashboard`, `/enforcement` and `/health` at
1920x1023, 1536x756 (125%), 1280x630 (150%) and 1440x820 - and the sidebar reports
as an internally scrolling pane at every one. `/attribution` and `/` still scroll as
documents, which is right for long-form pages. Phone unchanged (2061px of natural
scroll). 20/20 tests, five routes x three viewports, zero JS errors, zero horizontal
overflow, no `+nowh` anywhere.

**Files.** `frontend/src/routes/dashboard.tsx`, `frontend/src/components/charts.tsx`.

---

## B16 - Data audit: five defects, found by checking numbers instead of pixels (24 Aug 2026)

Reported from the live site: the dashboard read far higher than the other tabs,
high-AQI wards were yellow while low-AQI wards were red, and the ranking looked
wrong. All of it was real. Previous rounds verified that pages *rendered*; none
verified that the numbers were *sane*. These were found by comparing every surface
against `/live` and against the weather.

### 1. The page showed two different numbers for the same instant

`PulseStrip` never received the measured layer, so with **Now** selected it fell back
to `asForecast("now") = "24"` and reported the **+24 h forecast** under a "measured
now" caption. The header strip said 141; the strip beneath it said 62. The band
census, the worst-ward name and the delta were all forecast numbers too, while the
map beside them painted live values. Fixed by passing `liveAqi` in and reading it
when the horizon is "now".

### 2. A ward's AQI contradicted its own pollutant panel

`_interpolate` blended station **AQIs** by IDW while blending **concentrations**
separately - and each pollutant carried its own weight sum, since a station missing
PM10 contributes to PM2.5 but not to PM10. AQI is a max-of-sub-indices and is not
linear in concentration, so the two answers diverged. One ward card read *"AQI 36 ·
driven by PM10 · PM10 144 µg/m³"* - and 144 µg/m³ of PM10 **is** 130 on the CPCB
scale. **56 of 209 wards disagreed with their own numbers, by up to 117 points.**

Now the concentrations are interpolated and the CPCB formula is applied once, to
them. Disagreement: **0 of 209**. This also repaired the ranking, which had been
sorting on a number the cards never showed.

### 3. Red meant rank, so clean wards were red and dirty wards were yellow

The worst-10 overlay painted wards solid red *over* the CPCB band colour. On a day
when nothing exceeded "Poor", the ten worst wards went red while genuinely worse air
elsewhere stayed yellow - the map contradicted its own legend, exactly as reported.

**One rule now, everywhere: fill is the CPCB band, outline is dispatch priority.**
Colour means air quality and nothing else, on all three maps. Bad air is red because
CPCB says it is, not because we ranked it. Today's worst ward is 267 - "Poor",
orange - and the map says orange.

### 4. "Measured 44m ago" was measured 3 hours ago

`data_age_hours` reported `min(ages)` - the single freshest station - as the age of
the whole layer, and `observed_at` took the newest timestamp. The typical station was
**3.3 h** old and the oldest **4.8 h**.

This mattered today. Open-Meteo shows rain beginning at **00:00 IST** (8.4 mm, RH
96-99%), and most readings predated it: the layer was showing pre-rain pollution
stamped "44m ago". Now the layer reports the **median** station's timestamp and age,
with `data_age_hours_newest` / `_oldest` alongside. The badge reads "3h ago", which
is what it is.

### 5. Stale stations counted as fully as fresh ones

`FRESH_HOURS = 6.0` admits a reading that spans a full diurnal swing and any passing
weather system. Lowered to **4.0** (62 stations → 50), and IDW now weights by
recency as well as distance (`1/(1+age_h)`): across a weather change an older reading
is not less precise, it describes a different atmosphere.

**On "the dashboard is inflated".** After all five fixes the measured average is 140
and the forecast average is 71. Both are real: the forecast targets 12:30 IST
tomorrow, the measurement is 02:00 IST tonight, and Delhi PM10 is far higher at night
when the boundary layer collapses. The dashboard was never inflating - it was
*mislabelling*, showing one layer under the other's caption and hiding how old the
readings were. The remaining gap is genuine and now legible: "140 measured 3h ago"
beside "▲ 78 worse vs tomorrow".

### 6. Mobile

The sidebar got its own scrollbar on desktop but not on a phone, where it ran to full
height. Now bounded to 70vh with `overscroll-contain`, matching desktop. Page padded
so the chat dock stops covering the last rows.

**Verification.** Every dashboard surface cross-checked against `/live` in the same
render: header 140 / I.P Extention 267, pulse strip 140, census 26+167+16 = 209, age
badge "3h ago" - all matching the endpoint exactly, on desktop and phone. 20/20
tests, five routes x two viewports, zero JS errors, zero horizontal overflow.

**Files.** `advisory/live.py`, `advisory/openaq.py`,
`frontend/src/routes/dashboard.tsx`, `frontend/src/routes/enforcement.tsx`,
`frontend/src/components/DelhiWardMap.tsx`.

---

## B17 - The live layer was computing a different index and calling it CPCB (24 Aug 2026)

Reported: every tab except the dashboard matched what CPCB publishes. That was
right, and the cause was a methodology error, not a display bug.

**The CPCB National AQI is defined on averaged concentrations** - a 24-hour mean for
PM2.5, PM10, NO2 and SO2, and the highest 8-hour rolling mean for O3 and CO. We were
pushing OpenAQ's **latest hourly value** through those breakpoints. That is a
different index wearing the same name, and it runs high at night, when the boundary
layer collapses and the spot reading sits far above the day's mean.

Measured on our own stations:

| Station | hourly basis | CPCB 24 h basis | error |
|---|---:|---:|---:|
| Anand Vihar | 351 | 194 | **+157** |
| Punjabi Bagh | 160 | 111 | +49 |
| R K Puram | 125 | 98 | +27 |
| Pusa | 99 | 77 | +22 (and named the wrong dominant pollutant) |

City-wide, the whole live layer moved from **avg 139 / max 272** to **avg 89 / max
145**, and the mean per-ward gap between the measured and forecast layers fell from
**68 AQI to 27**. Bawana: 111 → 90 against a forecast of 102. The layers now tell the
same story because they are finally measuring the same thing.

`station_windows()` - which computes exactly this, and whose docstring already
described the error - had been written and left unwired because it was too slow for
the request path. Three things made it usable:

1. **One sensor per station per pollutant.** Many CPCB stations carry two generations
   of sensor for the same parameter: a legacy one that stopped reporting years ago
   and a live one. We were querying both, wasting **170 of 423 requests** on series
   that can only come back empty - and against a 55/min limit that waste starved the
   rest of the fill. Keeping the newest sensor id per (station, pollutant) took
   coverage from **1 station to 56 of 56**.
2. **Never on the request path.** `cached_station_windows()` returns immediately -
   `{}` on the first call, which serves spot readings *labelled as such* - and fills
   once in the background (~7.5 min). Every cycle after indexes the CPCB window. A
   stale cache keeps being served during a refill, because a 30-minute-old 24-hour
   mean beats a spot reading.
3. **Never a mixed field.** A map where some stations carry a 24-hour mean and others
   a spot reading is not a measurement of anything - the contrast between two wards
   would partly reflect which station happened to have history. The whole network
   switches together, gated at 60% coverage.

Also added CPCB's **data-completeness rule**: a sub-index needs enough of the window
to be honest. Relaxed to two thirds (CPCB uses 16 of 24) because OpenAQ mirrors CPCB
with gaps of its own, but a "24-hour mean" built from 2 readings falls back to the
spot value rather than pretending.

The basis is now stated in the freshness strip - "CPCB · 56 stations · 24h average
basis · latest 3h ago" - with the full method on hover. It is the reason the number
matches CPCB, and it is the first thing a jury will ask.

**Verification.** 20/20 tests. Live layer cross-checked against `/live` in the same
render: header 90 / Sarita Vihar 145, pulse strip 90, basis "24h average basis" -
matching the endpoint exactly. Per-ward: Bawana 90 vs forecast 102, Ashok Vihar 97 vs
102, Sangam Park 95 vs 106.

**Files.** `advisory/live.py`, `advisory/openaq.py`,
`frontend/src/components/DataFreshness.tsx`, `frontend/src/lib/api.ts`.

---

## B18 - One horizon control on every map, and no holes left in the city (24 Aug 2026)

**1. Attribution and Enforcement now carry the horizon control.** Both hardcoded
`horizon="24"` and said nothing about it, so a reader comparing them with the
dashboard's "Now" was comparing two different instants and concluding the numbers
disagreed. Both now have the same Now / +24 / +48 / +72 switch, default **Now**, with
the basis spelled out beside it ("AQI measured now (CPCB 24 h basis)"). On attribution
the switch drives the map, the worst-10 table *and* the band grouping in "source mix
by severity", so the whole page moves together; on enforcement it drives the ward
colouring while the dispatch ranking stays put, which is what it ranks on. Verified by
clicking: Now puts Kashmere Gate at 146 on top, +72 h puts Sangam Park at 117.

**2. The dashboard map's three caption lines are gone**, as asked - "209 real wards ·
click any ward", "Grey = ward outside the 209 we model", "Outlined = worst 10 now".

**3. No grey wards left.** The shapefile carries 287 MCD wards and the pipeline models
209; the other 78 rendered grey and read as a hole in the map. They are filled by
inverse-distance weighting over their three nearest modelled neighbours - the same
k=3, power-2 IDW the live layer already uses to carry stations onto ward centroids, so
this is one documented method rather than a second invented one. Centroids are in SVG
space, but the projection is affine over an area this small, so nearest-in-SVG and
nearest-on-the-ground agree, and only the ordering matters.

The qualifier travels with the number instead of sitting in a legend the reader has to
remember: hovering an interpolated ward reads *"estimated from neighbouring wards"*
where a modelled ward names its dominant source. All 290 shapes are now painted at
every horizon.

**4. The landing page said "Worst first · +now h".** Now is a measurement, not an
offset; it reads "Worst first · measured now".

**Verification.** 20/20 tests. Five routes x two viewports, zero JS errors, zero
horizontal overflow. Horizon switch present on all four map pages; grey ward count 0
on every one; none of the three removed captions found anywhere; no "+now h".

**Files.** `frontend/src/components/DelhiWardMap.tsx`,
`frontend/src/routes/attribution.tsx`, `frontend/src/routes/enforcement.tsx`,
`frontend/src/routes/dashboard.tsx`, `frontend/src/routes/index.tsx`.

---

## B19 - Worst wards filled red on every map (24 Aug 2026)

The worst-hit wards were outlined. Outlines disappear at projector distance, so they
are filled solid `--aqi-very-poor` at full opacity, on every map that ranks wards:

| Map | Red wards | What red marks |
|---|---:|---|
| Landing hero | 10 | worst at +24 h, the number that map paints |
| Dashboard | 10 | worst at the selected horizon |
| Attribution | 10 | worst at the selected horizon |
| Enforcement | 15 | wards holding a ranked source - dispatch first |

**Why this is safe now and was not before.** This is a rank overlay: it deliberately
overrides the CPCB band colour for those wards. When it was first tried, the map
painted the +24 h forecast while the ranking sorted on live values, and the ward AQI
disagreed with its own pollutant panel (B16, B17) - so red landed on wards that were
cleaner than yellow ones beside them, which is exactly what was reported. Both causes
are fixed: every map now ranks on the same number it paints, at whatever horizon is
selected, so the red wards are that map's highest by construction. The hero map ranks
on +24 h because that is what it paints.

The CPCB band is never lost - it is one hover away, and the hover card now appends
"· worst 10" so a red ward reads, for example, "Moderate · worst 10 · Traffic/Roads"
rather than implying a band it is not in.

**Verification.** 20/20 tests. Five routes x two viewports: red counts 10/10/10/15/0
exactly as intended (Citizen advisory has no ward map), zero JS errors, zero
horizontal overflow, zero grey wards.

**Files.** `frontend/src/components/DelhiWardMap.tsx`, `frontend/src/routes/index.tsx`,
`frontend/src/routes/attribution.tsx`, `frontend/src/routes/enforcement.tsx`.

---

## B20 - Scroll to zoom, drag to pan, pinch on touch (24 Aug 2026)

The maps zoomed only through the +/- buttons, and did it about the centre, so
reaching a specific ward meant zooming and then hoping.

**Zoom now anchors on the pointer.** Panning is held as the map point at the centre
of the viewport, which makes the clamp trivial and survives any zoom change without a
second source of truth. Screen-to-map conversion accounts for the letterboxing that
`preserveAspectRatio="xMidYMid meet"` introduces - ignoring it made the anchor drift
away from the cursor. Range widened from 0.6-4x to 0.6-8x.

**Wheel behaviour depends on whether the page still has somewhere to scroll.** A map
that eats the wheel is a trap on a scrolling page, so:

| Page | Plain wheel | Ctrl / ⌘ + wheel |
|---|---|---|
| Dashboard, Enforcement (viewport-locked) | zooms | zooms |
| Attribution, landing (page scrolls) | page scrolls, map shows a one-off hint | zooms |
| Landing hero (ambient) | page scrolls - no listener at all | page scrolls |

The check is `scrollHeight > clientHeight` at the moment of the event, so it follows
the layout rather than a hardcoded list of routes, and a plain wheel says why it did
nothing instead of silently ignoring the reader.

**Also added:** drag to pan (clamped to the padded world so the city cannot be thrown
off-screen), double-click to zoom in, and two-finger pinch. A gesture that moves more
than 4px is a drag and no longer fires a ward selection on release - verified both
ways: a plain click still selects, a drag no longer does.

**`touch-action` is progressive, and this one matters.** `none` is what lets us own
drag and pinch, but on a phone the map fills the column, so owning touch at rest means
a swipe over it cannot scroll the page. At 1x the page keeps the gesture (`pan-y`);
once the reader has zoomed in they clearly want to move around the map, so we take it.
Verified on a 390px viewport: `touch-action: pan-y`, page still scrolls.

**One latent bug fixed on the way.** The hover card was positioned from
`cx / GEO.w`, which assumes the viewBox is the whole unzoomed map. It was already
slightly off at 1x (the viewBox carries 14% padding) and would have detached
completely once panning existed. It now derives from the live viewBox.

**Verification.** 20/20 tests. Wheel zoom confirmed on the dashboard (viewBox
1117 → 454 wide, anchored at the cursor); attribution confirmed to scroll the page and
show the hint on a plain wheel, and to zoom on Ctrl+wheel without moving the page;
drag pans while the viewBox size holds; double-click zooms. Five routes x two
viewports: zero JS errors, zero horizontal overflow, red counts unchanged.

**Files.** `frontend/src/components/DelhiWardMap.tsx`.

---

## B21 - RAG, a knowledge graph, and a four-agent pipeline (24 Aug 2026)

The PS5 suggested-technologies list names multi-agent systems, RAG over document corpora
and knowledge graphs, and we had none. Each is now real, each earns its place, and each
found a bug on the way in. No new API keys: Groq covers the models, and retrieval is pure
standard library.

### RAG over the regulatory corpus

`advisory/corpus/*.md` holds 5 documents (CPCB National AQI method, GRAP stages, WHO 2021
guideline values, NCAP targets, pollutant source signatures), split at headings into 23
passages, each carrying publisher, year and URL. `advisory/rag.py` indexes them with
**BM25**, pure standard library.

Not embeddings, deliberately: a sentence-transformer means torch, roughly half a gigabyte
in a container that currently starts in seconds. BM25 is the standard lexical first stage
in production stacks, it is exact and inspectable, and on a closed regulatory vocabulary
the query words *are* the document words.

The advisory could already name the authority behind a number. It can now quote what that
authority says.

### Knowledge graph

`advisory/graph.py` builds a typed property graph from data the pipeline already
produces: **266 nodes, 935 edges** across Ward, SourceCategory, Pollutant, Band,
GrapStage, Persona, Authority, EnforcementTarget, Team and Action, with relations
ATTRIBUTED_TO, IN_BAND, TRIGGERS, EMITS, INDICATES, HOSTS, HANDLED_BY, PERFORMS.

`explain_ward()` walks it: ward to band to GRAP stage on the health side, ward to dominant
source to pollutant evidence to team to action on the enforcement side. The evidence chain
the product always implied is now a thing you can request. Rebuilt on every forecast
refresh so it never lags the data.

### Four-agent pipeline

`advisory/agents.py`. One LLM call had a structural weakness we could not close from
inside the prompt: nothing checked what came back.

| Stage | Model | Job |
|---|---|---|
| Router | gpt-oss-20b | classify intent, choose what evidence would answer it |
| Retriever | deterministic | BM25 passages + the ward's evidence chain |
| Analyst | gpt-oss-120b | compose from retrieved evidence only |
| **Verifier** | gpt-oss-20b | **reject any claim the evidence does not support** |

A rejected draft is not retried into a worse one - it falls back to the deterministic CPCB
template. The pipeline can only degrade toward the safe answer. `/chat` now reports
`reasoning.mode` as `agentic_rag` or `single_pass`, with citations and the evidence chain.

### Three bugs this work exposed

1. **A live safety bug in `band_for_aqi`.** The CPCB ranges are integer and contiguous
   only over integers - 0-50, then 51-100. A fractional AQI between an upper bound and the
   next lower bound matched no band and fell through to the "above the top range" case,
   returning **Severe**. Interpolated ward AQI is fractional by construction: **a ward
   reading 50.4 was being handed Severe health guidance**, and two of 209 wards were in
   that state. Now selected on the lower bound, with NaN guarded explicitly.
2. **The analyst inverted persona escalation.** Told a persona was "advised 2 bands
   earlier", it concluded a child's guidance comes from a *better* band and drafted "the
   condition for your child is treated like the Good band. At this level it is generally
   safe for children to play outdoors" - the exact inverse of the rule. **The verifier
   caught it on both runs.** Root cause fixed in the wording; the backstop stays as the net.
3. **The verifier over-rejected.** "Supported by" reads to a model as "restated verbatim",
   so it rejected drafts that correctly used measured facts. Rewritten as a closed list of
   five checkable violations, defaulting to approval.

### Safety of the addition

Additive only. New modules, new endpoints (`/ai/pipeline`, `/rag/search`, `/graph`,
`/graph/ward/{id}`, `/graph/ward/{id}/subgraph`). Every handler degrades to
`available: false` rather than raising. Verified with the LLM key removed: agents go
unavailable, `/chat` falls back to single-pass and still answers, RAG and the graph keep
working because both are deterministic. The frontend panel renders nothing at all unless
the endpoint confirms the layer is up.

**Verification.** 20/20 tests. 17 endpoints returning 200. Five routes x two viewports,
zero JS errors, zero horizontal overflow. Agent pipeline answered 6 of 7 persona-varied
questions; the one rejection was the verifier correctly catching a draft that claimed GRAP
Stage III applies at AQI 101-200 when it is 401-450.

**Files.** `advisory/corpus/*.md`, `advisory/rag.py`, `advisory/graph.py`,
`advisory/agents.py`, `advisory/chat.py`, `advisory/health_bands.py`,
`backend/advisory_api.py`, `frontend/src/components/ReasoningPanel.tsx`,
`frontend/src/routes/health.tsx`.

---

## B21 - Multi-agent, RAG and a knowledge graph, at no latency cost (25 Aug 2026)

Three technologies the problem statement lists and we did not have. Added as working
code rather than as claims, and measured before being recommended.

**RAG over a regulatory corpus.** `advisory/corpus/` holds five documents - the CPCB
National AQI method, the GRAP stage ladder, the WHO 2021 guideline values, NCAP targets,
and the pollutant-signature basis for attribution - split into 23 passages at their
headings so a band table or a GRAP stage stays whole. `advisory/rag.py` retrieves with
BM25, in pure standard library. Vector search would have meant a sentence-transformer,
which means torch, which means roughly half a gigabyte in a container that currently
starts in seconds; on a closed regulatory vocabulary where the query words *are* the
document words, lexical retrieval is the right tool rather than a compromise.

**A typed knowledge graph.** `advisory/graph.py` builds 266 nodes and 935 edges from
data the pipeline already produces: wards, bands, GRAP stages, source categories,
pollutants, personas, enforcement targets, teams and actions. `explain_ward()` walks it
to return the evidence chain the product always implied but could never show - ward, to
band, to GRAP stage, to persona escalation, to dominant source, to the team and action in
the queue.

**A four-agent pipeline** in `advisory/agents.py`: router, retriever, analyst, verifier,
with rejection falling back to the deterministic CPCB template so the answer can only get
safer.

### Two things the measurements changed

**The LLM router was making retrieval worse.** Its rewrite of "Can my child play outside
this evening?" was "Delhi child outdoor activity air quality regulation", which retrieved
GRAP and NCAP boilerplate; the raw question retrieves "Sensitive groups" and "Populations
at higher risk". Generic padding words match generic passages. Replaced with the
deterministic keyword router `chat.py` already had: faster, more accurate, cannot fail.

**The LLM verifier cost four seconds and could hallucinate its own verdict.** It is now
deterministic: it rejects a numeric claim that traces to neither the measured values nor
a retrieved passage, an authority named without a supporting passage, and any CPCB band
that is neither the measured band nor this persona's escalated band. Checking claims in
code is free, instant, and cannot itself invent anything.

Result: **one model call per reply, the same as before the pipeline existed.**

| | Chat reply |
|---|---:|
| Before this work | 1.55 s |
| With the full pipeline | **1.81 s** |

Four test questions, all completing as `agentic_rag` with no fallback.

### A live bug found while building it

Building the graph surfaced a genuine defect in `band_for_aqi`. The CPCB ranges are
integer and contiguous only over integers - 0-50, then 51-100 - so a fractional AQI
sitting between an upper bound and the next lower bound matched no band and fell through
to the "above the top range" case, returning **Severe**. On the deployed site this was
labelling four wards Severe: Harsh Vihar at AQI 100, Karawal Nagar West at 51, Raja
Garden and Gandhi Nagar at 50. Dark red on the map, Severe health guidance in the
advisory. Now selected on the lower bound, with NaN guarded explicitly rather than
picking a band by accident.

**New endpoints.** `/ai/pipeline`, `/rag/search`, `/rag/ask`, `/graph`, `/graph/ward/{id}`.
All additive and read-only; every one degrades to `available: false`.

**Verification.** 20/20 tests. Five routes x two viewports: zero JS errors, zero
horizontal overflow, no degraded state rendered anywhere. Graph warms in a background
thread at startup so its ~3 s build never lands on a request.

**Files.** `advisory/corpus/*.md`, `advisory/rag.py`, `advisory/graph.py`,
`advisory/agents.py`, `advisory/chat.py`, `advisory/health_bands.py`,
`backend/advisory_api.py`, `frontend/src/components/ReasoningPanel.tsx`,
`frontend/src/lib/api.ts`, `frontend/src/routes/health.tsx`.

---

## B22 - The OpenAQ key died, and the site spun forever instead of saying so (25 Aug 2026)

**Reported:** "fetching station readings…" across the whole site.

**Root cause, external:** the OpenAQ API key now returns **401 Invalid credentials**.
Verified directly against `api.openaq.org/v3/locations`, and verified that the key in
Render is byte-identical to the local one (both 64 chars, same SHA-256 prefix) and that
we send the correct `X-API-Key` header. The key has been revoked or expired upstream.
Nothing in our code caused it and no code change can fix it - it needs a new key.

**What our code got wrong, and this is the part worth fixing.** The failure was
invisible and unbounded:

1. **`live_wards` did not cache its failures.** Both early returns left `_result_cache`
   untouched, so `cached_live_wards()` kept answering `state: "warming"` - the state that
   means *we have not tried yet*. The feed had been failing for forty minutes and every
   screen showed a spinner that could never resolve.
2. **The refresh loop swallowed the reason.** `except Exception: pass` with no log, so
   the service logs contained no hint at all. Diagnosis had to start from the Render API.
3. **The UI claimed measurements it did not have.** With the live map empty,
   `aqiForSel` correctly falls through to the +24 h forecast - but four captions still
   said "measured now" over those forecast numbers, which is precisely the mislabelling
   fixed in B16.

**Fixed.** Failures are now recorded, with a rule that a transient upstream failure can
never erase readings we still hold: it marks them `stale` and attaches `refresh_error`
instead. Past its TTL the request path serves the last good reading labelled stale rather
than falling back to "warming". The refresher logs both exceptions and empty results. The
warm-up message only shows for a genuine warm-up (90 s), never indefinitely. And every
caption that said "measured now" now checks whether anything was actually measured:

| Surface | With the feed down |
|---|---|
| Freshness strip | "live station feed unavailable - forecast below is unaffected" |
| Band census | "+24 h forecast · live feed down" |
| Ward detail | "CPCB band · +24 h forecast · live feed down" |
| Ward finder | "Worst wards · +24 h forecast · live feed down" |
| Ward's measured panel | "no live station reading for this ward" |

**Verification.** With the dead key in place: 20/20 tests, five routes render with zero
JS errors, no spinner on any route, the dashboard fully usable on forecast data, and
`/live` now reports the actual reason instead of "warming". Log line confirmed:
`[live-refresh] no data: the station feed returned nothing usable…`

**Files.** `advisory/live.py`, `backend/advisory_api.py`,
`frontend/src/components/DataFreshness.tsx`, `frontend/src/routes/dashboard.tsx`.
