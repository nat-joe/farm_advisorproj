#!/usr/bin/env python3
"""
Farm Advisor AI
===============
Advises farmers on WHETHER to plant now and WHAT to plant, using rainfall
and temperature data from a weather station in real time.

How it reasons
--------------
1. Reads the live station data: the last 30 days of observations plus the
   16-day forecast (rain, temperature, evapotranspiration, soil moisture).
2. Detects the state of the season with standard agronomic rules:
     - rainy-season onset: >= 20 mm in 3 consecutive days,
     - false start risk: a dry spell of 7+ days forecast after the onset,
     - waterlogging risk: very heavy rain forecast.
3. Estimates how much rain each crop would receive over its whole growing
   cycle: the forecast for the first 16 days, then the average of the last
   5 years at this location for the remaining days (climatology).
4. Scores every crop with fuzzy-logic suitability curves (FAO ECOCROP
   style) for rainfall and temperature, and ranks them with reasons.

Data sources
------------
- Default: Open-Meteo (free, no API key). Uses the station/model nearest to
  the coordinates you give.
- Your own meteorological station: pass --station-csv with a file path or URL
  that serves CSV with columns: date,rain_mm,tmax,tmin  (one row per day,
  newest rows are today's readings). Forecast and climatology still come
  from Open-Meteo for the same coordinates.

Usage
-----
    python farm_advisor.py --lat 3.87 --lon 11.52            # Yaoundé
    python farm_advisor.py --lat 9.30 --lon 13.39 --json     # Garoua, JSON out
    python farm_advisor.py --lat 3.87 --lon 11.52 --watch 60 # refresh hourly
    python farm_advisor.py --lat 3.87 --lon 11.52 --station-csv station.csv

Only the Python standard library is needed (Python 3.9+).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from statistics import mean

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
CLIMATE_YEARS = 5


# --------------------------------------------------------------------------
# Crop knowledge base (approximate FAO ECOCROP ranges, per growing cycle)
#   rain: (absolute min, optimal low, optimal high, absolute max) in mm
#   temp: (absolute min, optimal low, optimal high, absolute max) in °C
# --------------------------------------------------------------------------
@dataclass
class Crop:
    name: str
    cycle_days: int
    rain: tuple[float, float, float, float]
    temp: tuple[float, float, float, float]
    drought_tolerant: bool = False
    note: str = ""


CROPS = [
    Crop("Maize", 110, (400, 500, 800, 1200), (10, 18, 32, 40),
         note="Needs steady rain at flowering (weeks 7-10)."),
    Crop("Sorghum", 110, (300, 450, 650, 1000), (15, 25, 32, 40), True,
         note="Good choice when rains are uncertain."),
    Crop("Pearl millet", 90, (200, 350, 600, 900), (18, 25, 35, 42), True,
         note="Most drought-tolerant cereal; suited to the far north."),
    Crop("Groundnut", 110, (350, 500, 700, 1000), (18, 24, 32, 38),
         note="Needs a drier spell at harvest to avoid pod rot."),
    Crop("Cowpea", 80, (250, 350, 600, 900), (18, 22, 32, 38), True,
         note="Short cycle; fits the tail end of a season."),
    Crop("Common beans", 90, (300, 350, 500, 700), (10, 16, 25, 30),
         note="Prefers cool highlands; dislikes heavy rain."),
    Crop("Soybean", 110, (400, 500, 750, 1000), (15, 20, 30, 35)),
    Crop("Upland rice", 120, (700, 900, 1500, 2500), (18, 22, 32, 38),
         note="Only where rain is abundant and well spread."),
    Crop("Cassava", 270, (600, 1000, 1500, 2500), (15, 22, 32, 38), True,
         note="Once established it survives dry spells well."),
    Crop("Cocoyam", 240, (1000, 1400, 2000, 3000), (18, 21, 30, 35),
         note="Needs a long wet season and moist soil."),
    Crop("Sweet potato", 120, (450, 600, 1000, 1400), (15, 21, 30, 35)),
    Crop("Tomato", 100, (400, 500, 700, 900), (12, 18, 28, 33),
         note="Heavy rain raises blight risk; stake and spray."),
    Crop("Irish potato", 100, (350, 500, 700, 900), (7, 15, 22, 28),
         note="Highlands only (cool nights)."),
]


def suitability(x: float, a: float, b: float, c: float, d: float) -> float:
    """Trapezoid fuzzy membership: 0 outside [a, d], 1 inside [b, c]."""
    if x <= a or x >= d:
        return 0.0
    if b <= x <= c:
        return 1.0
    if x < b:
        return (x - a) / (b - a)
    return (d - x) / (d - c)


# --------------------------------------------------------------------------
# Data fetching
# --------------------------------------------------------------------------
def _get_json(url: str, params: dict) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": "farm-advisor/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _years_ago(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 29 February
        return d.replace(year=d.year - years, day=28)


@dataclass
class DailyWeather:
    dates: list[date]
    rain: list[float]
    tmax: list[float]
    tmin: list[float]
    et0: list[float]


def fetch_live(lat: float, lon: float) -> tuple[DailyWeather, DailyWeather, float | None]:
    """Return (past 30 days, next 16 days, current topsoil moisture m3/m3)."""
    data = _get_json(FORECAST_URL, {
        "latitude": lat, "longitude": lon, "timezone": "auto",
        "past_days": 30, "forecast_days": 16,
        "daily": "precipitation_sum,temperature_2m_max,temperature_2m_min,"
                 "et0_fao_evapotranspiration",
        "current": "soil_moisture_3_to_9cm",
    })
    d = data["daily"]
    dates = [date.fromisoformat(x) for x in d["time"]]
    today = date.fromisoformat(data["current"]["time"][:10])
    split = dates.index(today)

    def part(lo, hi):
        clean = lambda xs: [0.0 if v is None else float(v) for v in xs[lo:hi]]
        return DailyWeather(dates[lo:hi], clean(d["precipitation_sum"]),
                            clean(d["temperature_2m_max"]),
                            clean(d["temperature_2m_min"]),
                            clean(d["et0_fao_evapotranspiration"]))

    soil = data.get("current", {}).get("soil_moisture_3_to_9cm")
    return part(0, split), part(split, len(dates)), soil


def load_station_csv(source: str, days: int = 30) -> DailyWeather:
    """Read observations from your own station (file path or http(s) URL)."""
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=30) as resp:
            text = resp.read().decode("utf-8")
    else:
        with open(source, encoding="utf-8") as fh:
            text = fh.read()
    return parse_station_csv(text, days)


def parse_station_csv(text: str, days: int = 30) -> DailyWeather:
    """Parse station CSV text with columns date,rain_mm,tmax,tmin."""
    rows =sorted(csv.DictReader(io.StringIO(text)), key=lambda r: r["date"])[-days:]
    tmax = [float(r["tmax"]) for r in rows]
    tmin = [float(r["tmin"]) for r in rows]
    # Hargreaves estimate of ET0 when the station has no evapotranspiration.
    et0 = [max(0.0, 0.0023 * 15 * ((a + b) / 2 + 17.8) * max(a - b, 0) ** 0.5)
           for a, b in zip(tmax, tmin)]
    return DailyWeather([date.fromisoformat(r["date"]) for r in rows],
                        [float(r["rain_mm"] or 0) for r in rows], tmax, tmin, et0)


def fetch_climatology(lat: float, lon: float, today: date, horizon: int):
    """Average daily rain and mean temperature for each of the next `horizon`
    days, computed from the same calendar window in the last CLIMATE_YEARS."""
    start = _years_ago(today, CLIMATE_YEARS)
    end = today - timedelta(days=7)  # archive lags a few days behind
    data = _get_json(ARCHIVE_URL, {
        "latitude": lat, "longitude": lon, "timezone": "auto",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "precipitation_sum,temperature_2m_mean",
    })
    d = data["daily"]
    index = {date.fromisoformat(t): i for i, t in enumerate(d["time"])}
    rain, temp = [], []
    for k in range(horizon):
        r_vals, t_vals = [], []
        for y in range(1, CLIMATE_YEARS + 1):
            i = index.get(_years_ago(today, y) + timedelta(days=k))
            if i is not None and d["precipitation_sum"][i] is not None:
                r_vals.append(d["precipitation_sum"][i])
                t_vals.append(d["temperature_2m_mean"][i])
        rain.append(mean(r_vals) if r_vals else 0.0)
        temp.append(mean(t_vals) if t_vals else 25.0)
    return rain, temp


# --------------------------------------------------------------------------
# Reasoning
# --------------------------------------------------------------------------
def longest_dry_spell(rain: list[float], threshold: float = 1.0) -> int:
    best = run = 0
    for r in rain:
        run = run + 1 if r < threshold else 0
        best = max(best, run)
    return best


def detect_onset(rain: list[float]) -> bool:
    """Onset rule: >= 20 mm over any 3 consecutive days."""
    return any(sum(rain[i:i + 3]) >= 20 for i in range(len(rain) - 2))


@dataclass
class Advice:
    decision: str
    reasons: list[str]
    indicators: dict
    ranking: list[dict]


def advise(past: DailyWeather, future: DailyWeather, soil: float | None,
           clim_rain: list[float], clim_temp: list[float]) -> Advice:
    rain_30 = sum(past.rain)
    rain_10 = sum(past.rain[-10:])
    rain_next_7 = sum(future.rain[:7])
    rain_next_16 = sum(future.rain)
    dry_next_14 = longest_dry_spell(future.rain[:14])
    balance_30 = rain_30 - sum(past.et0)
    max_3day_fc = max(sum(future.rain[i:i + 3]) for i in range(len(future.rain) - 2))
    onset_recent = detect_onset(past.rain[-10:])
    onset_soon = detect_onset(future.rain[:7])

    indicators = {
        "rain_last_30_days_mm": round(rain_30, 1),
        "rain_last_10_days_mm": round(rain_10, 1),
        "rain_next_7_days_mm": round(rain_next_7, 1),
        "rain_next_16_days_mm": round(rain_next_16, 1),
        "water_balance_30_days_mm": round(balance_30, 1),
        "longest_dry_spell_next_14_days": dry_next_14,
        "heaviest_3_day_rain_forecast_mm": round(max_3day_fc, 1),
        "soil_moisture_3_9cm": soil,
        "mean_temp_next_16_days_c": round(mean(
            (a + b) / 2 for a, b in zip(future.tmax, future.tmin)), 1),
    }

    reasons: list[str] = []
    soil_ok = soil is None or soil >= 0.20
    if max_3day_fc >= 120:
        decision = "WAIT"
        reasons.append(f"Very heavy rain forecast ({max_3day_fc:.0f} mm in 3 days): "
                       "seeds may rot or wash away. Plant after it passes.")
    elif onset_recent and soil_ok and dry_next_14 < 7:
        decision = "PLANT NOW"
        reasons.append(f"The rains have started ({rain_10:.0f} mm in the last 10 days) "
                       "and no long dry spell is forecast.")
    elif onset_recent and dry_next_14 >= 7:
        decision = "WAIT"
        reasons.append(f"Rain has started but a {dry_next_14}-day dry spell is "
                       "forecast: risk of a false start. Seedlings could die.")
    elif onset_soon or rain_next_7 >= 25:
        decision = "PREPARE"
        reasons.append(f"About {rain_next_7:.0f} mm is expected in the next 7 days. "
                       "Prepare land and seed now; sow once the soil is moist "
                       "to a hand's depth.")
    else:
        decision = "DO NOT PLANT (rain-fed)"
        reasons.append(f"Too little rain recently ({rain_10:.0f} mm in 10 days) and "
                       f"forecast ({rain_next_16:.0f} mm in 16 days). "
                       "Plant only with irrigation.")
    if soil is not None and soil < 0.20 and decision != "DO NOT PLANT (rain-fed)":
        reasons.append(f"Topsoil is still fairly dry ({soil:.2f} m³/m³).")
    if balance_30 < -50:
        reasons.append(f"Evaporation exceeded rain by {-balance_30:.0f} mm over 30 days.")

    # Crop ranking: expected rain over each crop's cycle.
    n_fc = len(future.rain)
    fc_temp = [(a + b) / 2 for a, b in zip(future.tmax, future.tmin)]
    ranking = []
    for crop in CROPS:
        days = crop.cycle_days
        exp_rain = sum(future.rain) + sum(clim_rain[n_fc:days])
        exp_temp = mean(fc_temp + clim_temp[n_fc:days])
        s_rain = suitability(exp_rain, *crop.rain)
        s_temp = suitability(exp_temp, *crop.temp)
        score = s_rain * s_temp
        # Early-season risk: favour drought-tolerant crops if rains are shaky.
        if dry_next_14 >= 7 and crop.drought_tolerant:
            score = min(1.0, score * 1.15)
        why = []
        lo, hi = crop.rain[1], crop.rain[2]
        if exp_rain < lo:
            why.append(f"expected {exp_rain:.0f} mm is below its ideal {lo:.0f}-{hi:.0f} mm")
        elif exp_rain > hi:
            why.append(f"expected {exp_rain:.0f} mm is above its ideal {lo:.0f}-{hi:.0f} mm")
        else:
            why.append(f"expected {exp_rain:.0f} mm fits its ideal {lo:.0f}-{hi:.0f} mm")
        if s_temp < 1:
            why.append(f"average {exp_temp:.0f} °C is outside its ideal "
                       f"{crop.temp[1]:.0f}-{crop.temp[2]:.0f} °C")
        if crop.note:
            why.append(crop.note)
        ranking.append({"crop": crop.name, "score": round(score * 100),
                        "cycle_days": days, "expected_rain_mm": round(exp_rain),
                        "why": "; ".join(why)})
    ranking.sort(key=lambda r: r["score"], reverse=True)
    return Advice(decision, reasons, indicators, ranking)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def render(advice: Advice, lat: float, lon: float, source: str) -> str:
    out = [f"FARM ADVISOR  ({lat:.2f}, {lon:.2f})  data: {source}  "
           f"{date.today().isoformat()}", "=" * 72,
           f"Decision: {advice.decision}"]
    out += [f"  - {r}" for r in advice.reasons]
    out += ["", "Weather indicators:"]
    out += [f"  {k.replace('_', ' '):38s} {v}" for k, v in advice.indicators.items()]
    out += ["", "Best crops for this season (score out of 100):"]
    for r in advice.ranking[:6]:
        if r["score"] == 0:
            break
        out.append(f"  {r['score']:3d}  {r['crop']:<14s} ({r['cycle_days']} days) "
                   f"- {r['why']}")
    poor = [r["crop"] for r in advice.ranking if r["score"] < 20]
    if poor:
        out += ["", "Avoid: " + ", ".join(poor)]
    out += ["", "Advice is a guide only; combine it with local knowledge and "
                "your extension officer's recommendations."]
    return "\n".join(out)


def get_advice(lat: float, lon: float, station: DailyWeather | None = None):
    """Fetch live data and return (advice, past weather, forecast)."""
    past, future, soil = fetch_live(lat, lon)
    if station is not None:
        past = station
    horizon = max(c.cycle_days for c in CROPS)
    clim_rain, clim_temp = fetch_climatology(lat, lon, future.dates[0], horizon)
    return advise(past, future, soil, clim_rain, clim_temp), past, future


def run_once(args) -> None:
    station, source = None, "Open-Meteo"
    if args.station_csv:
        station = load_station_csv(args.station_csv)
        source = f"station ({args.station_csv}) + Open-Meteo forecast"
    advice, _, _ = get_advice(args.lat, args.lon, station)
    if args.json:
        print(json.dumps(asdict(advice), indent=2))
    else:
        print(render(advice, args.lat, args.lon, source))


def main() -> None:
    p = argparse.ArgumentParser(description="Rainfall-based planting advisor")
    p.add_argument("--lat", type=float, required=True, help="latitude of the farm")
    p.add_argument("--lon", type=float, required=True, help="longitude of the farm")
    p.add_argument("--station-csv", help="path or URL of your station's CSV "
                   "(columns: date,rain_mm,tmax,tmin)")
    p.add_argument("--json", action="store_true", help="print JSON instead of text")
    p.add_argument("--watch", type=int, metavar="MINUTES",
                   help="keep running and refresh every N minutes")
    args = p.parse_args()

    while True:
        try:
            run_once(args)
        except Exception as exc:  # network hiccups should not kill --watch mode
            print(f"Could not update advice: {exc}")
            if not args.watch:
                raise SystemExit(1)
        if not args.watch:
            break
        time.sleep(args.watch * 60)
        print()


if __name__ == "__main__":
    main()
