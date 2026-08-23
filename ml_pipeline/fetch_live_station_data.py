"""
fetch_live_station_data.py

AirGrid — LIVE STATION DATA FETCH

Purpose
-------
Fetch the latest available real pollutant observations for the existing
AirGrid monitoring stations without rebuilding historical data.

This script:
    1. Loads the existing stations_static.csv
    2. Discovers current OpenAQ locations in the Delhi bbox
    3. Matches existing AirGrid stations to OpenAQ location IDs
    4. Fetches latest values using parameter-level /latest endpoints
    5. Builds station-level pollutant rows
    6. Calculates CPCB-style AQI from available pollutants
    7. Saves data/live_station_data.csv

IMPORTANT
---------
This script does NOT:
    - rebuild training_dataset.csv
    - retrain the spatial model
    - retrain the forecast models
    - modify historical data
"""

import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta


# ============================================================
# CONFIG
# ============================================================

OPENAQ_BASE = "https://api.openaq.org/v3"
OPENAQ_API_KEY = os.environ.get("OPENAQ_API_KEY", "")

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

DATA_DIR = os.path.join(PROJECT_ROOT, "data")

STATIONS_PATH = os.path.join(
    DATA_DIR,
    "stations_static.csv"
)

OUTPUT_PATH = os.path.join(
    DATA_DIR,
    "live_station_data.csv"
)

# Existing AirGrid Delhi bbox
BBOX = "76.80,28.40,77.40,28.90"

# OpenAQ parameter IDs.
#
# OpenAQ documentation:
# pm10 = 1
# pm25 = 2
# no2  = 7
# co   = 8
# so2  = 9
# o3   = 10
#
# These are parameter IDs, not sensor IDs.

PARAMETER_IDS = {
    "pm10": 1,
    "pm25": 2,
    "no2": 7,
    "co": 8,
    "so2": 9,
    "o3": 10,
}

POLLUTANTS = set(PARAMETER_IDS.keys())

# We only consider data that is reasonably recent.
#
# This is deliberately a little larger than one hour because different
# stations do not necessarily report at exactly the same time.
LOOKBACK_HOURS = 6

MAX_RETRIES = 5

# Maximum acceptable spatial difference when matching an AirGrid station
# to an OpenAQ location.
MAX_MATCH_DISTANCE_KM = 2.0


# ============================================================
# CPCB AQI BREAKPOINTS
# ============================================================

AQI_BP = {
    "PM2.5": [
        (0, 30, 0, 50),
        (30, 60, 51, 100),
        (60, 90, 101, 200),
        (90, 120, 201, 300),
        (120, 250, 301, 400),
        (250, 500, 401, 500),
    ],

    "PM10": [
        (0, 50, 0, 50),
        (50, 100, 51, 100),
        (100, 250, 101, 200),
        (250, 350, 201, 300),
        (350, 430, 301, 400),
        (430, 600, 401, 500),
    ],

    "NO2": [
        (0, 40, 0, 50),
        (40, 80, 51, 100),
        (80, 180, 101, 200),
        (180, 280, 201, 300),
        (280, 400, 301, 400),
        (400, 800, 401, 500),
    ],

    "SO2": [
        (0, 40, 0, 50),
        (40, 80, 51, 100),
        (80, 380, 101, 200),
        (380, 800, 201, 300),
        (800, 1600, 301, 400),
        (1600, 2100, 401, 500),
    ],

    "O3": [
        (0, 50, 0, 50),
        (50, 100, 51, 100),
        (100, 168, 101, 200),
        (168, 208, 201, 300),
        (208, 748, 301, 400),
        (748, 1000, 401, 500),
    ],

    "CO": [
        (0, 1, 0, 50),
        (1, 2, 51, 100),
        (2, 10, 101, 200),
        (10, 17, 201, 300),
        (17, 34, 301, 400),
        (34, 50, 401, 500),
    ],
}


