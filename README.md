# QOTD / SOTD Discord Bot

Posts a **Question of the Day** in one channel and a **Song of the Day** in another.
People submit suggestions with slash commands. Every day the bot randomly draws one
song and one question, but instead of posting them live it sends the draw to a
**private review channel** first — an admin can add a picture, approve it to publish,
or send it back and draw a different one. Once published, an item is deleted from the
pool for good, so nothing repeats.

## Commands

| Command | Who | What it does |
|---|---|---|
| `/submit-song` | everyone | Submit `from_who` (a real member picked from a dropdown), `lyrics`, plus either a `song` title or a Spotify/YouTube `link` (auto-fetches title + cover art), and an optional manual `image` |
| `/submit-question` | everyone | Submit a `question` and an optional `image` |
| `/qotd-sotd-status` | everyone | See how many songs/questions are queued, and what's currently pending review |
| `/draw-sotd-now` | admins (Manage Server) | Manually trigger today's Song draw, sending it to the review channel |
| `/draw-qotd-now` | admins (Manage Server) | Manually trigger today's Question draw, sending it to the review channel |
| `/approve-sotd` | admins (Manage Server) | Publish the pending song to the public SOTD channel |
| `/approve-qotd` | admins (Manage Server) | Publish the pending question to the public QOTD channel |
| `/redraw-sotd` | admins (Manage Server) | Skip the pending song (returns it to the pool) and draw a different one |
| `/redraw-qotd` | admins (Manage Server) | Skip the pending question (returns it to the pool) and draw a different one |
| `/edit-sotd-image` | admins (Manage Server) | Attach or replace the picture on the pending song before approving |
| `/edit-qotd-image` | admins (Manage Server) | Attach or replace the picture on the pending question before approving |

Submissions are stored in a local SQLite file (`data.db`) — no external database needed.

**The review workflow:** each day at the configured time, the bot draws one random song and one random question and posts them as an embed in your private `REVIEW_CHANNEL_ID` — not the public channels. From there, an admin runs `/approve-sotd` (or `-qotd`) to publish it for real, `/edit-sotd-image` first if it needs a picture, or `/redraw-sotd` to put it back and draw something else instead. If a draw is left un-reviewed, the bot won't draw a second one on top of it the next day — it just posts a reminder in the review channel until someone approves or redraws it. This means the daily schedule still runs like clockwork, but nothing reaches the public channel without a human glance first.

**Adding a photo at submission time:** when running `/submit-song` or `/submit-question`, an `image` field appears as an optional attachment — tap it and pick a photo from your device before sending the command. Accepted formats are PNG, JPG, GIF, and WEBP, up to 8MB.

