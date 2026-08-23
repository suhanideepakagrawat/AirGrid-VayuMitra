"""
fetch_forecast_weather.py
--------------------------

AirGrid production weather provider.

Provides TWO types of weather:

1. CURRENT WEATHER
   Used for source-time inference features:
       wind_speed
       wind_dir
       temp
       humidity

2. HOURLY FORECAST WEATHER
   Used for target-time inference features:
       target_wind_speed
       target_wind_dir
       target_temp
       target_humidity

Source:
    Open-Meteo forecast API

No API key required.

Outputs:
    data/forecast_weather.csv

The forecast CSV contains:

    timestamp
    target_temp
    target_humidity
    target_wind_speed
    target_wind_dir
    fetched_at
"""

import os
from datetime import datetime, timezone

import requests
import pandas as pd


# ============================================================================
# CITY
# ============================================================================

CITY = {
    "name": "Delhi",
    "lat_center": 28.65,
    "lon_center": 77.10,
}


# ============================================================================
# OPEN-METEO
# ============================================================================

OPENMETEO_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
)


# 120 hours gives a comfortable buffer beyond the 72h model horizon.
FORECAST_HOURS = 120


# ============================================================================
# PATHS
# ============================================================================

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.abspath(__file__)
    )
)

DATA_DIR = os.path.join(
    PROJECT_ROOT,
    "data"
)

os.makedirs(
    DATA_DIR,
    exist_ok=True
)

OUT_PATH = os.path.join(
    DATA_DIR,
    "forecast_weather.csv"
)


# ============================================================================
# CURRENT WEATHER
# ============================================================================

def get_current_weather() -> dict:
    """
    Fetch current weather for Delhi.

    Used for SOURCE-TIME model features.

    Returns:

        timestamp
        wind_speed
        wind_dir
        temp
        humidity

    Units:

        temperature  -> °C
        humidity     -> %
        wind_speed   -> km/h
        wind_dir     -> degrees
    """

    params = {
        "latitude": CITY["lat_center"],
        "longitude": CITY["lon_center"],

        "current": (
            "temperature_2m,"
            "relative_humidity_2m,"
            "wind_speed_10m,"
            "wind_direction_10m"
        ),

        "timezone": "Asia/Kolkata",

        "wind_speed_unit": "kmh",
    }

    response = requests.get(
        OPENMETEO_FORECAST_URL,
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    payload = response.json()

    if "current" not in payload:
        raise RuntimeError(
            "Open-Meteo response missing 'current'."
        )

    current = payload["current"]

    required = [
        "time",
        "temperature_2m",
        "relative_humidity_2m",
        "wind_speed_10m",
        "wind_direction_10m",
    ]

    missing = [
        key
        for key in required
        if key not in current
    ]

    if missing:
        raise RuntimeError(
            "Open-Meteo current weather is missing "
            f"fields: {missing}"
        )

    result = {
        "timestamp": pd.Timestamp(
            current["time"]
        ),

        "wind_speed": float(
            current["wind_speed_10m"]
        ),

        "wind_dir": float(
            current["wind_direction_10m"]
        ),

        "temp": float(
            current["temperature_2m"]
        ),

        "humidity": float(
            current["relative_humidity_2m"]
        ),
    }

    return result


# ============================================================================
# FORECAST WEATHER
# ============================================================================

def get_forecast_weather(
    hours: int = FORECAST_HOURS
) -> pd.DataFrame:
    """
    Fetch hourly forecast weather.

    Used for TARGET-TIME weather features.

    Returns:

        timestamp
        target_temp
        target_humidity
        target_wind_speed
        target_wind_dir
        fetched_at
    """

    params = {
        "latitude": CITY["lat_center"],
        "longitude": CITY["lon_center"],

        "hourly": (
            "temperature_2m,"
            "relative_humidity_2m,"
            "wind_speed_10m,"
            "wind_direction_10m"
        ),

        "timezone": "Asia/Kolkata",

        "forecast_hours": hours,

        "wind_speed_unit": "kmh",
    }

    response = requests.get(
        OPENMETEO_FORECAST_URL,
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    payload = response.json()

    if "hourly" not in payload:
        raise RuntimeError(
            "Open-Meteo response missing 'hourly'."
        )

    hourly = payload["hourly"]

    required = [
        "time",
        "temperature_2m",
        "relative_humidity_2m",
        "wind_speed_10m",
        "wind_direction_10m",
    ]

    missing = [
        key
        for key in required
        if key not in hourly
    ]

    if missing:
        raise RuntimeError(
            "Open-Meteo forecast is missing "
            f"fields: {missing}"
        )

    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                hourly["time"]
            ),

            "target_temp": pd.to_numeric(
                hourly["temperature_2m"],
                errors="coerce",
            ),

            "target_humidity": pd.to_numeric(
                hourly["relative_humidity_2m"],
                errors="coerce",
            ),

            "target_wind_speed": pd.to_numeric(
                hourly["wind_speed_10m"],
                errors="coerce",
            ),

            "target_wind_dir": pd.to_numeric(
                hourly["wind_direction_10m"],
                errors="coerce",
            ),
        }
    )

    df = (
        df
        .sort_values("timestamp")
        .drop_duplicates(
            subset=["timestamp"],
            keep="last",
        )
        .reset_index(drop=True)
    )

    if df.empty:
        raise RuntimeError(
            "Open-Meteo returned zero forecast rows."
        )

    weather_cols = [
        "target_temp",
        "target_humidity",
        "target_wind_speed",
        "target_wind_dir",
    ]

    missing_values = (
        df[weather_cols]
        .isna()
        .sum()
    )

    if missing_values.any():
        print(
            "\n  [WARN] Missing forecast values:"
        )

        print(
            missing_values[
                missing_values > 0
            ].to_string()
        )

    fetched_at = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    df["fetched_at"] = fetched_at

    return df