def sub_index(conc, pollutant):
    """
    Calculate pollutant-specific AQI sub-index.
    """

    if pd.isna(conc):
        return None

    try:
        conc = float(conc)
    except (TypeError, ValueError):
        return None

    if conc < 0:
        return None

    for clo, chi, ilo, ihi in AQI_BP[pollutant]:
        if clo <= conc <= chi:
            return ilo + (
                (ihi - ilo)
                / (chi - clo)
            ) * (conc - clo)

    return None


def aqi_from_row(row):
    """
    AQI is the maximum available pollutant sub-index.
    """

    mapping = {
        "pm25": "PM2.5",
        "pm10": "PM10",
        "no2": "NO2",
        "so2": "SO2",
        "o3": "O3",
        "co": "CO",
    }

    sub_indices = []

    for raw, pollutant in mapping.items():

        value = row.get(raw)

        if value is None:
            continue

        if pd.isna(value):
            continue

        si = sub_index(
            value,
            pollutant
        )

        if si is not None:
            sub_indices.append(si)

    if not sub_indices:
        return np.nan

    return round(max(sub_indices), 1)


# ============================================================
# GEO UTILITIES
# ============================================================

def haversine_km(lat1, lon1, lat2, lon2):
    """
    Great-circle distance between two WGS84 coordinates.
    """

    lat1 = np.radians(float(lat1))
    lon1 = np.radians(float(lon1))
    lat2 = np.radians(float(lat2))
    lon2 = np.radians(float(lon2))

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        np.sin(dlat / 2.0) ** 2
        + np.cos(lat1)
        * np.cos(lat2)
        * np.sin(dlon / 2.0) ** 2
    )

    return float(
        6371.0
        * 2.0
        * np.arcsin(np.sqrt(a))
    )


def normalize_name(value):
    """
    Normalize station names for exact-ish matching.
    """

    if pd.isna(value):
        return ""

    value = str(value).lower()

    replacements = [
        (" - dpcc", ""),
        (" - cpcb", ""),
        (" - cppcb", ""),
        (" - uppcb", ""),
        (" - hspcb", ""),
        (" - imd", ""),
        (" - iitm", ""),
    ]

    for old, new in replacements:
        value = value.replace(old, new)

    value = (
        value
        .replace(",", " ")
        .replace("-", " ")
        .replace("_", " ")
    )

    value = " ".join(value.split())

    return value


# ============================================================
# HTTP
# ============================================================

def openaq_get(path, params=None):
    """
    Safe OpenAQ GET request.

    Important difference from the previous implementation:
    429 responses use Retry-After / rate-limit information when available.
    """

    if not OPENAQ_API_KEY:
        raise RuntimeError(
            "OPENAQ_API_KEY is not set.\n\n"
            "Set it before running this script."
        )

    url = f"{OPENAQ_BASE}{path}"

    headers = {
        "X-API-Key": OPENAQ_API_KEY
    }

    if params is None:
        params = {}

    for attempt in range(MAX_RETRIES):

        try:

            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=30,
            )

            # --------------------------------------------------------
            # SUCCESS
            # --------------------------------------------------------

            if response.status_code == 200:
                return response.json()

            # --------------------------------------------------------
            # RATE LIMIT
            # --------------------------------------------------------

            if response.status_code == 429:

                retry_after = response.headers.get(
                    "Retry-After"
                )

                if retry_after is not None:

                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = min(
                            60,
                            2 ** attempt
                        )

                else:

                    wait = min(
                        60,
                        2 ** attempt
                    )

                # Do not hammer OpenAQ.
                print(
                    f"  HTTP 429 on {path}; "
                    f"waiting {wait:.1f}s..."
                )

                time.sleep(wait)

                continue

            # --------------------------------------------------------
            # TEMPORARY SERVER ERRORS
            # --------------------------------------------------------

            if response.status_code in {
                408,
                500,
                502,
                503,
                504,
            }:

                wait = min(
                    30,
                    2 ** attempt
                )

                print(
                    f"  HTTP {response.status_code} on {path}; "
                    f"retrying in {wait:.1f}s..."
                )

                time.sleep(wait)

                continue

            # --------------------------------------------------------
            # OTHER HTTP ERRORS
            # --------------------------------------------------------

            response.raise_for_status()

        except requests.RequestException as exc:

            if attempt == MAX_RETRIES - 1:
                raise

            wait = min(
                30,
                2 ** attempt
            )

            print(
                f"  Request error on {path}: {exc}; "
                f"retrying in {wait:.1f}s..."
            )

            time.sleep(wait)

    raise RuntimeError(
        f"OpenAQ request failed after "
        f"{MAX_RETRIES} attempts: {path}"
    )


