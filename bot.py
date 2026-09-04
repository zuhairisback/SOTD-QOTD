"""
QOTD / SOTD Discord Bot
-----------------------
- Users submit a Question of the Day via /submit-question (question + optional image).
- Users submit a Song of the Day via /submit-song: either raw (song title + optional
  image) or by pasting a Spotify track / YouTube video link, which auto-fetches the
  title and cover art. Either way, from_who is picked from a real member dropdown so
  nobody can fake who a song is from.
- Every day at a configured time, the bot randomly draws one unused question and one
  unused song from the pool (removing them so they can never repeat) and posts them
  to a private review channel instead of straight to the public channel.
- An admin reviews the draw in that private channel: they can attach/replace a picture
  with /edit-sotd-image or /edit-qotd-image, publish it with /approve-sotd or
  /approve-qotd, or send it back and draw a different one with /redraw-sotd or
  /redraw-qotd.
- If a pool runs low, the bot nudges a configured channel to ask for more submissions.

See README.md for setup and free hosting instructions.
"""

import os
import re
import json
import sqlite3
import datetime
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional: speeds up slash command sync during testing
QOTD_CHANNEL_ID = int(os.getenv("QOTD_CHANNEL_ID", "0"))
SOTD_CHANNEL_ID = int(os.getenv("SOTD_CHANNEL_ID", "0"))

# Roles pinged when a Song/Question of the Day is actually published (not on the private draw).
SOTD_ROLE_ID = int(os.getenv("SOTD_ROLE_ID", "0"))
QOTD_ROLE_ID = int(os.getenv("QOTD_ROLE_ID", "0"))

# Private channel where each day's draw is sent for review before it goes public.
REVIEW_CHANNEL_ID = int(os.getenv("REVIEW_CHANNEL_ID", "0"))

TIMEZONE = os.getenv("TIMEZONE", "Europe/London")
POST_HOUR = int(os.getenv("POST_HOUR", "9"))
POST_MINUTE = int(os.getenv("POST_MINUTE", "0"))
DB_PATH = os.getenv("DB_PATH", "data.db")

# Low-queue nudges: posted to NUDGE_CHANNEL_ID whenever a pool drops to/below NUDGE_THRESHOLD
# right after that day's draw. Leave NUDGE_CHANNEL_ID unset (0) to disable this feature.
NUDGE_CHANNEL_ID = int(os.getenv("NUDGE_CHANNEL_ID", "0"))
NUDGE_THRESHOLD = int(os.getenv("NUDGE_THRESHOLD", "3"))