**Spotify / YouTube links:** instead of typing a song title, paste a Spotify track link or a YouTube video link into the `link` field of `/submit-song`. The bot fetches the real title and cover art / thumbnail automatically (no API key needed — it uses each platform's public oEmbed endpoint) and uses that as the song's title and image. You can still attach a manual `image` alongside a link if you want to override the automatic art. Either `song` or `link` is required — not both.

**Who the song is from:** `from_who` is a real Discord member picker, not a text box — Discord shows a searchable dropdown of actual server members to choose from. This means people can't type a made-up or someone else's name to hide that a song is their own pick; the attribution is always tied to a genuine account.

**Low-queue nudges:** if you set `NUDGE_CHANNEL_ID`, the bot posts a reminder there whenever a pool's remaining count drops to or below `NUDGE_THRESHOLD` (default 3) right after that day's draw — e.g. "only 2 songs left, submit yours!" — so the queue rarely runs dry unannounced.

## 1. Create the Discord bot application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Go to **Bot** → **Add Bot**. Copy the token (you'll need it for `DISCORD_TOKEN`). Keep it secret.
3. Under **Bot**, you don't need any privileged intents for this bot (it doesn't read message content).
4. Go to **OAuth2 → URL Generator**. Select scopes `bot` and `applications.commands`. Under bot permissions, select at least `Send Messages`, `Embed Links`, `Use Slash Commands`. Copy the generated URL and open it to invite the bot to your server.
5. Right-click your server icon → **Copy Server ID** (enable Developer Mode in Discord settings first if you don't see this) → this is your optional `GUILD_ID`.
6. Right-click each destination channel → **Copy Channel ID** → these give you `QOTD_CHANNEL_ID` and `SOTD_CHANNEL_ID`.
7. Create (or pick an existing) **private channel visible only to admins/mods** — this is where daily draws go for review. Copy its ID → `REVIEW_CHANNEL_ID`. This one's required: without it, the bot will skip its daily draws entirely rather than post unreviewed content.

## 2. Configure

```bash
cp .env.example .env
```

Fill in `.env` with your token, channel IDs, timezone, and the time of day you want posts to go out.

## 3. Run it locally (to test)

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python bot.py
```

Try `/submit-song` (with both a manual title and, separately, a Spotify/YouTube link) and `/submit-question`, then `/draw-sotd-now` and `/draw-qotd-now` to confirm a draw shows up correctly in your review channel — then try `/edit-sotd-image`, `/approve-sotd`, and `/redraw-sotd` to make sure the full review flow works before waiting for the real daily schedule.

## 4. Hosting it for free (so it runs 24/7)

The bot needs to stay running all the time to catch its daily posting time and respond to slash commands instantly — a "serverless" free tier that sleeps (like Render's free web service) isn't a good fit. Good free options:

### Option A — Oracle Cloud Free Tier (best long-term free option)
Oracle's "Always Free" tier includes a small VM that's genuinely free forever (not a trial). Spin up an Ubuntu instance, install Python, `git clone` your bot, and run it with `pm2` or a `systemd` service so it restarts automatically. More setup effort, but no recurring cost and no sleep/spin-down.

### Option B — Railway (recommended, easiest)
Railway gives a small monthly free credit that's usually enough for a lightweight bot like this. This repo already includes a `Procfile` and `runtime.txt` so Railway knows how to run it. See the step-by-step walkthrough below.

#### Railway step-by-step

1. **Push this folder to a GitHub repo.**
   ```bash
   cd qotd-sotd-bot
   git init
   git add .
   git commit -m "Initial commit"
   ```
   Create a new empty repo on GitHub, then follow GitHub's instructions to push (`git remote add origin ...`, `git push -u origin main`). Since `.env` is in `.gitignore`, your token won't be pushed — good.

2. **Sign up at [railway.app](https://railway.app)** with your GitHub account.

3. **New Project → Deploy from GitHub repo** → select the repo you just pushed.

4. Railway will detect it's a Python app (thanks to `requirements.txt` and `runtime.txt`) and use the `Procfile` to know it should run `python bot.py` as a **worker** — not a web service, so you don't need to worry about ports or health checks.

5. **Set environment variables**: in the Railway project, go to your service → **Variables** tab, and add each value from your `.env` file one at a time (`DISCORD_TOKEN`, `GUILD_ID`, `QOTD_CHANNEL_ID`, `SOTD_CHANNEL_ID`, `REVIEW_CHANNEL_ID`, `NUDGE_CHANNEL_ID`, `NUDGE_THRESHOLD`, `TIMEZONE`, `POST_HOUR`, `POST_MINUTE`). Don't upload `.env` itself — Railway variables replace it.

6. Railway deploys automatically. Check the **Deployments → Logs** tab — you should see `Logged in as YourBot#1234 — posting daily at 09:00 Europe/London`.

7. From now on, every `git push` to this repo auto-redeploys the bot.

**One thing to know about storage:** Railway's free filesystem is ephemeral by default — if the service restarts, `data.db` can reset. This matters a bit more now than it would for a simple queue: a song or question that's been *drawn* but not yet `/approve`-d only exists in the `pending` table, so a restart at exactly the wrong moment could lose that one draw (it would already be out of the main pool too). Low-stakes, but if you want it to persist properly, add a **Railway Volume** to the service (Settings → Volumes → mount at e.g. `/data`) and set `DB_PATH=/data/data.db` in your variables. Recommended if you plan to actually rely on this day to day.

### Option C — A spare computer / Raspberry Pi at home
If you've got an old laptop, desktop, or Raspberry Pi that can stay on, this is truly free with no limits. Run the bot with `pm2` (or `nohup python3 bot.py &` / a `systemd` unit) so it survives reboots and restarts if it crashes.

### Option D — fly.io
Also has a small free allowance and supports always-on background workers (not just web services), so it works well for Discord bots.

Whichever you pick, remember to add `data.db` and `.env` to a `.gitignore` before pushing to GitHub — you don't want your bot token or your users' submissions public.

## Notes / possible tweaks

- Right now both QOTD and SOTD are drawn at the same configured time. If you want them at different times, duplicate `POST_HOUR`/`POST_MINUTE` into two separate pairs and adjust `daily_qotd` / `daily_sotd` in `bot.py` accordingly.
- If a pool runs dry on a given day, the bot posts a friendly "no submissions" message directly to the public channel (no review needed for an empty draw).
- If a draw is sitting unreviewed in the review channel, the bot won't draw a second one on top of it — it just posts a reminder each day until you `/approve` or `/redraw` it. So it's safe to leave the bot running unattended; nothing piles up or gets skipped, it just waits for you.
- `/redraw-sotd` / `/redraw-qotd` put the skipped item back into the pool rather than deleting it — it can still be drawn again another day.
- Submissions aren't tied to a specific date — anyone can submit any time and it just joins the pool for a future random draw.
