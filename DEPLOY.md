# NWA MTG Finder — Deploy to Render (Free)

## What you need
- A free GitHub account (github.com)
- A free Render account (render.com)

---

## Step 1 — Put the code on GitHub

1. Go to **github.com** and sign in
2. Click **+** → **New repository**
3. Name it `nwa-mtg-finder`, set to **Public**, click **Create repository**
4. On the next page, click **uploading an existing file**
5. Drag and drop ALL files from this folder into the upload area:
   - `app.py`
   - `requirements.txt`
   - `render.yaml`
   - `static/index.html`  ← make sure to upload this inside a `static/` folder
6. Click **Commit changes**

---

## Step 2 — Deploy on Render

1. Go to **render.com** and sign up (free)
2. Click **New +** → **Web Service**
3. Connect your GitHub account and select the `nwa-mtg-finder` repo
4. Render will auto-detect the `render.yaml` — if not, use these settings:
   - **Build Command:** `pip install -r requirements.txt && python -m playwright install chromium --with-deps`
   - **Start Command:** `gunicorn app:app --workers 1 --timeout 60 --bind 0.0.0.0:$PORT`
   - **Instance Type:** Free
5. Click **Create Web Service**
6. Wait 3–5 minutes for the first deploy to finish (it installs Playwright + Chromium)

---

## Step 3 — Use it on your iPhone

1. Once deployed, Render gives you a URL like `https://nwa-mtg-finder.onrender.com`
2. Open that URL in **Safari** on your iPhone
3. Tap the **Share** button → **Add to Home Screen**
4. Now it works like an app icon!

---

## Notes

- **First search** after a period of inactivity may take 20–30 seconds (Render free tier sleeps after 15 min idle)
- **Subsequent searches** are fast
- All 5 stores are searched: Final Boss, Gear Gaming ×2, Chaos Games NWA, Games Xxplosion
- Chaos Games & Games Xxplosion use Playwright (real browser) to get around JS rendering
- Card info, legality, and prices come from Scryfall's free API
- EDHREC and Moxfield links are generated automatically per card
