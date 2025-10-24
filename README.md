# Timecard Pay Calculator (Streamlit)

Paste your Delta-style Monthly Time Data (single line or multi-line) and get accurate total pay hours.

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud
1. Push these files to a new GitHub repo.
2. Go to https://streamlit.io/cloud → "Deploy an app".
3. Select your repo, set **Main file path** to `app.py`, Python version 3.10–3.12.
4. Click **Deploy**.

> The app does not store any data. All parsing happens in memory in your browser session.

## Files
- `app.py` — Streamlit app
- `requirements.txt` — pip deps
- `.streamlit/config.toml` — theme & server settings
- `README.md` — this file
