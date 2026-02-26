# NWA MTG Finder — Deploy to Render (Free)

## What you need
- A free GitHub account (github.com)
- A free Render account (render.com)

---

## Step 1 — Put the code on GitHub

1. Go to **github.com** and sign in
2. Click **+** (top right) → **New repository**
3. Name it `nwa-mtg-finder`, set to **Public**, click **Create repository**
4. On the next screen click **uploading an existing file**

   **First upload (root files)** — drag these 4 files in:
   - `app.py`
   - `requirements.txt`
   - `render.yaml`
   - `Dockerfile`

   Click **Commit changes**.

   **Second upload (static folder)** — GitHub needs a trick for subfolders:
   - Click **Add file** → **Create new file**
   - In the filename box type: `static/index.html` (the slash creates the folder)
   - Open your local `static/index.html` in a text editor, select all, paste into the editor
   - Click **Commit changes**

---

## Step 2 — Deploy via Blueprint (CRITICAL — do not use "New Web Service")

> If you use **New Web Service**, Render ignores the Dockerfile and falls back to Python mode — that's the error you've been seeing. You MUST use **Blueprint** instead.

1. Go to **render.com** and sign in
2. Click **New +** → **Blueprint**
3. Connect your GitHub account and select `nwa-mtg-finder`
4. Render reads `render.yaml` and shows the service with **Docker** runtime
5. Click **Apply**
6. First build takes **5–10 minutes** (downloading the Playwright + Chromium Docker image)

---

## Step 3 — Add to iPhone home screen

1. Visit your Render URL (e.g. `https://nwa-mtg-finder.onrender.com`) in Safari
2. Tap **Share** → **Add to Home Screen**

---

## Notes

- Free tier sleeps after 15 min idle — first search of the day takes ~30s to wake up
- Enter your Moxfield username below the search bar to check your own decks
- Only public Moxfield decks are searchable (private decks require login)
- Auto-redeploys on every GitHub push

## Troubleshooting

| Problem | Fix |
|---|---|
| Still getting Python build error | Delete the service, use **New Blueprint** not New Web Service |
| `static/index.html` not found | Check the file is at `static/index.html` in the repo, not the root |
| Searches time out on first try | Wait 60s after cold start and retry |
