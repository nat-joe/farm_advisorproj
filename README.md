# Farm Advisor

Advises farmers whether to plant now and which crops suit the season, using
live rainfall and temperature data.

| File | What it is |
|---|---|
| `farm_advisor.py` | The advisor logic. Also works from the command line. |
| `app.py` | The Streamlit web app. |
| `requirements.txt` | Packages the web app needs. |

## Run on your own computer

```bash
pip install -r requirements.txt
streamlit run app.py
```

The app opens at http://localhost:8501.

Command-line only, no install needed:

```bash
python3 farm_advisor.py --lat 3.87 --lon 11.52
```

## Put it online (Streamlit Community Cloud, free)

1. Create a free account at https://github.com.
2. Create a new **public** repository, for example `farm-advisor`.
3. Click **Add file → Upload files**, drag in `app.py`, `farm_advisor.py`,
   `requirements.txt` and this `README.md`, then click **Commit changes**.
4. Go to https://share.streamlit.io and sign in with GitHub.
5. Click **Create app → Deploy a public app from GitHub**.
6. Choose the repository, branch `main`, and main file `app.py`.
   Optionally pick a custom address such as `farm-advisor-cm.streamlit.app`.
7. Click **Deploy**. After a few minutes the app is live, and anyone with
   the link can use it from a phone browser.

To update the app later, change the files on GitHub; Streamlit Cloud
redeploys automatically.

No API keys or secrets are needed: weather data comes from Open-Meteo,
which is free for non-commercial use. For commercial use, see
https://open-meteo.com/en/pricing.

## Using your own weather station

On the app page, open **I have my own weather station** and upload a CSV of
the last 30 days with these columns:

```
date,rain_mm,tmax,tmin
2026-11-01,12.5,31,22
2026-11-02,0,32,21
```

## Notes

- Free Streamlit apps go to sleep after a few days without visitors; the
  first visitor clicks a button to wake them up (about 30 seconds).
- Results are cached for one hour, so many farmers can use it at once
  without overloading the weather service.
- Crop requirements are approximate general values. Adjust the `CROPS` list
  in `farm_advisor.py` with local agronomy data for better advice.