# ============================================================================
# STALENESS
# ============================================================================

def check_staleness(
    df: pd.DataFrame,
    max_age_hours: float = 6.0,
):
    """
    Check cached forecast age.
    """

    if (
        df.empty
        or "fetched_at" not in df.columns
    ):
        return

    fetched = pd.to_datetime(
        df["fetched_at"].iloc[0],
        utc=True,
        errors="coerce",
    )

    if pd.isna(fetched):
        print(
            "  [WARN] Invalid fetched_at."
        )
        return

    age_hours = (
        datetime.now(timezone.utc)
        - fetched
    ).total_seconds() / 3600

    if age_hours > max_age_hours:

        print(
            f"  [WARN] Forecast weather is "
            f"{age_hours:.1f}h old."
        )

    else:

        print(
            f"  [OK] Forecast weather is "
            f"{age_hours:.1f}h old."
        )


# ============================================================================
# LOAD OR FETCH
# ============================================================================

def load_or_fetch(
    max_age_hours: float = 6.0,
) -> pd.DataFrame:
    """
    Load a valid recent forecast cache.

    Re-fetch if:

        - file does not exist
        - schema is invalid
        - cache is empty
        - fetched_at is invalid
        - cache is stale
    """

    if os.path.exists(OUT_PATH):

        try:

            df = pd.read_csv(
                OUT_PATH,
                parse_dates=["timestamp"],
            )

            required = {
                "timestamp",
                "target_temp",
                "target_humidity",
                "target_wind_speed",
                "target_wind_dir",
                "fetched_at",
            }

            missing = (
                required
                - set(df.columns)
            )

            if missing:

                print(
                    "  [cache invalid] Missing "
                    f"columns: {sorted(missing)}"
                )

            elif df.empty:

                print(
                    "  [cache invalid] Empty cache."
                )

            else:

                fetched = pd.to_datetime(
                    df["fetched_at"].iloc[0],
                    utc=True,
                    errors="coerce",
                )

                if pd.isna(fetched):

                    print(
                        "  [cache invalid] "
                        "Bad fetched_at."
                    )

                else:

                    age_hours = (
                        datetime.now(timezone.utc)
                        - fetched
                    ).total_seconds() / 3600

                    if (
                        0 <= age_hours
                        <= max_age_hours
                    ):

                        print(
                            f"  [cache] Forecast weather "
                            f"loaded ({age_hours:.1f}h old)."
                        )

                        return df

                    print(
                        f"  [stale] Cached forecast "
                        f"is {age_hours:.1f}h old."
                    )

        except Exception as exc:

            print(
                f"  [cache invalid] {exc}"
            )

    print(
        "  [fetch] Downloading fresh "
        "forecast weather ..."
    )

    df = get_forecast_weather()

    df.to_csv(
        OUT_PATH,
        index=False,
    )

    return df


