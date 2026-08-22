# Flight fare tracker

Polls Google Flights every 3 hours for a fixed set of routes and dates, keeps a full
price history, and messages you on Telegram the moment a fare drops. Runs free on
GitHub Actions — no server, no laptop left open.

**Currently tracking**

| Route | Dates |
|---|---|
| DEL → BKK (Suvarnabhumi) | 16, 17, 18 Jan 2027 |
| DEL → DMK (Don Mueang) | 16, 17, 18 Jan 2027 |
| HKT (Phuket) → DEL | 23, 24, 25 Jan 2027 |

DMK is included because AirAsia, Thai Lion and Nok fly into Don Mueang, not
Suvarnabhumi — Bangkok fares often differ by several thousand rupees between the two
airports. Delete that block in `tracker.py` if you don't want it.

---

## Setup (about 10 minutes)

### 1. Make a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → pick a name.
   It replies with a token like `8123456789:AAF...`.
2. Send your new bot any message (say "hi") so it's allowed to write to you.
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and copy
   the `"chat":{"id":...}` number.

### 2–4. One command

With the [GitHub CLI](https://cli.github.com) installed and `gh auth login` done once:

```bash
chmod +x setup.sh && ./setup.sh
```

It creates the repo, pushes the code, stores your Telegram values as Actions secrets,
enables the dashboard page, and triggers the first run. It sends a test message to your
phone before touching anything, so a wrong token fails immediately rather than silently.

Prefer to do it by hand? The manual version follows.

### 2. Put the code on GitHub

Create a **private** repo, drop these files in, and push. Then go to
**Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the BotFather token |
| `TELEGRAM_CHAT_ID` | the chat id from step 1 |

Email is optional and works alongside Telegram — add `SMTP_HOST`, `SMTP_USER`,
`SMTP_PASS` (a Gmail **app password**, not your login) and `ALERT_EMAIL` if you want it.

### 3. Kick off the first run

**Actions** tab → *Track flight prices* → **Run workflow**. That first run seeds the
history; alerts start from the second run onward, once there's something to compare
against. Check it worked with `python tracker.py --test-alert` locally, or just look
at the Actions log.

### 4. See the charts (optional)

**Settings → Pages → Source: Deploy from a branch → `main` / `docs`.** Your dashboard
lands at `https://<username>.github.io/<repo>/` — every tracked date with its current
fare, the change since last check, a sparkline of the whole history, and a booking link.
It's regenerated on every run.

---

## Running it locally

```bash
pip install -r requirements.txt
python tracker.py                 # one check, prints results, updates history.json
python tracker.py --report-only   # rebuild docs/index.html from saved history
python tracker.py --test-alert    # verify Telegram/email credentials
```

Without credentials set, alerts just print to the terminal.

## Tuning

Everything worth changing sits at the top of `tracker.py`:

- `ROUTES` — origins, destinations, dates, and an optional `target` price that always
  fires an alert when reached (e.g. `"target": 16000`).
- `DROP_PCT` / `DROP_ABS` — a drop must clear **both** to alert (3% *and* ₹800 by
  default). Raise these if you get pinged too often; airline fares wobble by a few
  hundred rupees constantly.
- `ALERT_ON_NEW_LOW` — always alert on an all-time low, regardless of drop size.
- `MAX_STOPS`, `SEAT`, `ADULTS`, `CURRENCY`.
- `MIN_DELAY` / `MAX_DELAY` — the pause between searches. Don't shrink these; nine
  searches back-to-back is what gets a scraper blocked.

## Things to know

- **It scrapes, it isn't an official API.** `fast-flights` reads Google Flights' own
  response format. That format changes occasionally, and when it does the library
  needs an update (`pip install -U fast-flights`). Runs will log errors rather than
  crash the workflow, but keep an eye on the Actions tab — silence could mean "no
  price drops" or "it broke three weeks ago". The dashboard's *last checked* timestamp
  is the honest tell.
- **GitHub disables scheduled workflows after 60 days of repo inactivity.** It emails
  you first. The commit each run makes counts as activity, so this shouldn't bite.
- **Hourly is roughly the sensible floor.** Fares reprice several times a day, not
  several times an hour, and more frequent polling mostly buys you a higher chance of
  being rate-limited.
- **Mistake fares can vanish in minutes.** A 3-hour poll will miss some of them. That's
  the gap dedicated deal services fill, and it's not one a cron job closes.
