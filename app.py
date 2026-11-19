"""Streamlit web app for the Farm Advisor.

Run locally:   streamlit run app.py
Deploy:        push this folder to GitHub, then create an app on
               share.streamlit.io with app.py as the main file.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from farm_advisor import get_advice, parse_station_csv

st.set_page_config(page_title="Farm Advisor", page_icon="🌱", layout="centered")

LOCATIONS = {
    "Yaoundé (Centre)": (3.87, 11.52),
    "Douala (Littoral)": (4.05, 9.77),
    "Bafoussam (West)": (5.48, 10.42),
    "Bamenda (North-West)": (5.96, 10.15),
    "Buea (South-West)": (4.16, 9.24),
    "Bertoua (East)": (4.58, 13.68),
    "Ebolowa (South)": (2.91, 11.15),
    "Ngaoundéré (Adamawa)": (7.32, 13.58),
    "Garoua (North)": (9.30, 13.39),
    "Maroua (Far North)": (10.59, 14.32),
    "Other (enter coordinates)": None,
}

DECISION_STYLE = {
    "PLANT NOW": st.success,
    "PREPARE": st.info,
    "WAIT": st.warning,
}


@st.cache_data(ttl=3600, show_spinner=False)
def cached_advice(lat: float, lon: float, station_text: str | None):
    station = parse_station_csv(station_text) if station_text else None
    return get_advice(lat, lon, station)


st.title("🌱 Farm Advisor")
st.caption("Should you plant now, and what? Based on live rainfall and "
           "temperature data for your area.")

choice = st.selectbox("Where is your farm?", list(LOCATIONS))
if LOCATIONS[choice] is None:
    c1, c2 = st.columns(2)
    lat = c1.number_input("Latitude", -90.0, 90.0, 3.87, format="%.4f")
    lon = c2.number_input("Longitude", -180.0, 180.0, 11.52, format="%.4f")
    st.caption("Tip: in Google Maps, right-click your farm to copy its coordinates.")
else:
    lat, lon = LOCATIONS[choice]

with st.expander("I have my own weather station"):
    upload = st.file_uploader("Upload the last 30 days as CSV "
                              "(columns: date, rain_mm, tmax, tmin)", type="csv")
station_text = upload.getvalue().decode("utf-8") if upload else None

try:
    with st.spinner("Reading the latest weather data…"):
        advice, past, future = cached_advice(lat, lon, station_text)
except Exception as exc:
    st.error(f"Could not get weather data right now. Please try again later.\n\n{exc}")
    st.stop()

# Decision
show = DECISION_STYLE.get(advice.decision, st.error)
show(f"### {advice.decision}\n" + "\n".join(f"- {r}" for r in advice.reasons))

# Key numbers
ind = advice.indicators
m1, m2, m3 = st.columns(3)
m1.metric("Rain, last 10 days", f"{ind['rain_last_10_days_mm']:.0f} mm")
m2.metric("Rain, next 7 days", f"{ind['rain_next_7_days_mm']:.0f} mm")
m3.metric("Dry days ahead (max)", ind["longest_dry_spell_next_14_days"])

# Rainfall chart: observed vs forecast
rain = pd.DataFrame({
    "date": past.dates + future.dates,
    "Observed": past.rain + [None] * len(future.rain),
    "Forecast": [None] * len(past.rain) + future.rain,
}).set_index("date")
st.subheader("Daily rainfall (mm)")
st.bar_chart(rain, color=["#2f7d4f", "#7fb3d5"])

# Crop ranking
st.subheader("Best crops for this season")
good = [r for r in advice.ranking if r["score"] > 0]
if not good:
    st.write("No rain-fed crop is suitable right now. Plant only with irrigation.")
for r in good[:6]:
    st.markdown(f"**{r['crop']}** — {r['score']}/100 · {r['cycle_days']} days")
    st.progress(r["score"] / 100)
    st.caption(r["why"])

avoid = [r["crop"] for r in advice.ranking if r["score"] < 20]
if avoid:
    st.markdown("**Avoid for now:** " + ", ".join(avoid))

with st.expander("All weather indicators"):
    st.table(pd.DataFrame(
        {"value": [str(v) for v in ind.values()]},
        index=[k.replace("_", " ") for k in ind]))

st.caption("Weather data: Open-Meteo. Advice is a guide only; combine it with "
           "local knowledge and your agricultural extension officer.")