TZ = ZoneInfo(TIMEZONE)
POST_TIME = datetime.time(hour=POST_HOUR, minute=POST_MINUTE, tzinfo=TZ)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            song TEXT NOT NULL,
            from_who TEXT NOT NULL,
            from_who_id INTEGER,
            lyrics TEXT,
            image_url TEXT,
            source_link TEXT,
            submitted_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            question TEXT NOT NULL,
            image_url TEXT,
            submitted_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending (
            kind TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            review_message_id INTEGER,
            review_channel_id INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduled (
            kind TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    # Migration: add columns to databases created before these features existed.
    for table, col, coltype in (
        ("songs", "image_url", "TEXT"),
        ("questions", "image_url", "TEXT"),
        ("songs", "from_who_id", "INTEGER"),
        ("songs", "source_link", "TEXT"),
    ):
        existing_cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    conn.commit()
    conn.close()


def add_song(
    user_id: int,
    username: str,
    song: str,
    from_who: str,
    lyrics: str,
    image_url: Optional[str] = None,
    from_who_id: Optional[int] = None,
    source_link: Optional[str] = None,
):
    conn = get_conn()
    conn.execute(
        "INSERT INTO songs (user_id, username, song, from_who, from_who_id, lyrics, image_url, source_link, submitted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            username,
            song,
            from_who,
            from_who_id,
            lyrics,
            image_url,
            source_link,
            datetime.datetime.now(TZ).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def add_question(user_id: int, username: str, question: str, image_url: Optional[str] = None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO questions (user_id, username, question, image_url, submitted_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, question, image_url, datetime.datetime.now(TZ).isoformat()),
    )
    conn.commit()
    conn.close()


def pop_random_song():
    """Return a random song row and delete it from the DB, or None if empty."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM songs ORDER BY RANDOM() LIMIT 1").fetchone()
    if row:
        conn.execute("DELETE FROM songs WHERE id = ?", (row["id"],))
        conn.commit()
    conn.close()
    return row


def pop_random_question():
    conn = get_conn()
    row = conn.execute("SELECT * FROM questions ORDER BY RANDOM() LIMIT 1").fetchone()
    if row:
        conn.execute("DELETE FROM questions WHERE id = ?", (row["id"],))
        conn.commit()
    conn.close()
    return row


def counts():
    conn = get_conn()
    s = conn.execute("SELECT COUNT(*) c FROM songs").fetchone()["c"]
    q = conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
    conn.close()
    return s, q


def return_song_to_pool(data: dict):
    add_song(
        data["user_id"],
        data["username"],
        data["song"],
        data["from_who"],
        data["lyrics"],
        data.get("image_url"),
        data.get("from_who_id"),
        data.get("source_link"),
    )


def return_question_to_pool(data: dict):
    add_question(data["user_id"], data["username"], data["question"], data.get("image_url"))


# --- pending (drawn-but-not-yet-approved) items ----------------------------

def get_pending(kind: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM pending WHERE kind = ?", (kind,)).fetchone()
    conn.close()
    return row


def set_pending(kind: str, data: dict, review_message_id: int, review_channel_id: int):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO pending (kind, data, review_message_id, review_channel_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(kind) DO UPDATE SET
            data=excluded.data,
            review_message_id=excluded.review_message_id,
            review_channel_id=excluded.review_channel_id,
            created_at=excluded.created_at
        """,
        (kind, json.dumps(data), review_message_id, review_channel_id, datetime.datetime.now(TZ).isoformat()),
    )
    conn.commit()
    conn.close()


def update_pending_data(kind: str, data: dict):
    conn = get_conn()
    conn.execute("UPDATE pending SET data = ? WHERE kind = ?", (json.dumps(data), kind))
    conn.commit()
    conn.close()


def clear_pending(kind: str):
    conn = get_conn()
    conn.execute("DELETE FROM pending WHERE kind = ?", (kind,))
    conn.commit()
    conn.close()


# --- scheduled (approved, waiting for the next daily publish time) ---------

def get_scheduled(kind: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM scheduled WHERE kind = ?", (kind,)).fetchone()
    conn.close()
    return row


def set_scheduled(kind: str, data: dict):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO scheduled (kind, data, created_at) VALUES (?, ?, ?)
        ON CONFLICT(kind) DO UPDATE SET data=excluded.data, created_at=excluded.created_at
        """,
        (kind, json.dumps(data), datetime.datetime.now(TZ).isoformat()),
    )
    conn.commit()
    conn.close()


def clear_scheduled(kind: str):
    conn = get_conn()
    conn.execute("DELETE FROM scheduled WHERE kind = ?", (kind,))
    conn.commit()
    conn.close()


def next_post_datetime() -> datetime.datetime:
    """The next upcoming occurrence of POST_HOUR:POST_MINUTE — today if it hasn't
    happened yet, otherwise tomorrow."""
    now = datetime.datetime.now(TZ)
    candidate = now.replace(hour=POST_HOUR, minute=POST_MINUTE, second=0, microsecond=0)
    if candidate <= now:
        candidate += datetime.timedelta(days=1)
    return candidate


def describe_next_post_time() -> str:
    dt = next_post_datetime()
    now = datetime.datetime.now(TZ)
    day_word = "today" if dt.date() == now.date() else "tomorrow"
    return f"{day_word} at {dt.strftime('%H:%M')} {TIMEZONE}"


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    init_db()
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        else:
            synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Slash command sync failed: {e}")

    if not REVIEW_CHANNEL_ID:
        print("WARNING: REVIEW_CHANNEL_ID is not set. Daily draws will be skipped until it's configured.")

    if not daily_qotd.is_running():
        daily_qotd.start()
    if not daily_sotd.is_running():
        daily_sotd.start()

    print(f"Logged in as {bot.user} — drawing daily at {POST_HOUR:02d}:{POST_MINUTE:02d} {TIMEZONE}")


# ---------------------------------------------------------------------------
# Link metadata (Spotify / YouTube oEmbed — no API key required)
# ---------------------------------------------------------------------------

SPOTIFY_TRACK_RE = re.compile(r"open\.spotify\.com/track/", re.IGNORECASE)
YOUTUBE_RE = re.compile(r"(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)", re.IGNORECASE)


def detect_link_provider(link: str):
    """Return (provider, error_message). Exactly one of the two will be None."""
    if SPOTIFY_TRACK_RE.search(link):
        return "spotify", None
    if YOUTUBE_RE.search(link):
        return "youtube", None
    return None, "That doesn't look like a Spotify track link or a YouTube video link."


async def fetch_oembed_metadata(link: str, provider: str):
    """Return (title, thumbnail_url) or None on any failure."""
    if provider == "spotify":
        oembed_url = f"https://open.spotify.com/oembed?url={quote(link, safe='')}"
    else:
        oembed_url = f"https://www.youtube.com/oembed?url={quote(link, safe='')}&format=json"

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(oembed_url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                title = data.get("title")
                thumbnail = data.get("thumbnail_url")
                if not title:
                    return None
                return title, thumbnail
    except Exception as e:
        print(f"oEmbed fetch failed for {link}: {e}")
        return None


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def build_song_embed(data: dict, pending: bool) -> discord.Embed:
    title = "🎵 Pending Review — Song of the Day" if pending else "🎵 Song of the Day"
    color = discord.Color.orange() if pending else discord.Color.blurple()
    embed = discord.Embed(title=title, color=color)
    song_value = f"[{data['song']}]({data['source_link']})" if data.get("source_link") else data["song"]
    embed.add_field(name="Song", value=song_value, inline=False)
    embed.add_field(name="From", value=data["from_who"], inline=False)
    if data.get("lyrics"):
        embed.add_field(name="Favourite lyric", value=data["lyrics"], inline=False)
    if data.get("image_url"):
        embed.set_image(url=data["image_url"])
    if pending:
        embed.description = (
            "`/edit-sotd-image` to add/replace the picture • "
            "`/approve-sotd` to publish this • "
            "`/redraw-sotd` to skip it and draw another"
        )
        # Only the private review copy shows who really submitted it — the public
        # post deliberately omits this so people can dedicate songs anonymously
        # without giving away that it's actually their own pick.
        embed.set_footer(text=f"Submitted by {data['username']}")
    return embed


def build_question_embed(data: dict, pending: bool) -> discord.Embed:
    title = "❓ Pending Review — Question of the Day" if pending else "❓ Question of the Day"
    color = discord.Color.orange() if pending else discord.Color.gold()
    embed = discord.Embed(title=title, description=data["question"], color=color)
    if data.get("image_url"):
        embed.set_image(url=data["image_url"])
    if pending:
        embed.add_field(
            name="Actions",
            value=(
                "`/edit-qotd-image` to add/replace the picture • "
                "`/approve-qotd` to publish this • "
                "`/redraw-qotd` to skip it and draw another"
            ),
            inline=False,
        )
        # Same reasoning as the song embed — only visible in the private review copy.
        embed.set_footer(text=f"Submitted by {data['username']}")
    return embed


# ---------------------------------------------------------------------------
# Slash commands: submissions
# ---------------------------------------------------------------------------

def _validate_image(attachment: Optional[discord.Attachment]) -> Optional[str]:
    """Return an error message if the attachment isn't a usable image, else None."""
    if attachment is None:
        return None
    if attachment.content_type is None or not attachment.content_type.startswith("image/"):
        return "That attachment doesn't look like an image — please attach a PNG, JPG, GIF, or WEBP."
    if attachment.size > 8 * 1024 * 1024:  # 8MB, safely under Discord's non-boosted upload limits
        return "That image is too large (max 8MB). Try a smaller file."
    return None


@bot.tree.command(name="submit-song", description="Submit a Song of the Day suggestion")
@app_commands.describe(
    from_who="Who this song is from — pick their actual Discord account from the list",
    lyrics="Your favourite lyric from the song",
    song="The song title (only needed if you're not pasting a link below)",
    link="Optional: a Spotify track or YouTube link — we'll grab the title & cover art for you",
    image="Optional: manually attach a picture (overrides the automatic cover art if you also gave a link)",
)
async def submit_song(
    interaction: discord.Interaction,
    from_who: discord.Member,
    lyrics: str,
    song: Optional[str] = None,
    link: Optional[str] = None,
    image: Optional[discord.Attachment] = None,
):
    await interaction.response.defer(ephemeral=True)

    error = _validate_image(image)
    if error:
        await interaction.followup.send(f"⚠️ {error}", ephemeral=True)
        return

    fetched_title = None
    fetched_thumb = None

    if link:
        provider, err = detect_link_provider(link)
        if err:
            await interaction.followup.send(f"⚠️ {err}", ephemeral=True)
            return
        meta = await fetch_oembed_metadata(link, provider)
        if meta is None:
            await interaction.followup.send(
                "⚠️ Couldn't fetch info from that link — double check it's a public Spotify track "
                "or YouTube video link, or just fill in the song title manually instead.",
                ephemeral=True,
            )
            return
        fetched_title, fetched_thumb = meta

    if not song and not fetched_title:
        await interaction.followup.send(
            "⚠️ Please provide either a `song` title or a Spotify/YouTube `link`.", ephemeral=True
        )
        return

    final_song = song or fetched_title
    final_image = image.url if image else fetched_thumb

    add_song(
        interaction.user.id,
        str(interaction.user),
        final_song,
        from_who.display_name,
        lyrics,
        final_image,
        from_who.id,
        link,
    )

    msg = f"🎵 Got it! **{final_song}** (from {from_who.mention}) has been added to the Song of the Day pool."
    if fetched_thumb and not image:
        msg += " (cover art fetched automatically)"
    elif image:
        msg += " (with your image attached)"
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="submit-question", description="Submit a Question of the Day suggestion")
@app_commands.describe(
    question="The question you want to submit",
    image="Optional: a photo or image to go with the question",
)
async def submit_question(
    interaction: discord.Interaction,
    question: str,
    image: Optional[discord.Attachment] = None,
):
    error = _validate_image(image)
    if error:
        await interaction.response.send_message(f"⚠️ {error}", ephemeral=True)
        return

    add_question(interaction.user.id, str(interaction.user), question, image.url if image else None)
    msg = "❓ Got it! Your question has been added to the Question of the Day pool."
    if image:
        msg += " (with your image attached)"
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="qotd-sotd-status", description="See how many submissions are queued and what's pending/scheduled")
async def status(interaction: discord.Interaction):
    s, q = counts()
    lines = [f"📊 Currently queued: **{s}** song(s), **{q}** question(s)."]

    pending_song = get_pending("song")
    if pending_song:
        data = json.loads(pending_song["data"])
        lines.append(f"🎵 Pending review: **{data['song']}** (from {data['from_who']}) — awaiting `/approve-sotd`.")

    pending_question = get_pending("question")
    if pending_question:
        data = json.loads(pending_question["data"])
        lines.append(f"❓ Pending review: \"{data['question']}\" — awaiting `/approve-qotd`.")

    scheduled_song = get_scheduled("song")
    if scheduled_song:
        data = json.loads(scheduled_song["data"])
        lines.append(f"✅ Approved, will publish {describe_next_post_time()}: **{data['song']}**")

    scheduled_question = get_scheduled("question")
    if scheduled_question:
        data = json.loads(scheduled_question["data"])
        lines.append(f"✅ Approved, will publish {describe_next_post_time()}: \"{data['question']}\"")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------
# Core draw / review / approve / redraw logic
# ---------------------------------------------------------------------------

async def maybe_send_nudge(kind: str):
    if not NUDGE_CHANNEL_ID:
        return
    s, q = counts()
    remaining = s if kind == "song" else q
    if 0 < remaining <= NUDGE_THRESHOLD:
        channel = bot.get_channel(NUDGE_CHANNEL_ID)
        if channel is None:
            return
        if kind == "song":
            await channel.send(
                f"🎵 Only **{remaining}** song(s) left in the queue! Add yours with `/submit-song` before we run dry."
            )
        else:
            await channel.send(
                f"❓ Only **{remaining}** question(s) left in the queue! Add yours with `/submit-question` before we run dry."
            )


async def draw_and_review(kind: str) -> str:
    """Draw a random item and send it to the review channel. Returns a short status code."""
    review_channel = bot.get_channel(REVIEW_CHANNEL_ID) if REVIEW_CHANNEL_ID else None
    if review_channel is None:
        print(f"Cannot draw {kind}: REVIEW_CHANNEL_ID not set or channel not found.")
        return "no-review-channel"

    if get_pending(kind):
        await review_channel.send(
            f"⏳ Reminder: there's still a **{kind}** pick waiting for your review from a previous day — "
            f"approve or redraw it before the next one can be drawn."
        )
        return "reminder"

    row = pop_random_song() if kind == "song" else pop_random_question()
    if row is None:
        public_channel = bot.get_channel(SOTD_CHANNEL_ID if kind == "song" else QOTD_CHANNEL_ID)
        if public_channel:
            noun = "Song" if kind == "song" else "Question"
            cmd = "/submit-song" if kind == "song" else "/submit-question"
            await public_channel.send(f"😔 No {noun} of the Day submissions in the queue! Use `{cmd}` to add one.")
        return "empty"

    data = dict(row)
    embed = build_song_embed(data, pending=True) if kind == "song" else build_question_embed(data, pending=True)
    msg = await review_channel.send(embed=embed)
    set_pending(kind, data, msg.id, review_channel.id)

    await maybe_send_nudge(kind)
    return "drawn"


async def _publish(kind: str, data: dict) -> bool:
    """Actually send the item to its public channel, with role/person pings. Used both
    by the scheduled daily publish and by the instant /publish-*-now escape hatch."""
    public_channel = bot.get_channel(SOTD_CHANNEL_ID if kind == "song" else QOTD_CHANNEL_ID)
    if public_channel is None:
        return False

    embed = build_song_embed(data, pending=False) if kind == "song" else build_question_embed(data, pending=False)

    mentions = []
    if kind == "song":
        if SOTD_ROLE_ID:
            mentions.append(f"<@&{SOTD_ROLE_ID}>")
        if data.get("from_who_id"):
            mentions.append(f"<@{data['from_who_id']}>")
    else:
        if QOTD_ROLE_ID:
            mentions.append(f"<@&{QOTD_ROLE_ID}>")
    content = " ".join(mentions) if mentions else None

    await public_channel.send(
        content=content,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
    )
    return True


async def approve(kind: str) -> str:
    """Lock in the pending item to publish at the next daily post time (does NOT
    post immediately). Returns a short status code."""
    if get_scheduled(kind):
        return "already-scheduled"

    pending = get_pending(kind)
    if not pending:
        return "none"

    data = json.loads(pending["data"])
    set_scheduled(kind, data)
    clear_pending(kind)

    try:
        review_channel = bot.get_channel(pending["review_channel_id"])
        review_msg = await review_channel.fetch_message(pending["review_message_id"])
        await review_msg.edit(content=f"✅ Approved — will publish {describe_next_post_time()}.")
    except Exception:
        pass  # non-fatal — the approval itself already succeeded

    return "scheduled"


async def publish_now(kind: str) -> bool:
    """Escape hatch: instantly publish whatever's currently scheduled, bypassing the wait."""
    scheduled = get_scheduled(kind)
    if not scheduled:
        return False
    data = json.loads(scheduled["data"])
    ok = await _publish(kind, data)
    if ok:
        clear_scheduled(kind)
    return ok


async def redraw(kind: str) -> str:
    pending = get_pending(kind)
    if not pending:
        return "none"

    data = json.loads(pending["data"])
    if kind == "song":
        return_song_to_pool(data)
    else:
        return_question_to_pool(data)

    try:
        review_channel = bot.get_channel(pending["review_channel_id"])
        review_msg = await review_channel.fetch_message(pending["review_message_id"])
        await review_msg.edit(content="🔀 Skipped — drawing another one below.")
    except Exception:
        pass

    clear_pending(kind)
    return await draw_and_review(kind)


async def set_pending_image(kind: str, image_url: str) -> bool:
    pending = get_pending(kind)
    if not pending:
        return False

    data = json.loads(pending["data"])
    data["image_url"] = image_url
    update_pending_data(kind, data)

    try:
        review_channel = bot.get_channel(pending["review_channel_id"])
        review_msg = await review_channel.fetch_message(pending["review_message_id"])
        embed = build_song_embed(data, pending=True) if kind == "song" else build_question_embed(data, pending=True)
        await review_msg.edit(embed=embed)
    except Exception:
        pass  # non-fatal — the DB is already updated, so /approve will still use the new image

    return True


# ---------------------------------------------------------------------------
# Slash commands: admin review workflow
# ---------------------------------------------------------------------------

def _require_review_channel_msg() -> str:
    return "⚠️ `REVIEW_CHANNEL_ID` isn't configured — set it as an environment variable to use the review workflow."


@bot.tree.command(name="draw-sotd-now", description="[Admin] Manually trigger today's Song of the Day draw for review")
@app_commands.checks.has_permissions(manage_guild=True)
async def draw_sotd_now(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not REVIEW_CHANNEL_ID:
        await interaction.followup.send(_require_review_channel_msg(), ephemeral=True)
        return
    result = await draw_and_review("song")
    messages = {
        "drawn": "Drawn! Check the review channel.",
        "empty": "No songs queued.",
        "reminder": "There's already a pending song waiting for review.",
        "no-review-channel": _require_review_channel_msg(),
    }
    await interaction.followup.send(messages.get(result, "Done."), ephemeral=True)


@bot.tree.command(name="draw-qotd-now", description="[Admin] Manually trigger today's Question of the Day draw for review")
@app_commands.checks.has_permissions(manage_guild=True)
async def draw_qotd_now(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not REVIEW_CHANNEL_ID:
        await interaction.followup.send(_require_review_channel_msg(), ephemeral=True)
        return
    result = await draw_and_review("question")
    messages = {
        "drawn": "Drawn! Check the review channel.",
        "empty": "No questions queued.",
        "reminder": "There's already a pending question waiting for review.",
        "no-review-channel": _require_review_channel_msg(),
    }
    await interaction.followup.send(messages.get(result, "Done."), ephemeral=True)


@bot.tree.command(name="approve-sotd", description="[Admin] Approve the pending song to publish at the next daily post time")
@app_commands.checks.has_permissions(manage_guild=True)
async def approve_sotd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await approve("song")
    messages = {
        "scheduled": f"✅ Approved — will publish {describe_next_post_time()}.",
        "none": "Nothing is pending review right now.",
        "already-scheduled": "There's already an approved song waiting to publish — use `/publish-sotd-now` if you want it out immediately instead.",
    }
    await interaction.followup.send(messages.get(result, "Done."), ephemeral=True)


@bot.tree.command(name="approve-qotd", description="[Admin] Approve the pending question to publish at the next daily post time")
@app_commands.checks.has_permissions(manage_guild=True)
async def approve_qotd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await approve("question")
    messages = {
        "scheduled": f"✅ Approved — will publish {describe_next_post_time()}.",
        "none": "Nothing is pending review right now.",
        "already-scheduled": "There's already an approved question waiting to publish — use `/publish-qotd-now` if you want it out immediately instead.",
    }
    await interaction.followup.send(messages.get(result, "Done."), ephemeral=True)


@bot.tree.command(name="publish-sotd-now", description="[Admin] Instantly publish the already-approved song, skipping the wait for the scheduled time")
@app_commands.checks.has_permissions(manage_guild=True)
async def publish_sotd_now(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok = await publish_now("song")
    await interaction.followup.send(
        "✅ Published immediately!" if ok else "Nothing is currently approved/scheduled for song.", ephemeral=True
    )


@bot.tree.command(name="publish-qotd-now", description="[Admin] Instantly publish the already-approved question, skipping the wait for the scheduled time")
@app_commands.checks.has_permissions(manage_guild=True)
async def publish_qotd_now(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok = await publish_now("question")
    await interaction.followup.send(
        "✅ Published immediately!" if ok else "Nothing is currently approved/scheduled for question.", ephemeral=True
    )


@bot.tree.command(name="redraw-sotd", description="[Admin] Skip the pending song and draw a different one")
@app_commands.checks.has_permissions(manage_guild=True)
async def redraw_sotd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await redraw("song")
    messages = {
        "none": "Nothing is pending review right now.",
        "drawn": "Skipped — a new song has been drawn for review.",
        "empty": "Skipped — but the pool is now empty.",
        "no-review-channel": _require_review_channel_msg(),
    }
    await interaction.followup.send(messages.get(result, "Done."), ephemeral=True)


@bot.tree.command(name="redraw-qotd", description="[Admin] Skip the pending question and draw a different one")
@app_commands.checks.has_permissions(manage_guild=True)
async def redraw_qotd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await redraw("question")
    messages = {
        "none": "Nothing is pending review right now.",
        "drawn": "Skipped — a new question has been drawn for review.",
        "empty": "Skipped — but the pool is now empty.",
        "no-review-channel": _require_review_channel_msg(),
    }
    await interaction.followup.send(messages.get(result, "Done."), ephemeral=True)


@bot.tree.command(name="edit-sotd-image", description="[Admin] Add or replace the picture on the pending Song of the Day")
@app_commands.describe(image="The image to attach to the pending song")
@app_commands.checks.has_permissions(manage_guild=True)
async def edit_sotd_image(interaction: discord.Interaction, image: discord.Attachment):
    error = _validate_image(image)
    if error:
        await interaction.response.send_message(f"⚠️ {error}", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok = await set_pending_image("song", image.url)
    await interaction.followup.send(
        "🖼️ Picture updated on the pending song." if ok else "Nothing is pending review right now.", ephemeral=True
    )


@bot.tree.command(name="edit-qotd-image", description="[Admin] Add or replace the picture on the pending Question of the Day")
@app_commands.describe(image="The image to attach to the pending question")
@app_commands.checks.has_permissions(manage_guild=True)
async def edit_qotd_image(interaction: discord.Interaction, image: discord.Attachment):
    error = _validate_image(image)
    if error:
        await interaction.response.send_message(f"⚠️ {error}", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    ok = await set_pending_image("question", image.url)
    await interaction.followup.send(
        "🖼️ Picture updated on the pending question." if ok else "Nothing is pending review right now.", ephemeral=True
    )


# ---------------------------------------------------------------------------
# Scheduled daily tasks
# ---------------------------------------------------------------------------

@tasks.loop(time=POST_TIME)
async def daily_qotd():
    if get_scheduled("question"):
        await publish_now("question")
    if REVIEW_CHANNEL_ID:
        await draw_and_review("question")


@tasks.loop(time=POST_TIME)
async def daily_sotd():
    if get_scheduled("song"):
        await publish_now("song")
    if REVIEW_CHANNEL_ID:
        await draw_and_review("song")


@daily_qotd.before_loop
@daily_sotd.before_loop
async def before_loops():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(TOKEN)