# ============================================================
# LOAD EXISTING AIRGRID STATIONS
# ============================================================

def load_stations():

    if not os.path.exists(STATIONS_PATH):

        raise FileNotFoundError(
            f"Missing:\n{STATIONS_PATH}\n\n"
            "The existing stations_static.csv is required."
        )

    stations = pd.read_csv(
        STATIONS_PATH
    )

    required = {
        "station",
        "latitude",
        "longitude",
    }

    missing = required - set(
        stations.columns
    )

    if missing:

        raise RuntimeError(
            f"stations_static.csv missing: "
            f"{sorted(missing)}"
        )

    stations = (
        stations
        .drop_duplicates(
            subset=["station"]
        )
        .copy()
    )

    stations["latitude"] = pd.to_numeric(
        stations["latitude"],
        errors="coerce"
    )

    stations["longitude"] = pd.to_numeric(
        stations["longitude"],
        errors="coerce"
    )

    stations = stations.dropna(
        subset=[
            "latitude",
            "longitude",
        ]
    )

    stations["normalized_station"] = (
        stations["station"]
        .map(normalize_name)
    )

    return stations


# ============================================================
# OPENAQ LOCATION DISCOVERY
# ============================================================

def fetch_locations():

    print(
        "\n[1/4] Fetching OpenAQ location metadata..."
    )

    rows = []

    page = 1

    while True:

        data = openaq_get(
            "/locations",
            {
                "bbox": BBOX,
                "iso": "IN",
                "limit": 1000,
                "page": page,
            }
        )

        results = data.get(
            "results",
            []
        )

        if not results:
            break

        for location in results:

            coords = (
                location.get(
                    "coordinates"
                )
                or {}
            )

            location_id = location.get(
                "id"
            )

            station_name = location.get(
                "name",
                f"loc_{location_id}"
            )

            lat = coords.get(
                "latitude"
            )

            lon = coords.get(
                "longitude"
            )

            if location_id is None:
                continue

            if lat is None or lon is None:
                continue

            rows.append({
                "location_id": location_id,
                "station": station_name,
                "latitude": lat,
                "longitude": lon,
            })

        if len(results) < 1000:
            break

        page += 1

    result = pd.DataFrame(
        rows
    )

    if result.empty:

        raise RuntimeError(
            "OpenAQ returned no Delhi locations."
        )

    result = (
        result
        .drop_duplicates(
            subset=["location_id"]
        )
        .reset_index(drop=True)
    )

    result["normalized_station"] = (
        result["station"]
        .map(normalize_name)
    )

    print(
        f"  → {len(result):,} OpenAQ locations"
    )

    return result


# ============================================================
# MATCH AIRGRID STATIONS TO OPENAQ LOCATIONS
# ============================================================

