# Deploying BusTrack for a shareable link (Render, free tier)

This gets you a public `https://bustrack-XXXX.onrender.com` link to attach as proof of
the prototype. It's free, but Render's free web services sleep after 15 minutes with no
traffic and take 30-60 seconds to wake on the next visit - fine for someone checking a
project link, not for constant use. It's also **ephemeral**: `bustrack.db` (accounts,
sessions) resets whenever the service restarts or you push a new deploy, since Render's
free tier gives no persistent disk. `geocode_seed.json` is what makes stop locations
survive that reset - it isn't a login database, so it's safe to commit and does not
suffer the same wipe.

## 1. Geocode locally, once

You still need working local geocoding for this one-time step (see the main README).

```bat
pip install -r requirements.txt
python -m uvicorn main:app --reload
```

Wait for `network ready: X/684 stops geocoded`, then stop the server (Ctrl+C) and run:

```bat
python export_geocode.py
```

This writes `geocode_seed.json`. Commit it - the hosted copy reads stop locations from
this file instead of calling Google's Geocoding API, so it starts in seconds and never
needs your server-side key online.

## 2. Push to GitHub

```bat
git init
git add .
git commit -m "BusTrack"
```

Create a repo on GitHub and push to it. `.gitignore` already excludes `.env` and
`bustrack.db` - check `git status` before your first commit to be sure neither is staged.

## 3. Create the Render service

1. Go to https://dashboard.render.com → **New** → **Blueprint**, and point it at your
   GitHub repo. Render reads `render.yaml` and creates the service.
   (No Blueprint option? **New** → **Web Service** instead, same repo, and it will
   detect `requirements.txt` and use the same build/start commands as `render.yaml`.)
2. In the service's **Environment** tab, set:
   - `GOOGLE_MAPS_BROWSER_KEY` - a *separate* Google Maps key, restricted in Google
     Cloud Console to the **Maps JavaScript API** and to the HTTP referrer
     `https://bustrack-XXXX.onrender.com/*` (you'll see your actual URL after the
     first deploy - add it then and redeploy, or add it as a guess and fix it after).
   - `DEMO_EMAIL` / `DEMO_PASSWORD` (optional) - a login that's recreated every time
     the free server wakes up, so it survives the database resets above. Skip this and
     just sign up fresh each time if you'd rather not have a standing demo account.
   - `APP_BASE_URL` - your Render URL, e.g. `https://bustrack-XXXX.onrender.com`
     (used in password-reset links).
3. Deploy. Watch the logs for `network ready: .../684 stops geocoded` using the seed
   file (should be instant, no Google calls), then open the URL.

## 4. Sanity-check before sharing the link

- Visit `/healthz` - should return `{"ok": true}`.
- Sign up or log in with your demo account, pick a route, confirm the map and bus
  markers render (positions are simulated - see the main README).
- The first visit after idle time will be slow (cold start) - mention that if you're
  sharing the link live, e.g. in a viva.

## Notes specific to the hosted copy

- **Accounts reset on redeploy/restart.** Fine for a portfolio link; not fine if you
  need to preserve real user data - that needs a paid plan with a persistent disk, or
  moving to a managed database (Render Postgres free tier is a reasonable next step).
- **Password-reset emails**: leave `SMTP_HOST` unset and the reset link is only printed
  in Render's logs (Dashboard → your service → Logs), not emailed - fine for a demo,
  not for real users.
- **Cold starts**: the first request after 15 idle minutes takes 30-60 seconds. Later
  requests are fast until it goes idle again.
