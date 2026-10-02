# BusTrack

Log in → choose a starting point and destination → see which real WBTC/CTC bus routes
run that trip, when they reach you, and follow them live on a Google Map.

## Run locally (one terminal)

```bat
cd bustrack2
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://127.0.0.1:8000 (if `uvicorn` isn't recognised, use `python -m uvicorn main:app --reload`).

**The first startup takes longer than usual** — the server geocodes ~680 real Kolkata bus
stop names through the Google Geocoding API and caches the results in `bustrack.db`.
Later restarts are instant (nothing re-geocodes unless you ask it to — see below).

## Files

```
bustrack2/
  main.py            API, login, real route data, geocoding, simulation, WebSocket
  requirements.txt
  .env               your Google Maps API key (kept out of git by .gitignore)
  static/
    index.html       login screen + planner + Google Map
    app.js
    style.css
```

## Deploying it as a public link

See `DEPLOY.md` for step-by-step Render instructions (a free hosted URL you can share or attach as proof of the prototype).

## Your Google Maps key

Your key is in `.env` (used server-side for geocoding) and in `static/index.html`
(used by the browser to load the map). Both need to be the same key with these APIs
enabled in Google Cloud Console, with billing on:
- **Maps JavaScript API** (draws the map)
- **Geocoding API** (turns stop names into coordinates)

**Before putting this anywhere other than your own machine**, restrict the key by
HTTP referrer (e.g. `127.0.0.1:8000/*`, `localhost:8000/*`, and your real domain if you
deploy it) — Maps JavaScript API keys are always visible in the page's source, so the
referrer restriction is what keeps other sites from using your key and your quota.

## Where the route data comes from — and its limits

- **Route numbers, depots, and stop sequences** for all 47 usable routes (C-11, T-11,
  D1, E-7, and so on) are transcribed from the WBTC/Calcutta Tramways public bus-route
  page (calcuttatramways.com/bus-route). One route, AC-2, is omitted because that page
  lists no stops for it.
- **No official timetable exists to show.** That same page's "1st car", "last car" and
  "frequency" columns are blank for every route — WBTC doesn't publish them. The times
  you see in the app come from the live simulation below, not a real schedule. If you
  find an official timetable source later, replace the frequency/ETA logic in `main.py`
  with real values.
- **Stop coordinates are geocoded, not hand-placed.** Some of the more informal stop
  names ("More", "Xing", "8B Bus Stand") may geocode to the wrong spot or fail outright.
  After the server starts, check `GET /api/geocode-status` (send your session token as
  a Bearer header, e.g. via the browser dev console once logged in) to see how many
  stops resolved and which ones didn't.
- **To fix a specific stop**, add it to `STOP_NAME_OVERRIDES` near the top of `main.py`
  with better search text (a couple of examples are already there), then call
  `POST /api/geocode-retry` (also authenticated) to re-geocode just the failed ones and
  rebuild the network — no restart needed.
- **Bus positions are simulated**, not real GPS — WBTC doesn't publish live vehicle
  locations. Each bus drives its real stop sequence out and back at a simulated speed
  (`DEMO_SPEEDUP` in `main.py`, default 8x, so movement is visible; set it to 1 for
  roughly real-time speeds).
- **Route lines are straight lines between geocoded stops**, not road-following paths.
  Adding the Directions API for real road-snapped polylines is a reasonable next step
  if you want that level of accuracy.

## Forgot password

"Forgot password?" on the login screen asks for an email and sends a reset link. The link
opens the app on a "Choose a new password" screen.

- **While developing (no email set up):** the link is printed in the terminal running
  uvicorn. Copy it into your browser to finish the flow.
- **Real emails:** fill in `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
  `SMTP_FROM` in `.env`, and set `APP_BASE_URL` to the address the app is served from.
  For Gmail use `smtp.gmail.com`, port `587`, and an App Password (not your normal password).
- Links expire after 30 minutes and work once. Resetting signs the account out everywhere.
- The screen gives the same reply whether or not the email has an account, and each email
  can request at most 3 links per 15 minutes.

## Developer-only user database

Accounts live in `bustrack.db` (SQLite) next to `main.py`: name, email, join date, last
login, and a one-way hash of each password. Users can only ever see their own name and
email through the app; nothing in the website reads or lists other accounts.

To look at the data yourself, use the admin tool in a terminal in this folder:

```bat
python admin.py users                   list all accounts
python admin.py user asha@example.com   one account's details
python admin.py stats                   totals
python admin.py export users.csv        save the list as CSV (no passwords)
python admin.py set-password EMAIL      set someone a new password
python admin.py delete EMAIL            delete an account
```

You can also open `bustrack.db` with a free viewer such as "DB Browser for SQLite".

**Passwords are not viewable, by design.** Only a salted PBKDF2 hash is stored, which can't
be turned back into the password, so not even you can read them. That protects users if the
file ever leaks. If someone forgets theirs, they use "Forgot password?" or you run `set-password`.

Keeping it developer-only:
- The database is not inside `static/`, so the website can't serve it. Keep it that way.
- `bustrack.db` and `.env` are in `.gitignore`. Never upload them to GitHub or share them.
- Anyone who can open the folder on your computer (or the server) can read the file, so protect
  the machine with a login, and back the file up somewhere private.
- The admin tool is not part of the website: there is no admin page or URL to attack.

## Accounts

Passwords are salted and hashed (PBKDF2, 200,000 iterations). Login returns a session
token valid for 7 days. Five wrong passwords for the same email within five minutes
locks that email out for a few minutes. Accounts live in `bustrack.db` (SQLite,
created on first run) — delete that file to reset everything, including the geocode
cache (which will re-geocode on next startup).