# ============================================================================
# SUMMARY
# ============================================================================

def print_summary(
    df: pd.DataFrame
):

    print(
        f"\n  City          : "
        f"{CITY['name']}"
    )

    print(
        f"  Rows          : "
        f"{len(df)} hours"
    )

    print(
        f"  Window        : "
        f"{df['timestamp'].min()} → "
        f"{df['timestamp'].max()}"
    )

    print(
        f"  Fetched at    : "
        f"{df['fetched_at'].iloc[0]}"
    )

    print("\n  Weather range:")

    print(
        f"    temp         : "
        f"{df['target_temp'].min():.1f}°C – "
        f"{df['target_temp'].max():.1f}°C"
    )

    print(
        f"    humidity     : "
        f"{df['target_humidity'].min():.0f}% – "
        f"{df['target_humidity'].max():.0f}%"
    )

    print(
        f"    wind speed   : "
        f"{df['target_wind_speed'].min():.1f} – "
        f"{df['target_wind_speed'].max():.1f} km/h"
    )

    print(
        f"    wind dir     : "
        f"{df['target_wind_dir'].min():.0f}° – "
        f"{df['target_wind_dir'].max():.0f}°"
    )

    print("\n  First 5 rows:")

    print(
        df[
            [
                "timestamp",
                "target_temp",
                "target_humidity",
                "target_wind_speed",
                "target_wind_dir",
            ]
        ]
        .head(5)
        .to_string(index=False)
    )


# ============================================================================
# MAIN
# ============================================================================

def main():

    print(
        f"\n{'=' * 70}"
    )

    print(
        f"  AirGrid — Weather Provider"
    )

    print(
        f"  City: {CITY['name']}"
    )

    print(
        f"{'=' * 70}\n"
    )

    # ------------------------------------------------------------------
    # CURRENT WEATHER
    # ------------------------------------------------------------------

    print(
        "[1/2] Fetching current weather"
    )

    current = get_current_weather()

    print(
        f"  timestamp : "
        f"{current['timestamp']}"
    )

    print(
        f"  temp      : "
        f"{current['temp']:.1f} °C"
    )

    print(
        f"  humidity  : "
        f"{current['humidity']:.0f} %"
    )

    print(
        f"  wind      : "
        f"{current['wind_speed']:.1f} km/h"
    )

    print(
        f"  direction : "
        f"{current['wind_dir']:.0f}°"
    )

    # ------------------------------------------------------------------
    # FORECAST WEATHER
    # ------------------------------------------------------------------

    print(
        f"\n[2/2] Fetching "
        f"{FORECAST_HOURS}h forecast"
    )

    forecast = get_forecast_weather(
        FORECAST_HOURS
    )

    forecast.to_csv(
        OUT_PATH,
        index=False,
    )

    print_summary(
        forecast
    )

    print(
        f"\n  ✓ Saved: {OUT_PATH}"
    )

    print(
        f"\n{'=' * 70}\n"
    )


if __name__ == "__main__":
    main()