def match_stations(
    airgrid_stations,
    locations,
):

    print(
        "\n[2/4] Matching AirGrid stations "
        "to OpenAQ locations..."
    )

    matched = []

    used_location_ids = set()

    exact_matches = 0
    coordinate_matches = 0
    unmatched = 0

    for _, station in airgrid_stations.iterrows():

        station_name = station["station"]

        station_norm = station[
            "normalized_station"
        ]

        lat = float(
            station["latitude"]
        )

        lon = float(
            station["longitude"]
        )

        # --------------------------------------------------------
        # First: normalized station-name match
        # --------------------------------------------------------

        candidates = locations[
            locations[
                "normalized_station"
            ] == station_norm
        ]

        # Do not reuse the same OpenAQ location for multiple
        # AirGrid stations unless absolutely necessary.
        candidates = candidates[
            ~candidates["location_id"].isin(
                used_location_ids
            )
        ]

        if not candidates.empty:

            # If multiple locations have the same name,
            # choose geographically closest.
            candidates = candidates.copy()

            candidates["distance_km"] = candidates.apply(
                lambda r: haversine_km(
                    lat,
                    lon,
                    r["latitude"],
                    r["longitude"],
                ),
                axis=1,
            )

            best = candidates.sort_values(
                "distance_km"
            ).iloc[0]

            if best["distance_km"] <= MAX_MATCH_DISTANCE_KM:

                matched.append({
                    "airgrid_station": station_name,
                    "location_id": int(
                        best["location_id"]
                    ),
                    "openaq_station": best["station"],
                    "latitude": lat,
                    "longitude": lon,
                    "match_distance_km": float(
                        best["distance_km"]
                    ),
                })

                used_location_ids.add(
                    int(best["location_id"])
                )

                exact_matches += 1

                continue

        # --------------------------------------------------------
        # Second: nearest geographic match
        # --------------------------------------------------------

        candidates = locations[
            ~locations["location_id"].isin(
                used_location_ids
            )
        ].copy()

        if candidates.empty:
            unmatched += 1
            continue

        candidates["distance_km"] = candidates.apply(
            lambda r: haversine_km(
                lat,
                lon,
                r["latitude"],
                r["longitude"],
            ),
            axis=1,
        )

        best = candidates.sort_values(
            "distance_km"
        ).iloc[0]

        if best["distance_km"] <= MAX_MATCH_DISTANCE_KM:

            matched.append({
                "airgrid_station": station_name,
                "location_id": int(
                    best["location_id"]
                ),
                "openaq_station": best["station"],
                "latitude": lat,
                "longitude": lon,
                "match_distance_km": float(
                    best["distance_km"]
                ),
            })

            used_location_ids.add(
                int(best["location_id"])
            )

            coordinate_matches += 1

        else:

            unmatched += 1

    result = pd.DataFrame(
        matched
    )

    print(
        f"  → matched: {len(result)}"
    )

    print(
        f"     name matches      : {exact_matches}"
    )

    print(
        f"     coordinate matches: {coordinate_matches}"
    )

    print(
        f"     unmatched         : {unmatched}"
    )

    if result.empty:

        raise RuntimeError(
            "Could not match any AirGrid stations "
            "to OpenAQ locations."
        )

    print(
        "\n  Matched station examples:"
    )

    print(
        result[
            [
                "airgrid_station",
                "openaq_station",
                "location_id",
                "match_distance_km",
            ]
        ]
        .head(10)
        .to_string(index=False)
    )

    return result


# ============================================================
# FETCH LATEST PARAMETER DATA
# ============================================================

def fetch_parameter_latest(
    parameter_name,
    parameter_id,
    location_ids,
    datetime_min,
):

    print(
        f"\n  Fetching latest {parameter_name}..."
    )

    rows = []

    page = 1

    location_ids = set(
        int(x)
        for x in location_ids
    )

    while True:

        data = openaq_get(
            f"/parameters/{parameter_id}/latest",
            {
                "limit": 1000,
                "page": page,
                "datetime_min": datetime_min.isoformat(),
            }
        )

        results = data.get(
            "results",
            []
        )

        if not results:
            break

        for result in results:

            location_id = result.get(
                "locationsId"
            )

            if location_id is None:
                continue

            try:
                location_id = int(
                    location_id
                )
            except (
                TypeError,
                ValueError,
            ):
                continue

            # Keep only stations belonging to AirGrid.
            if location_id not in location_ids:
                continue

            value = result.get(
                "value"
            )

            if value is None:
                continue

            dt = (
                result.get(
                    "datetime"
                )
                or {}
            )

            timestamp = dt.get(
                "utc"
            )

            if timestamp is None:
                continue

            rows.append({
                "location_id": location_id,
                "parameter": parameter_name,
                "timestamp": timestamp,
                "value": value,
                "sensor_id": result.get(
                    "sensorsId"
                ),
            })

        if len(results) < 1000:
            break

        page += 1

    result = pd.DataFrame(
        rows
    )

    if result.empty:

        print(
            f"     → no recent {parameter_name} "
            f"observations for AirGrid stations"
        )

        return result

    result["timestamp"] = pd.to_datetime(
        result["timestamp"],
        utc=True,
        errors="coerce",
    )

    result["value"] = pd.to_numeric(
        result["value"],
        errors="coerce",
    )

    result = result.dropna(
        subset=[
            "timestamp",
            "value",
        ]
    )

    print(
        f"     → {len(result):,} "
        f"{parameter_name} observations"
    )

    return result


