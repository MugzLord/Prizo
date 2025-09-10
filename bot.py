# bot.py — Prizo (multi-server)
import os
import re
import json
import math
import random
import sqlite3
import contextlib
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

# ========= Basics =========
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

INTENTS = discord.Intents.default()
INTENTS.message_content = True        # required for reading numbers
INTENTS.guilds = True
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ========= Storage =========
DB_PATH = os.getenv("DB_PATH", "counting_fun.db")  # you can mount a volume and point this to /data/counting_fun.db

def db():
    # Ensure directory exists (handles /data/... or any custom path)
    path = os.getenv("DB_PATH", "counting_fun.db")
    abs_path = os.path.abspath(path)
    dir_path = os.path.dirname(abs_path)
    if dir_path:  # empty when using a bare filename
        os.makedirs(dir_path, exist_ok=True)

    conn = sqlite3.connect(abs_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_state (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            current_number INTEGER NOT NULL DEFAULT 0,
            start_number INTEGER NOT NULL DEFAULT 1,
            last_user_id INTEGER,
            theme TEXT NOT NULL DEFAULT 'party',
            numbers_only INTEGER NOT NULL DEFAULT 0,
            facts_on INTEGER NOT NULL DEFAULT 1,
            guild_streak INTEGER NOT NULL DEFAULT 0,
            best_guild_streak INTEGER NOT NULL DEFAULT 0,
            giveaway_target INTEGER,
            giveaway_range_min INTEGER NOT NULL DEFAULT 10,
            giveaway_range_max INTEGER NOT NULL DEFAULT 120,
            last_giveaway_n INTEGER NOT NULL DEFAULT 0,
            giveaway_prize TEXT NOT NULL DEFAULT '💎 500 VU Credits'
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_stats (
            guild_id INTEGER,
            user_id INTEGER,
            correct_counts INTEGER NOT NULL DEFAULT 0,
            wrong_counts INTEGER NOT NULL DEFAULT 0,
            streak_best INTEGER NOT NULL DEFAULT 0,
            badges INTEGER NOT NULL DEFAULT 0,
            last_updated TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS count_log (
            guild_id INTEGER,
            n INTEGER,
            user_id INTEGER,
            ts TEXT NOT NULL,
            PRIMARY KEY (guild_id, n)
        );
        """)

def get_state(gid: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM guild_state WHERE guild_id=?", (gid,)).fetchone()
        if not row:
            conn.execute("INSERT INTO guild_state (guild_id) VALUES (?)", (gid,))
            row = conn.execute("SELECT * FROM guild_state WHERE guild_id=?", (gid,)).fetchone()
        return row

def set_channel(gid: int, cid: int):
    with db() as conn:
        conn.execute("""
        INSERT INTO guild_state (guild_id, channel_id) VALUES (?,?)
        ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id;
        """, (gid, cid))

def set_start(gid: int, start: int):
    with db() as conn:
        conn.execute("""
        INSERT INTO guild_state (guild_id, start_number, current_number, last_user_id, guild_streak)
        VALUES (?,?,?,?,0)
        ON CONFLICT(guild_id) DO UPDATE SET
          start_number=excluded.start_number,
          current_number=excluded.current_number,
          last_user_id=NULL,
          guild_streak=0;
        """, (gid, start, start-1, None))

def reset_count(gid: int):
    with db() as conn:
        row = get_state(gid)
        conn.execute("UPDATE guild_state SET current_number=?, last_user_id=NULL, guild_streak=0 WHERE guild_id=?",
                     (row["start_number"] - 1, gid))

def set_numbers_only(gid: int, flag: bool):
    with db() as conn:
        conn.execute("UPDATE guild_state SET numbers_only=? WHERE guild_id=?", (1 if flag else 0, gid))

def set_facts_on(gid: int, flag: bool):
    with db() as conn:
        conn.execute("UPDATE guild_state SET facts_on=? WHERE guild_id=?", (1 if flag else 0, gid))

def set_theme(gid: int, theme: str):
    if theme not in THEMES: theme = DEFAULT_THEME
    with db() as conn:
        conn.execute("UPDATE guild_state SET theme=? WHERE guild_id=?", (theme, gid))

def _touch_user(conn, gid: int, uid: int, correct=0, wrong=0, streak_best=None, add_badge=False):
    now = datetime.utcnow().isoformat()
    row = conn.execute("SELECT * FROM user_stats WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()
    if not row:
        conn.execute("""
        INSERT INTO user_stats (guild_id, user_id, correct_counts, wrong_counts, streak_best, badges, last_updated)
        VALUES (?,?,?,?,?,?,?)
        """, (gid, uid, correct, wrong, streak_best or 0, 1 if add_badge else 0, now))
    else:
        new_badges = row["badges"] + (1 if add_badge else 0)
        new_streak = max(row["streak_best"], streak_best or 0)
        conn.execute("""
        UPDATE user_stats
        SET correct_counts = correct_counts + ?,
            wrong_counts   = wrong_counts + ?,
            streak_best    = ?,
            badges         = ?,
            last_updated   = ?
        WHERE guild_id=? AND user_id=?
        """, (correct, wrong, new_streak, new_badges, now, gid, uid))

def get_leaderboard(gid: int, limit=10):
    with db() as conn:
        return conn.execute("""
        SELECT user_id, correct_counts, badges
        FROM user_stats WHERE guild_id=?
        ORDER BY correct_counts DESC, badges DESC, user_id ASC
        LIMIT ?;
        """, (gid, limit)).fetchall()

def get_user_stats(gid: int, uid: int):
    with db() as conn:
        row = conn.execute("""
        SELECT correct_counts, wrong_counts, streak_best, badges, last_updated
        FROM user_stats WHERE guild_id=? AND user_id=?;
        """, (gid, uid)).fetchone()
        if not row:
            return {"correct_counts": 0, "wrong_counts": 0, "streak_best": 0, "badges": 0, "last_updated": None}
        return dict(row)

def bump_ok(gid: int, uid: int):
    with db() as conn:
        st = get_state(gid)
        next_num = st["current_number"] + 1
        new_streak = st["guild_streak"] + 1
        best = max(new_streak, st["best_guild_streak"])
        conn.execute("""
        UPDATE guild_state
        SET current_number=?, last_user_id=?, guild_streak=?, best_guild_streak=?
        WHERE guild_id=?;
        """, (next_num, uid, new_streak, best, gid))
        _touch_user(conn, gid, uid, correct=1, streak_best=new_streak)

def mark_wrong(gid: int, uid: int):
    with db() as conn:
        _touch_user(conn, gid, uid, wrong=1)
        conn.execute("UPDATE guild_state SET guild_streak=0 WHERE guild_id=?", (gid,))

def now_iso(): return datetime.utcnow().isoformat()

def log_correct_count(gid: int, n: int, uid: int):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO count_log (guild_id, n, user_id, ts) VALUES (?,?,?,?)",
                     (gid, n, uid, now_iso()))

# ========= Game bits =========
THEMES = {
    "party": {"ok": "✅", "bump": "🎉", "oops": "❌", "block": "⛔", "banner": "🎊 Party Mode 🎊"},
    "cats": {"ok": "✅", "bump": "😸", "oops": "🙀", "block": "😾", "banner": "🐾 Cat Parade 🐾"},
    "skulls": {"ok": "✅", "bump": "💀", "oops": "☠️", "block": "🧨", "banner": "💀 Bone Rattlers 💀"},
    "sports": {"ok": "✅", "bump": "🏆", "oops": "🚫", "block": "🟥", "banner": "🏟️ Stadium Roar 🏟️"},
    "hearts": {"ok": "✅", "bump": "💖", "oops": "💔", "block": "💢", "banner": "💘 Love Train 💘"},
}
DEFAULT_THEME = "party"

MILESTONES = {10, 20, 25, 30, 40, 50, 69, 75, 80, 90, 100, 111, 123, 150, 200, 250,
              300, 333, 369, 400, 420, 500, 600, 666, 700, 750, 800, 900, 999, 1000}

# banter from JSON
BANTER_PATH = os.getenv("BANTER_PATH", "banter.json")
def load_banter():
    try:
        with open(BANTER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k in ("wrong", "winner", "milestone"):
                data.setdefault(k, [])
            return data
    except Exception:
        return {"wrong": [], "winner": [], "milestone": []}
BANTER = load_banter()
def pick_banter(cat: str) -> str:
    lines = BANTER.get(cat, [])
    return random.choice(lines) if lines else ""

# utilities
INT_STRICT = re.compile(r"^\s*(-?\d+)\s*$")   # numbers-only
INT_LOOSE  = re.compile(r"^\s*(-?\d+)\b")     # number at start allowed
def extract_int(text: str, strict: bool):
    m = (INT_STRICT if strict else INT_LOOSE).match(text)
    return int(m.group(1)) if m else None

def is_admin(ix: discord.Interaction) -> bool:
    return ix.user.guild_permissions.manage_guild

def is_prime(n: int) -> bool:
    if n <= 1: return False
    if n <= 3: return True
    if n % 2 == 0 or n % 3 == 0: return False
    r, f = int(math.sqrt(n)), 5
    while f <= r:
        if n % f == 0 or n % (f+2) == 0: return False
        f += 6
    return True
def is_palindrome(n: int) -> bool:
    s = str(abs(n)); return len(s) > 1 and s == s[::-1]
def funny_number(n: int) -> bool:
    return n in {42, 69, 73, 96, 101, 111, 222, 333, 369, 404, 420, 666, 777, 999}
def maths_fact(n: int) -> str | None:
    # Custom IMVU-style fun facts—only one per number (priority order)
    # Priority: special funny numbers → palindrome → prime → multiples of 100 → multiples of 10 → nothing

    # Special “funny” numbers with custom lines
    funny_custom = {
        69: "a spicy content alert — probably hidden by Discover mods. 🌶️",
        420: "a smoke-room lobby count — hazy vibes incoming. 🚬",
        777: "casino credits energy — jackpot vibes. 🎰",
        999: "badge collector max mode — go collect 'em all! 🏅",
    }
    if n in funny_custom:
        return f"Fun fact: **{n}** is {funny_custom[n]}"

    # Palindromes
    if is_palindrome(n):
        return f"Fun fact: **{n}** is a mirror-selfie number — posting the same pic twice hoping for double likes. 📸"

    # Primes
    if is_prime(n):
        return f"Fun fact: **{n}** is rarer than a host online at 3 AM — iconic, questionable, unforgettable. 🌙"

    # Multiples of 100
    if n % 100 == 0:
        return f"Fun fact: **{n}** is pageant-crowd size — everyone’s clapping, half muted, full drama. 👑"

    # Multiples of 10
    if n % 10 == 0:
        return f"Fun fact: **{n}** is a bundle-drop number — clean, overpriced, and still selling out. 🛍️"

    # If nothing special, no fact
    return None

def theme_emoji(state, kind="bump"):
    theme = THEMES.get(state["theme"] or DEFAULT_THEME, THEMES[DEFAULT_THEME])
    return theme.get(kind, "🎉")

# ========= Hidden random giveaways =========
def _roll_next_target_after(conn, gid: int, current_n: int):
    st = conn.execute("SELECT giveaway_range_min, giveaway_range_max FROM guild_state WHERE guild_id=?",(gid,)).fetchone()
    rmin = max(5, int(st["giveaway_range_min"] or 10))
    rmax = max(rmin + 1, int(st["giveaway_range_max"] or 120))
    delta = random.randint(rmin, rmax)
    target = current_n + delta
    conn.execute("UPDATE guild_state SET giveaway_target=? WHERE guild_id=?", (target, gid))
    return target

def ensure_giveaway_target(gid: int):
    with db() as conn:
        row = conn.execute("SELECT current_number, giveaway_target FROM guild_state WHERE guild_id=?", (gid,)).fetchone()
        if row["giveaway_target"] is None or row["giveaway_target"] <= row["current_number"]:
            return _roll_next_target_after(conn, gid, row["current_number"])
        return row["giveaway_target"]

def try_giveaway_draw(bot: commands.Bot, message: discord.Message, reached_n: int):
    gid = message.guild.id
    with db() as conn:
        st = conn.execute("""
        SELECT giveaway_target, last_giveaway_n, giveaway_prize
        FROM guild_state WHERE guild_id=?
        """,(gid,)).fetchone()

        target = st["giveaway_target"]
        if target is None or reached_n != target:
            if target is None:
                _roll_next_target_after(conn, gid, reached_n)
            return False

        last_n = st["last_giveaway_n"] or 0
        rows = conn.execute("""
        SELECT DISTINCT user_id FROM count_log
        WHERE guild_id=? AND n>? AND n<=?
        """,(gid, last_n, reached_n)).fetchall()
        participants = [r["user_id"] for r in rows]
        if not participants:
            _roll_next_target_after(conn, gid, reached_n)
            conn.execute("UPDATE guild_state SET last_giveaway_n=? WHERE guild_id=?", (reached_n, gid))
            return False

        winner_id = random.choice(participants)
        prize = st["giveaway_prize"] or "🎁 Surprise Gift"
        winner_banter = pick_banter("winner") or "Legend behaviour. Take a bow. 👑"

        embed = discord.Embed(
            title="🎲 Random Giveaway!",
            description=(
                f"Hidden jackpot at **{reached_n}**!\n"
                f"Winner: <@{winner_id}> — {prize} 🥳\n\n"
                f"**{winner_banter}**\n"
                f"To claim your prize: **DM @mikey.moon on Discord** within 48 hours. 💬"
            ),
            colour=discord.Colour.gold()
        )
        embed.set_footer(text="New jackpot is secretly armed again… keep counting.")
        async def _announce():
            await message.channel.send(embed=embed)
        bot.loop.create_task(_announce())

        conn.execute("UPDATE guild_state SET last_giveaway_n=? WHERE guild_id=?", (reached_n, gid))
        _roll_next_target_after(conn, gid, reached_n)
        return True

# ========= Events =========
@bot.event
async def on_ready():
    init_db()
    # Global sync so commands work in EVERY server (and also sync per-guild on join below)
    try:
        synced = await bot.tree.sync()
        print(f"Globally synced {len(synced)} commands ✅")
    except Exception as e:
        print("Global sync error:", e)
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print("DB_PATH:", os.getenv("DB_PATH", "counting_fun.db"))


@bot.event
async def on_guild_join(guild: discord.Guild):
    # fast per-guild sync when the bot is added to a new server
    with contextlib.suppress(Exception):
        await bot.tree.sync(guild=guild)
        print(f"Per-guild synced commands to {guild.id}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    st = get_state(message.guild.id)
    # only act in the configured counting channel
    if not st["channel_id"] or message.channel.id != st["channel_id"]:
        return

    strict = bool(st["numbers_only"])
    posted = extract_int(message.content, strict=strict)
    if posted is None:
        return

    expected = st["current_number"] + 1
    last_user = st["last_user_id"]

    # rule: no double-posts
    if last_user == message.author.id:
        mark_wrong(message.guild.id, message.author.id)
        with contextlib.suppress(Exception):
            await message.add_reaction(theme_emoji(st, "block"))
            banter = pick_banter("wrong") or "Not two in a row. Behave. 😅"
            await message.reply(f"Not two in a row, {message.author.mention}. {banter} Next is **{expected}** for someone else.")
        return

    # rule: must be exact next number
    if posted != expected:
        mark_wrong(message.guild.id, message.author.id)
        with contextlib.suppress(Exception):
            await message.add_reaction(theme_emoji(st, "oops"))
            banter = pick_banter("wrong") or "Oof—maths says ‘nah’. 📏"
            await message.reply(f"{banter} Next up is **{expected}**.")
        return

    # success!
    bump_ok(message.guild.id, message.author.id)
    with contextlib.suppress(Exception):
        await message.add_reaction(theme_emoji(st, "ok"))

    # log for giveaway eligibility
    with contextlib.suppress(Exception):
        log_correct_count(message.guild.id, expected, message.author.id)

    # milestones
    if expected in MILESTONES:
        theme = THEMES.get(st["theme"], THEMES[DEFAULT_THEME])
        banner = theme["banner"]
        em = discord.Embed(
            title=banner,
            description=f"**{expected}** smashed by {message.author.mention}!",
            colour=discord.Colour.gold()
        )
        em.set_footer(text=f"Guild streak: {get_state(message.guild.id)['guild_streak']} • Keep it rolling!")
        with contextlib.suppress(Exception):
            await message.channel.send(embed=em)
            mb = pick_banter("milestone")
            if mb:
                await message.channel.send(mb)

    # facts + badges
    add_badge = (is_prime(expected) or is_palindrome(expected) or (expected % 100 == 0) or funny_number(expected))
    if add_badge or st["facts_on"]:
        fact = maths_fact(expected)
        if fact:
            with contextlib.suppress(Exception):
                await message.channel.send(f"✨ {fact}")
    if add_badge:
        with db() as conn:
            _touch_user(conn, message.guild.id, message.author.id, correct=0, wrong=0,
                        streak_best=get_state(message.guild.id)["guild_streak"], add_badge=True)

    # hidden giveaway
    ensure_giveaway_target(message.guild.id)
    try_giveaway_draw(bot, message, expected)

# ========= Slash Commands =========
class FunCounting(commands.Cog):
    def __init__(self, b: commands.Bot):
        self.bot = b

    # Setup
    @app_commands.command(name="setup_counting", description="Set the counting channel and starting number.")
    @app_commands.describe(channel="Channel to count in", start="Start number (default 1)")
    @app_commands.guild_only()
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel, start: int = 1):
        if not is_admin(interaction):
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        set_channel(interaction.guild_id, channel.id)
        set_start(interaction.guild_id, start)
        st = get_state(interaction.guild_id)
        await interaction.response.send_message(
            f"{theme_emoji(st,'bump')} Counting set in {channel.mention} from **{start}**. "
            f"Numbers-only: **{'ON' if st['numbers_only'] else 'OFF'}**, Theme: **{st['theme']}**.",
            ephemeral=True
        )

    # Channel / start / reset
    @app_commands.command(name="set_channel", description="Change the counting channel.")
    @app_commands.guild_only()
    async def setch(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not is_admin(interaction):
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        set_channel(interaction.guild_id, channel.id)
        st = get_state(interaction.guild_id)
        await interaction.response.send_message(
            f"✅ Counting channel set to {channel.mention}. Next: **{st['current_number']+1}**.",
            ephemeral=True
        )

    @app_commands.command(name="set_start", description="Set/Change the starting number (resets progress).")
    @app_commands.guild_only()
    async def setstart(self, interaction: discord.Interaction, number: int):
        if not is_admin(interaction):
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        set_start(interaction.guild_id, number)
        await interaction.response.send_message(
            f"🔄 Start number set to **{number}**. Next expected: **{number}**.", ephemeral=True
        )

    @app_commands.command(name="reset_count", description="Reset to the configured start number.")
    @app_commands.guild_only()
    async def resetc(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        reset_count(interaction.guild_id)
        st = get_state(interaction.guild_id)
        await interaction.response.send_message(
            f"🔁 Counter reset. Next expected: **{st['current_number']+1}**.", ephemeral=True
        )

    # Fun toggles
    @app_commands.command(name="theme", description="Set the bot’s reaction theme.")
    @app_commands.choices(name=[app_commands.Choice(name=t, value=t) for t in THEMES.keys()])
    @app_commands.guild_only()
    async def theme(self, interaction: discord.Interaction, name: app_commands.Choice[str]):
        if not is_admin(interaction):
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        set_theme(interaction.guild_id, name.value)
        st = get_state(interaction.guild_id)
        await interaction.response.send_message(f"Theme set to **{st['theme']}** {theme_emoji(st,'bump')}", ephemeral=True)

    @app_commands.command(name="numbers_only", description="Toggle numbers-only mode.")
    @app_commands.guild_only()
    async def numbers_only(self, interaction: discord.Interaction, on: bool):
        if not is_admin(interaction):
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        set_numbers_only(interaction.guild_id, on)
        await interaction.response.send_message(f"Numbers-only mode: **{'ON' if on else 'OFF'}**", ephemeral=True)

    @app_commands.command(name="fun_facts", description="Toggle maths fun facts.")
    @app_commands.guild_only()
    async def fun_facts(self, interaction: discord.Interaction, on: bool):
        if not is_admin(interaction):
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        set_facts_on(interaction.guild_id, on)
        await interaction.response.send_message(f"Fun facts: **{'ON' if on else 'OFF'}**", ephemeral=True)

    # Leaderboards / stats
    @app_commands.command(name="leaderboard", description="Show the top counters (with badges).")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = get_leaderboard(interaction.guild_id, 10)
        if not rows:
            return await interaction.response.send_message("No stats yet—start counting!", ephemeral=True)
        lines = []
        for i, r in enumerate(rows, start=1):
            try:
                user = interaction.guild.get_member(r["user_id"]) or await self.bot.fetch_user(r["user_id"])
                name = user.mention if isinstance(user, discord.Member) else f"<@{r['user_id']}>"
            except Exception:
                name = f"<@{r['user_id']}>"
            medal = "👑" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else " "))
            lines.append(f"{medal} **#{i}** {name} — **{r['correct_counts']}** correct • 🏅 {r['badges']} badges")
        em = discord.Embed(title="📈 Top Counters", description="\n".join(lines), colour=discord.Colour.blurple())
        await interaction.response.send_message(embed=em)

    @app_commands.command(name="stats", description="See counting stats for a user.")
    @app_commands.guild_only()
    async def stats(self, interaction: discord.Interaction, user: discord.Member | None = None):
        user = user or interaction.user
        s = get_user_stats(interaction.guild_id, user.id)
        em = discord.Embed(title=f"📊 Stats for {user.display_name}", colour=discord.Colour.green())
        em.add_field(name="Correct", value=str(s["correct_counts"]), inline=True)
        em.add_field(name="Wrong", value=str(s["wrong_counts"]), inline=True)
        em.add_field(name="Best Streak", value=str(s["streak_best"]), inline=True)
        em.add_field(name="Badges", value=str(s["badges"]), inline=True)
        em.set_footer(text=f"Last updated: {s['last_updated'] or '—'}")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(name="streak", description="Show current and best guild streak.")
    @app_commands.guild_only()
    async def streak(self, interaction: discord.Interaction):
        st = get_state(interaction.guild_id)
        await interaction.response.send_message(
            f"🔥 Guild streak: **{st['guild_streak']}** • Best: **{st['best_guild_streak']}**", ephemeral=False
        )

    # Giveaways (random)
    @app_commands.command(name="giveaway_config", description="Set random giveaway range and prize label.")
    @app_commands.describe(range_min="Min steps until a hidden giveaway (default 10)",
                           range_max="Max steps (default 120)",
                           prize="Prize label, e.g. '💎 1000 VU Credits'")
    @app_commands.guild_only()
    async def giveaway_config(self, interaction: discord.Interaction,
                              range_min: int = 10, range_max: int = 120,
                              prize: str = "💎 1000 VU Credits"):
        if not is_admin(interaction):
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        if range_min < 5: range_min = 5
        if range_max <= range_min: range_max = range_min + 1
        with db() as conn:
            conn.execute("""
            UPDATE guild_state
            SET giveaway_range_min=?, giveaway_range_max=?, giveaway_prize=?
            WHERE guild_id=?
            """, (range_min, range_max, prize, interaction.guild_id))
            cur = conn.execute("SELECT current_number FROM guild_state WHERE guild_id=?",(interaction.guild_id,)).fetchone()
            _roll_next_target_after(conn, interaction.guild_id, cur["current_number"])
        await interaction.response.send_message(
            f"🎰 Giveaway armed. Range **{range_min}–{range_max}** • Prize: **{prize}** (target is secret).",
            ephemeral=True
        )

    @app_commands.command(name="giveaway_status", description="Peek giveaway info (admins only, secret).")
    @app_commands.guild_only()
    async def giveaway_status(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        with db() as conn:
            st = conn.execute("""
            SELECT current_number, giveaway_target, giveaway_range_min, giveaway_range_max, last_giveaway_n, giveaway_prize
            FROM guild_state WHERE guild_id=?
            """,(interaction.guild_id,)).fetchone()
        left = (st["giveaway_target"] - st["current_number"]) if st["giveaway_target"] else None
        await interaction.response.send_message(
            f"🔐 Armed.\n"
            f"- Range: **{st['giveaway_range_min']}–{st['giveaway_range_max']}**\n"
            f"- Since last jackpot: **{st['last_giveaway_n']}**\n"
            f"- Prize: **{st['giveaway_prize']}**\n"
            f"- ≈Next in **{left}** steps", ephemeral=True
        )

    @app_commands.command(name="giveaway_now", description="Instant draw among recent correct counters.")
    @app_commands.describe(window="How many recent correct counts to include (default 80)")
    @app_commands.guild_only()
    async def giveaway_now(self, interaction: discord.Interaction, window: int = 80):
        if not is_admin(interaction):
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        gid = interaction.guild_id
        with db() as conn:
            st = conn.execute("SELECT current_number, giveaway_prize FROM guild_state WHERE guild_id=?", (gid,)).fetchone()
            current_n = st["current_number"]
            rows = conn.execute("""
            SELECT DISTINCT user_id FROM count_log
            WHERE guild_id=? AND n>? AND n<=?
            ORDER BY n DESC
            """,(gid, max(0, current_n - window), current_n)).fetchall()
            pool = [r["user_id"] for r in rows]
        if not pool:
            return await interaction.response.send_message("No recent participants to draw from.", ephemeral=True)
        winner = random.choice(pool)
        em = discord.Embed(
            title="⚡ Instant Giveaway",
            description=f"Winner: <@{winner}> — {st['giveaway_prize']} 🎉\n"
                        f"To claim: **DM @mikey.moon on Discord** within 48 hours.",
            colour=discord.Colour.purple()
        )
        await interaction.response.send_message(embed=em, ephemeral=False)

    # Banter JSON management
    @app_commands.command(name="reload_banter", description="Reload banter.json without restarting.")
    @app_commands.guild_only()
    async def reload_banter(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        global BANTER
        BANTER = load_banter()
        await interaction.response.send_message("✅ Banter reloaded.", ephemeral=True)

    # Optional: manual /sync (admin)
    @app_commands.command(name="sync", description="Force re-sync slash commands (admin).")
    @app_commands.guild_only()
    async def sync_cmd(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Nope.", ephemeral=True)
        with contextlib.suppress(Exception):
            synced = await self.bot.tree.sync(guild=interaction.guild)
            return await interaction.response.send_message(f"Resynced {len(synced)} commands to this server.", ephemeral=True)
        await interaction.response.send_message("Sync failed.", ephemeral=True)

async def setup_cog():
    await bot.add_cog(FunCounting(bot))

@bot.event
async def setup_hook():
    await setup_cog()

if __name__ == "__main__":
    bot.run(TOKEN)