# ============================================================
# BUILD STATION TABLE
# ============================================================

def build_station_table(
    matches,
    raw,
):

    if raw.empty:

        raise RuntimeError(
            "No recent pollutant observations "
            "were returned for the matched stations."
        )

    # --------------------------------------------------------
    # Convert timestamps to Delhi local time
    # --------------------------------------------------------

    raw["timestamp"] = (
        pd.to_datetime(
            raw["timestamp"],
            utc=True,
        )
        .dt.tz_convert(
            "Asia/Kolkata"
        )
        .dt.tz_localize(None)
    )

    # --------------------------------------------------------
    # If multiple latest records exist for the same
    # station/pollutant, retain the newest one.
    # --------------------------------------------------------

    raw = raw.sort_values(
        "timestamp"
    )

    raw = raw.drop_duplicates(
        subset=[
            "location_id",
            "parameter",
        ],
        keep="last",
    )

    # --------------------------------------------------------
    # Attach AirGrid station identity
    # --------------------------------------------------------

    wide = raw.merge(
        matches[
            [
                "airgrid_station",
                "location_id",
                "latitude",
                "longitude",
                "openaq_station",
            ]
        ],
        on="location_id",
        how="inner",
    )

    # --------------------------------------------------------
    # Pivot pollutants
    # --------------------------------------------------------

    wide = (
        wide
        .pivot_table(
            index=[
                "airgrid_station",
                "location_id",
                "latitude",
                "longitude",
                "openaq_station",
                "timestamp",
            ],
            columns="parameter",
            values="value",
            aggfunc="mean",
        )
        .reset_index()
    )

    wide.columns.name = None

    for pollutant in POLLUTANTS:

        if pollutant not in wide.columns:

            wide[pollutant] = np.nan

    # --------------------------------------------------------
    # AQI
    # --------------------------------------------------------

    wide["true_aqi"] = wide.apply(
        aqi_from_row,
        axis=1,
    )

    # --------------------------------------------------------
    # Rename station back to existing project convention
    # --------------------------------------------------------

    wide = wide.rename(
        columns={
            "airgrid_station": "station"
        }
    )

    wide = wide[
        [
            "station",
            "latitude",
            "longitude",
            "timestamp",
            "pm25",
            "pm10",
            "no2",
            "so2",
            "o3",
            "co",
            "true_aqi",
        ]
    ]

    wide = wide.sort_values(
        [
            "timestamp",
            "station",
        ]
    ).reset_index(
        drop=True
    )

    return wide


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "\n" + "=" * 70
    )

    print(
        "AirGrid — LIVE STATION DATA FETCH"
    )

    print(
        "=" * 70
    )

    print(
        "\nIMPORTANT:"
    )

    print(
        "This fetch uses parameter-level OpenAQ latest endpoints."
    )

    print(
        "It does NOT make one request per sensor."
    )

    # --------------------------------------------------------
    # API KEY
    # --------------------------------------------------------

    if not OPENAQ_API_KEY:

        raise RuntimeError(
            "OPENAQ_API_KEY is not set.\n"
            "Export your OpenAQ API key before running."
        )

    # --------------------------------------------------------
    # Load AirGrid stations
    # --------------------------------------------------------

    airgrid_stations = load_stations()

    print(
        f"\nExisting AirGrid stations: "
        f"{len(airgrid_stations)}"
    )

    # --------------------------------------------------------
    # [1] OpenAQ locations
    # --------------------------------------------------------

    locations = fetch_locations()

    # --------------------------------------------------------
    # Match stations
    # --------------------------------------------------------

    matches = match_stations(
        airgrid_stations,
        locations,
    )

    # --------------------------------------------------------
    # Time window
    # --------------------------------------------------------

    now = datetime.now(
        timezone.utc
    )

    datetime_min = (
        now
        - timedelta(
            hours=LOOKBACK_HOURS
        )
    )

    print(
        "\nRecent-data threshold:"
    )

    print(
        f"  {datetime_min.isoformat()}"
    )

    print(
        f"  → {now.isoformat()}"
    )

    # --------------------------------------------------------
    # [2] Fetch latest values
    # --------------------------------------------------------

    print(
        "\n[3/4] Fetching latest pollutant observations..."
    )

    all_raw = []

    for parameter_name, parameter_id in PARAMETER_IDS.items():

        result = fetch_parameter_latest(
            parameter_name=parameter_name,
            parameter_id=parameter_id,
            location_ids=matches[
                "location_id"
            ].tolist(),
            datetime_min=datetime_min,
        )

        if not result.empty:
            all_raw.append(
                result
            )

    if not all_raw:

        raise RuntimeError(
            "\nNo recent pollutant observations "
            "were obtained.\n\n"
            "Possible causes:\n"
            "  - OpenAQ has no recent data for these stations\n"
            "  - API rate limiting\n"
            "  - station mappings are stale\n"
            "  - the stations are temporarily offline"
        )

    raw = pd.concat(
        all_raw,
        ignore_index=True,
    )

    print(
        f"\n  → total recent pollutant observations: "
        f"{len(raw):,}"
    )

    # --------------------------------------------------------
    # Build final station-hour table
    # --------------------------------------------------------

    print(
        "\n[4/4] Building live station AQI table..."
    )

    wide = build_station_table(
        matches,
        raw,
    )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    wide.to_csv(
        OUTPUT_PATH,
        index=False,
    )

    print(
        f"\n  → saved: {OUTPUT_PATH}"
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print(
        "\n" + "=" * 70
    )

    print(
        "LIVE DATA SUMMARY"
    )

    print(
        "=" * 70
    )

    print(
        f"Matched AirGrid stations : "
        f"{matches['airgrid_station'].nunique()}"
    )

    print(
        f"Stations with observations: "
        f"{wide['station'].nunique()}"
    )

    print(
        f"Station rows              : "
        f"{len(wide):,}"
    )

    print(
        f"Rows with AQI             : "
        f"{wide['true_aqi'].notna().sum():,}"
    )

    if not wide.empty:

        print(
            f"Latest observation        : "
            f"{wide['timestamp'].max()}"
        )

        print(
            f"Earliest retained         : "
            f"{wide['timestamp'].min()}"
        )

        print(
            "\nAQI statistics:"
        )

        print(
            wide["true_aqi"]
            .describe()
            .to_string()
        )

        print(
            "\nLatest station coverage:"
        )

        latest = wide[
            wide["timestamp"]
            == wide["timestamp"].max()
        ]

        print(
            f"  Stations: "
            f"{latest['station'].nunique()}"
        )

        print(
            "\nLatest station AQI:"
        )

        print(
            latest[
                [
                    "station",
                    "timestamp",
                    "true_aqi",
                ]
            ]
            .sort_values(
                "true_aqi",
                ascending=False,
            )
            .head(15)
            .to_string(
                index=False
            )
        )

    print(
        "\n" + "=" * 70
    )

    print(
        "LIVE FETCH COMPLETE"
    )

    print(
        "=" * 70
    )


if __name__ == "__main__":
    main()