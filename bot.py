# bot.py — Prizo (multi-server, no themes, single settings command) + FIXED-NUMBER GIVEAWAY
import os
import re
import json
import math
import random
import sqlite3
import contextlib
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands


# ========= Basics =========
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ========= Storage =========
DB_PATH = os.getenv("DB_PATH", "counting_fun.db")

def db():
    # Ensure directory exists (handles /data/... or any custom path)
    abs_path = os.path.abspath(DB_PATH)
    dir_path = os.path.dirname(abs_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    conn = sqlite3.connect(abs_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn

# ========= Safe reaction helper =========
async def safe_react(msg: discord.Message, emoji: str):
    try:
        me = msg.guild.me if msg.guild else None
        perms = msg.channel.permissions_for(me) if me else None
        if perms and perms.add_reactions and perms.read_message_history:
            await msg.add_reaction(emoji)
        else:
            await msg.reply(emoji, mention_author=False)
    except Exception:
        with contextlib.suppress(Exception):
            await msg.reply(emoji, mention_author=False)

# ========= DB =========
def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_state (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            current_number INTEGER NOT NULL DEFAULT 0,
            start_number INTEGER NOT NULL DEFAULT 1,
            last_user_id INTEGER,
            numbers_only INTEGER NOT NULL DEFAULT 0,
            facts_on INTEGER NOT NULL DEFAULT 1,
            guild_streak INTEGER NOT NULL DEFAULT 0,
            best_guild_streak INTEGER NOT NULL DEFAULT 0,
            giveaway_target INTEGER,
            giveaway_range_min INTEGER NOT NULL DEFAULT 10,
            giveaway_range_max INTEGER NOT NULL DEFAULT 120,
            last_giveaway_n INTEGER NOT NULL DEFAULT 0,
            giveaway_prize TEXT NOT NULL DEFAULT '💎 500 VU Credits',
            ticket_url TEXT,
            giveaway_mode TEXT NOT NULL DEFAULT 'random',
            giveaway_fixed_max INTEGER
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
        conn.execute("""
        CREATE TABLE IF NOT EXISTS winners (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id     INTEGER NOT NULL,
            channel_id   INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            prize        TEXT    NOT NULL,
            n_won_at     INTEGER NOT NULL,
            created_at   TEXT    NOT NULL,
            UNIQUE(guild_id, n_won_at)
        );
        """)

        # evolve for older DBs (safe if present)
        with contextlib.suppress(Exception):
            conn.execute("ALTER TABLE guild_state ADD COLUMN giveaway_open INTEGER NOT NULL DEFAULT 1")
        with contextlib.suppress(Exception):
            conn.execute("ALTER TABLE guild_state ADD COLUMN winner_user_id INTEGER")
        with contextlib.suppress(Exception):
            conn.execute("ALTER TABLE guild_state ADD COLUMN ticket_category_id INTEGER")
        with contextlib.suppress(Exception):
            conn.execute("ALTER TABLE guild_state ADD COLUMN ticket_staff_role_id INTEGER")


# ========= Helpers =========
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
        conn.execute(
            "UPDATE guild_state SET current_number=?, last_user_id=NULL, guild_streak=0 WHERE guild_id=?",
            (row["start_number"] - 1, gid)
        )

def set_numbers_only(gid: int, flag: bool):
    with db() as conn:
        conn.execute("UPDATE guild_state SET numbers_only=? WHERE guild_id=?", (1 if flag else 0, gid))

def set_facts_on(gid: int, flag: bool):
    with db() as conn:
        conn.execute("UPDATE guild_state SET facts_on=? WHERE guild_id=?", (1 if flag else 0, gid))

def get_ticket_url(gid: int) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT ticket_url FROM guild_state WHERE guild_id=?", (gid,)).fetchone()
        return row["ticket_url"] if row else None

def get_ticket_cfg(gid: int) -> tuple[int | None, int | None]:
    with db() as conn:
        row = conn.execute(
            "SELECT ticket_category_id, ticket_staff_role_id FROM guild_state WHERE guild_id=?",
            (gid,)
        ).fetchone()
        if not row:
            return None, None
        return row["ticket_category_id"], row["ticket_staff_role_id"]

# ========= Views (Ticket Buttons) =========
class OpenTicketPersistent(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 Open Ticket", style=discord.ButtonStyle.green, custom_id="prizo_open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # --- parse the announce embed for winner/number/prize ---
        if not interaction.message.embeds:
            return await interaction.response.send_message("No prize info found on this message.", ephemeral=True)
        emb = interaction.message.embeds[0]
        desc = (emb.description or "")

        m_user = re.search(r"Winner:\s*<@(\d+)>", desc)
        m_num  = re.search(r"(?:Target:?|Number)\s*\*{2}(\d+)\*{2}", desc)
        if not (m_user and m_num):
            return await interaction.response.send_message("Couldn't read winner/number from this message.", ephemeral=True)

        winner_id = int(m_user.group(1))
        n_hit     = int(m_num.group(1))
        if interaction.user.id != winner_id:
            return await interaction.response.send_message("Only the winner can open this ticket.", ephemeral=True)

        m_prize = re.search(r"Winner:\s*<@\d+>\s*—\s*(.+?)\s", desc)
        prize = (m_prize.group(1).strip() if m_prize else "🎁 Surprise Gift")

        # --- de-dupe: if a ticket with the same name already exists, just point to it ---
        name = f"ticket-{interaction.user.name.lower()}-{n_hit}"
        existing = discord.utils.get(interaction.guild.text_channels, name=name)
        if existing:
            await interaction.response.send_message(f"✅ Ticket already exists: {existing.mention}", ephemeral=True)
            with contextlib.suppress(Exception):
                button.disabled = True
                await interaction.message.edit(view=self)
            return

        # --- try to create; if forbidden, fall back to set_ticket URL ---
        try:
            chan = await create_winner_ticket(interaction.guild, interaction.user, prize, n_hit)
        except discord.Forbidden:
            turl = get_ticket_url(interaction.guild_id)
            if turl:
                view = discord.ui.View()
                view.add_item(discord.ui.Button(label="🎫 Open Ticket (Link)", url=turl))
                return await interaction.response.send_message(
                    "I couldn’t create a channel due to category permissions. Use the link below:",
                    view=view, ephemeral=True
                )
            return await interaction.response.send_message(
                "I need **Manage Channels** permission on the ticket category to create tickets.",
                ephemeral=True
            )
        except Exception as e:
            return await interaction.response.send_message(f"Ticket creation failed: {e}", ephemeral=True)

        # Success: confirm first, then try to disable button (ignore failures)
        await interaction.response.send_message(f"✅ Ticket created: {chan.mention}", ephemeral=True)
        with contextlib.suppress(Exception):
            button.disabled = True
            await interaction.message.edit(view=self)

async def create_winner_ticket(
    guild: discord.Guild,
    winner: discord.Member,
    prize: str,
    n_hit: int
) -> discord.TextChannel:
    """Create a private ticket channel visible to winner + staff role, under configured category."""
    cat_id, staff_role_id = get_ticket_cfg(guild.id)
    category = guild.get_channel(cat_id) if cat_id else None
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None

    # Make sure we have a full Member object
    if not isinstance(winner, discord.Member):
        winner = guild.get_member(winner.id) or await guild.fetch_member(winner.id)

    # Build explicit overwrites (deny @everyone; allow winner, bot, staff)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        winner: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(  # ensure the bot always has access
            view_channel=True, send_messages=True, read_message_history=True, manage_channels=True
        ),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, manage_messages=True
        )

    name = f"ticket-{winner.name.lower()}-{n_hit}"

    # Create the channel (unsynced from category by virtue of custom overwrites)
    chan = await guild.create_text_channel(
        name=name,
        category=category,
        overwrites=overwrites,
        reason="Prizo prize ticket"
    )

    # 🔒 Double-ensure permissions in case category sync/overrides interfered
    await chan.set_permissions(
        winner, view_channel=True, send_messages=True, read_message_history=True
    )
    if staff_role:
        await chan.set_permissions(
            staff_role, view_channel=True, send_messages=True, read_message_history=True, manage_messages=True
        )
    await chan.set_permissions(
        guild.default_role, view_channel=False
    )
    await chan.set_permissions(
        guild.me, view_channel=True, send_messages=True, read_message_history=True, manage_channels=True
    )

    # (Optional) make sure the channel stays **unsynced** from category so these overwrites persist
    with contextlib.suppress(Exception):
        await chan.edit(sync_permissions=False)

    # Welcome embed
    em = discord.Embed(
        title="🎟️ Prize Ticket",
        description=(
            f"🎟 Ticket for {winner.mention}\n\n"
            f"Please provide:\n"
            f"• **IMVU Account Link:**\n"
            f"• **Lucky Number Won:** {n_hit}\n"
            f"• **Prize Claim Notes:**preferred delivery method (credits or gift)(\n\n"
            "Mikey.Moon will review, shut this ticket down, and fire the creds to your VU account, try not to blow it all on dodgy shoes. 😏."
        ),
        colour=discord.Colour.green()
    )
    em.set_footer(text=f"{guild.name} • Ticket")
    await chan.send(embed=em)
    return chan


def get_fixed_max(gid: int) -> int | None:
    with db() as conn:
        row = conn.execute("SELECT giveaway_fixed_max FROM guild_state WHERE guild_id=?", (gid,)).fetchone()
        return row["giveaway_fixed_max"] if row else None


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

# ========= Fun facts / banter =========
BANTER_PATH = os.getenv("BANTER_PATH", "banter.json")
def load_banter():
    try:
        with open(BANTER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k in ("wrong", "winner", "milestone", "roast", "nonnumeric", "claim"):
                data.setdefault(k, [])
            return data
    except Exception:
        return {"wrong": [], "winner": [], "milestone": [], "roast": [], "nonnumeric": [], "claim": []}
BANTER = load_banter()

def pick_banter(cat: str) -> str:
    lines = BANTER.get(cat, [])
    return random.choice(lines) if lines else ""

FUNFACTS_PATH = os.getenv("FUNFACTS_PATH", "funfacts.json")
def load_funfacts():
    try:
        with open(FUNFACTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
FUNFACTS = load_funfacts()

def pick_fact(category: str, n: int) -> str | None:
    lines = FUNFACTS.get(category, [])
    if isinstance(lines, list) and lines:
        return random.choice(lines).replace("{n}", str(n))
    return None

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
    special = FUNFACTS.get("funny", {})
    if str(n) in special:
        return random.choice(special[str(n)]).replace("{n}", str(n))
    if is_palindrome(n):
        return pick_fact("palindrome", n)
    if is_prime(n):
        return pick_fact("prime", n)
    if n % 100 == 0:
        return pick_fact("multiple100", n)
    if n % 10 == 0:
        return pick_fact("multiple10", n)
    return None

# ========= Milestones =========
MILESTONES = {10, 20, 25, 30, 40, 50, 69, 75, 80, 90, 100, 111, 123, 150, 200, 250,
              300, 333, 369, 400, 420, 500, 600, 666, 700, 750, 800, 900, 999, 1000}

# ========= Giveaways =========
# ONE draw per round; target = current + randint(min,max)
def _roll_next_target_after(conn, guild_id: int, current_number: int):
    row = conn.execute(
        "SELECT giveaway_range_min, giveaway_range_max FROM guild_state WHERE guild_id=?",
        (guild_id,)
    ).fetchone()

    # ✅ Respect exact config, allow 1..N
    lo = int(row["giveaway_range_min"] or 1)
    hi = int(row["giveaway_range_max"] or (lo + 1))

    if lo < 1:
        lo = 1
    if hi < lo:
        hi = lo  # allow fixed step if equal

    delta = random.randint(lo, hi)  # inclusive
    target = int(current_number) + delta

    conn.execute(
        "UPDATE guild_state SET giveaway_target=?, giveaway_mode='random' WHERE guild_id=?",
        (target, guild_id)
    )

def ensure_giveaway_target(gid: int):
    """Only re-arm if target is missing or already passed."""
    with db() as conn:
        row = conn.execute(
            "SELECT current_number, giveaway_target FROM guild_state WHERE guild_id=?", (gid,)
        ).fetchone()
        if row["giveaway_target"] is None or row["giveaway_target"] < row["current_number"]:
            _roll_next_target_after(conn, gid, row["current_number"])
            return conn.execute(
                "SELECT giveaway_target FROM guild_state WHERE guild_id=?", (gid,)
            ).fetchone()["giveaway_target"]
        return row["giveaway_target"]

async def try_giveaway_draw(bot: commands.Bot, message: discord.Message, reached_n: int):
    gid = message.guild.id

    # ----- phase 1: read state & decide winner (no side-effects yet) -----
    with db() as conn:
        st = conn.execute("""
            SELECT giveaway_target, last_giveaway_n, giveaway_prize, giveaway_mode
            FROM guild_state WHERE guild_id=?
        """,(gid,)).fetchone()

        target = st["giveaway_target"]
        mode = (st["giveaway_mode"] or "random").lower()

        # Only fire on exact target
        if target is None or reached_n != target:
            if target is None:
                _roll_next_target_after(conn, gid, reached_n)
            return False

        prize = st["giveaway_prize"] or "🎁 Surprise Gift"

        # ✅ Winner is ALWAYS the author who hit the target (both modes)
        chosen_winner_id = message.author.id

    # ----- phase 2: single-winner lock (transaction) -----
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM winners WHERE guild_id=? AND n_won_at=?",
            (gid, reached_n)
        ).fetchone()
        if existing:
            conn.execute("COMMIT")
            return False

        conn.execute("""
            INSERT INTO winners (guild_id, channel_id, user_id, prize, n_won_at, created_at)
            VALUES (?,?,?,?,?,?)
        """, (gid, message.channel.id, chosen_winner_id, prize, reached_n, datetime.utcnow().isoformat()))

        if mode == "fixed":
            conn.execute(
                "UPDATE guild_state SET last_giveaway_n=?, giveaway_mode='random', giveaway_target=NULL WHERE guild_id=?",
                (reached_n, gid)
            )
        else:
            conn.execute("UPDATE guild_state SET last_giveaway_n=? WHERE guild_id=?", (reached_n, gid))

        _roll_next_target_after(conn, gid, reached_n)
        conn.execute("COMMIT")

    # ----- phase 3: announce + persistent ticket button -----
    winner_mention = f"<@{chosen_winner_id}>"
    winner_banter = pick_banter("winner") or "Legend behaviour. Take a bow. 👑"
    title = "🎯 Fixed Milestone Win!" if mode == "fixed" else "🎲 Random Giveaway!"
    claim_text = "Click **Open Ticket** below to claim within 48h. 💬"

    embed = discord.Embed(
        title=title,
        description=(
            f"Target **{reached_n}** hit!\n"
            f"Winner: {winner_mention} — {prize} 🥳\n\n"
            f"**{winner_banter}**\n"
            f"{claim_text}\n\n"
            f"*New jackpot is armed… keep counting.*"
        ),
        colour=discord.Colour.gold()
    )
    embed.set_footer(text="Jackpot Announcement")

    view = OpenTicketPersistent()
    await message.channel.send(embed=embed, view=view)

    # Optional DM to winner
    try:
        winner_user = message.guild.get_member(chosen_winner_id) or await bot.fetch_user(chosen_winner_id)
        await winner_user.send(
            f"🎉 You won in {message.channel.mention} at **{reached_n}**!\n"
            f"Prize: {prize}\n"
            f"Use the **Open Ticket** button in the channel to claim."
        )
    except Exception:
        pass

    return True

# ========= Events =========
@bot.event
async def on_ready():
    init_db()

    # Register the persistent ticket button (works across restarts)
    try:
        bot.add_view(OpenTicketPersistent())
    except Exception as e:
        print("Failed to add persistent view:", e)

    # runtime trackers — per guild
    bot.locked_players = {}   # {guild_id: {user_id: unlock_dt}}
    bot.last_poster = {}      # {guild_id: {"user_id": int|None, "count": int}}
    try:
        synced = await bot.tree.sync()
        print(f"Globally synced {len(synced)} commands ✅")
    except Exception as e:
        print("Global sync error:", e)
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print("DB_PATH:", DB_PATH)

@bot.event
async def on_guild_join(guild: discord.Guild):
    with contextlib.suppress(Exception):
        await bot.tree.sync(guild=guild)
        print(f"Per-guild synced commands to {guild.id}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    st = get_state(message.guild.id)
    if not st["channel_id"] or message.channel.id != st["channel_id"]:
        return

    # numbers-only extract (or loose: number must be at start)
    INT_STRICT = re.compile(r"^\s*(-?\d+)\s*$")
    INT_LOOSE  = re.compile(r"^\s*(-?\d+)\b")
    def extract_int(text: str, strict: bool):
        m = (INT_STRICT if strict else INT_LOOSE).match(text)
        return int(m.group(1)) if m else None

    posted = extract_int(message.content, strict=bool(st["numbers_only"]))
    if posted is None:
        banter = (pick_banter("nonnumeric") or "Numbers only in here, mate.").replace("{user}", message.author.mention)
        with contextlib.suppress(Exception):
            await message.reply(banter)
        return

    expected = st["current_number"] + 1
    last_user = st["last_user_id"]

    # per-guild runtime trackers
    gid = message.guild.id
    locks = bot.locked_players.setdefault(gid, {})               # {user_id: unlock_dt}
    lp = bot.last_poster.setdefault(gid, {"user_id": None, "count": 0})

    # --- timeout check ---
    now = datetime.utcnow()
    if message.author.id in locks:
        if now < locks[message.author.id]:
            with contextlib.suppress(Exception):
                await message.delete()
            return
        else:
            del locks[message.author.id]

    # --- consecutive 3-in-a-row tracking (by author) ---
    if lp["user_id"] == message.author.id:
        lp["count"] += 1
    else:
        lp["user_id"] = message.author.id
        lp["count"] = 1
    if lp["count"] >= 3:
        locks[message.author.id] = now + timedelta(minutes=10)
        roast = pick_banter("roast") or "Greedy digits get locked, enjoy the bench. 🏀"
        with contextlib.suppress(Exception):
            await safe_react(message, "⛔")
            await message.reply(
                f"⛔ {message.author.mention} tried **3 in a row**. Locked for **10 minutes**. {roast}"
            )
        lp["user_id"] = None
        lp["count"] = 0
        return

    # --- rule: no double posts ---
    if last_user == message.author.id:
        mark_wrong(message.guild.id, message.author.id)
        await safe_react(message, "⛔")
        banter = (pick_banter("wrong") or "Not two in a row. Behave. 😅").replace("{n}", str(expected))
        with contextlib.suppress(Exception):
            await message.reply(
                f"Not two in a row, {message.author.mention}. {banter} Next is **{expected}** for someone else."
            )
        return

    # --- rule: must be exact next number ---
    if posted != expected:
        mark_wrong(message.guild.id, message.author.id)
        await safe_react(message, "❌")
        banter = pick_banter("wrong") or "Oof—maths says ‘nah’. 📏"
        with contextlib.suppress(Exception):
            await message.reply(f"{banter} Next up is **{expected}**.")
        return

    # --- success! ---
    bump_ok(message.guild.id, message.author.id)
    await safe_react(message, "✅")

    # log for giveaway eligibility
    with contextlib.suppress(Exception):
        log_correct_count(message.guild.id, expected, message.author.id)

    # milestones
    if expected in MILESTONES:
        em = discord.Embed(
            title="🎉 Party Mode 🎉",
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

    # hidden giveaway target maintenance
    ensure_giveaway_target(message.guild.id)

    # >>> If target just got hit, perform the draw and announce (random or fixed)
    st_now = get_state(message.guild.id)
    target_now = st_now["giveaway_target"]
    if target_now is not None and expected == int(target_now):
        did_announce = await try_giveaway_draw(bot, message, expected)
        if did_announce:
            return


# ========= Slash Commands =========
class FunCounting(commands.Cog):
    def __init__(self, b: commands.Bot):
        self.bot = b

    @app_commands.command(name="set_ticket_category", description="Set the category where winner tickets will be created.")
    @app_commands.guild_only()
    async def set_ticket_category(self, interaction: discord.Interaction, category: discord.CategoryChannel):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        with db() as conn:
            conn.execute("UPDATE guild_state SET ticket_category_id=? WHERE guild_id=?", (category.id, interaction.guild_id))
        await interaction.response.send_message(f"📂 Ticket category set to **{category.name}**.", ephemeral=True)

    @app_commands.command(name="set_ticket_staffrole", description="Set the staff role that can view/manage winner tickets.")
    @app_commands.guild_only()
    async def set_ticket_staffrole(self, interaction: discord.Interaction, role: discord.Role):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        with db() as conn:
            conn.execute("UPDATE guild_state SET ticket_staff_role_id=? WHERE guild_id=?", (role.id, interaction.guild_id))
        await interaction.response.send_message(f"🛡️ Ticket staff role set to {role.mention}.", ephemeral=True)

    @app_commands.command(name="settings_counting", description="Set the counting channel and starting number.")
    @app_commands.describe(channel="Channel to count in", start="Start number (default 1)")
    @app_commands.guild_only()
    async def settings_counting(self, interaction: discord.Interaction, channel: discord.TextChannel, start: int = 1):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        set_channel(interaction.guild_id, channel.id)
        set_start(interaction.guild_id, start)
        st = get_state(interaction.guild_id)
        await interaction.response.send_message(
            f"🎯 Counting set in {channel.mention} from **{start}**. "
            f"Numbers-only: **{'ON' if st['numbers_only'] else 'OFF'}**, Fun facts: **{'ON' if st['facts_on'] else 'OFF'}**.",
            ephemeral=True
        )

    @app_commands.command(name="numbers_only", description="Toggle numbers-only mode.")
    @app_commands.guild_only()
    async def numbers_only_cmd(self, interaction: discord.Interaction, on: bool):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        set_numbers_only(interaction.guild_id, on)
        await interaction.response.send_message(f"Numbers-only mode: **{'ON' if on else 'OFF'}**", ephemeral=True)

    @app_commands.command(name="fun_facts", description="Toggle maths fun facts.")
    @app_commands.guild_only()
    async def fun_facts_cmd(self, interaction: discord.Interaction, on: bool):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        set_facts_on(interaction.guild_id, on)
        await interaction.response.send_message(f"Fun facts: **{'ON' if on else 'OFF'}**", ephemeral=True)

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
            lines.append(f"{medal} **#{i}** {name} — **{r['correct_counts']}** correct • 🏅 {r['badges']}")
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

    # Giveaways (random config)
    @app_commands.command(name="giveaway_config", description="Set random giveaway range and prize label.")
    @app_commands.describe(range_min="Min steps until a hidden giveaway (e.g. 1)",
                           range_max="Max steps (e.g. 6)",
                           prize="Prize label, e.g. '💎 1000 VU Credits'")
    @app_commands.guild_only()
    async def giveaway_config(self, interaction: discord.Interaction,
                              range_min: int = 10, range_max: int = 120,
                              prize: str = "💎 1000 VU Credits"):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        # ✅ allow exact 1..N ranges
        if range_min < 1: range_min = 1
        if range_max < range_min: range_max = range_min
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO guild_state (guild_id) VALUES (?)", (interaction.guild_id,))
            conn.execute("""
                UPDATE guild_state
                SET giveaway_range_min=?,
                    giveaway_range_max=?,
                    giveaway_prize=?,
                    giveaway_mode='random'
                WHERE guild_id=?
            """, (range_min, range_max, prize, interaction.guild_id))
            cur = conn.execute("""
                SELECT COALESCE(current_number, 0) AS current_number
                FROM guild_state WHERE guild_id=?
            """, (interaction.guild_id,)).fetchone()
            _roll_next_target_after(conn, interaction.guild_id, cur["current_number"])

        await interaction.response.send_message(
            f"🎰 Giveaway armed. Range **{range_min}–{range_max}** • Prize: **{prize}** (target is secret).",
            ephemeral=True
        )

    @app_commands.command(name="set_ticket", description="Set the server ticket link for prize claims (fallback).")
    @app_commands.guild_only()
    async def set_ticket(self, interaction: discord.Interaction, url: str):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        with db() as conn:
            conn.execute("UPDATE guild_state SET ticket_url=? WHERE guild_id=?", (url, interaction.guild_id))
        await interaction.response.send_message(f"🎫 Ticket link set. Claims will point to: {url}", ephemeral=True)

    @app_commands.command(name="giveaway_status", description="Peek giveaway info (admins only).")
    @app_commands.guild_only()
    async def giveaway_status(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        with db() as conn:
            st = conn.execute("""
            SELECT current_number, giveaway_target, giveaway_range_min, giveaway_range_max, last_giveaway_n, giveaway_prize, ticket_url, giveaway_mode
            FROM guild_state WHERE guild_id=?
            """,(interaction.guild_id,)).fetchone()
        mode = (st["giveaway_mode"] or "random").lower()
        turl = st["ticket_url"] or "— not set —"
        lines = [
            "🔐 Armed.",
            f"- Mode: **{mode}**",
            f"- Range: **{st['giveaway_range_min']}–{st['giveaway_range_max']}**",
            f"- Since last jackpot: **{st['last_giveaway_n']}**",
            f"- Prize: **{st['giveaway_prize']}**",
            f"- Ticket link: {turl}",
        ]
        if mode == "random":
            left = (st["giveaway_target"] or 0) - (st["current_number"] or 0)
            lines.append(f"- ≈Next in **{max(0, left)}** steps")
        else:
            lines.append(f"- 🎯 Lucky number: **hidden ahead**")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="giveaway_now", description="Instant draw among recent correct counters.")
    @app_commands.describe(window="How many recent correct counts to include (default 80)")
    @app_commands.guild_only()
    async def giveaway_now(self, interaction: discord.Interaction, window: int = 80):
        if not interaction.user.guild_permissions.manage_guild:
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

        ticket_url = get_ticket_url(gid)
        claim_text = pick_banter("claim") or "To claim your prize: **DM @mikey.moon on Discord** within 48 hours. 💬"

        em = discord.Embed(
            title="⚡ Instant Giveaway",
            description=f"Winner: <@{winner}> — {st['giveaway_prize']} 🎉\n{claim_text}",
            colour=discord.Colour.purple()
        )
        if ticket_url:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="🎫 Open Ticket", url=ticket_url))
            await interaction.response.send_message(embed=em, view=view, ephemeral=False)
        else:
            await interaction.response.send_message(embed=em, ephemeral=False)

    @app_commands.command(name="giveaway_fixed", description="Arm a fixed-number jackpot hidden within the next N counts.")
    @app_commands.guild_only()
    async def giveaway_fixed(self, interaction: discord.Interaction, number: int, prize: str = "💎 500 VU Credits"):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        if number < 2:
            return await interaction.response.send_message("Number must be **≥ 2**.", ephemeral=True)

        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO guild_state (guild_id) VALUES (?)", (interaction.guild_id,))
            row = conn.execute(
                "SELECT COALESCE(current_number, 0) AS current_number FROM guild_state WHERE guild_id=?",
                (interaction.guild_id,)
            ).fetchone()
            current_n = row["current_number"]
            delta = random.randint(1, number)
            lucky_abs = current_n + delta
            conn.execute("""
                UPDATE guild_state
                SET giveaway_target=?,
                    giveaway_prize=?,
                    giveaway_mode='fixed',
                    giveaway_open=1,
                    winner_user_id=NULL,
                    giveaway_fixed_max=?
                WHERE guild_id=?
            """, (lucky_abs, prize, number, interaction.guild_id))

        await interaction.response.send_message(
            f"🎲 Lucky number armed somewhere in the next **{number}** counts.\n"
            f"First to hit it wins **{prize}**.",
            ephemeral=True
        )

    @app_commands.command(name="giveaway_fixed_off", description="Disable fixed-number prize and return to random mode.")
    @app_commands.guild_only()
    async def giveaway_fixed_off(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        with db() as conn:
            row = conn.execute("SELECT current_number FROM guild_state WHERE guild_id=?", (interaction.guild_id,)).fetchone()
            conn.execute("UPDATE guild_state SET giveaway_mode='random', giveaway_target=NULL WHERE guild_id=?", (interaction.guild_id,))
            _roll_next_target_after(conn, interaction.guild_id, row["current_number"])
        await interaction.response.send_message("✅ Fixed-number mode **OFF**. Random jackpot re-armed.", ephemeral=True)

    @app_commands.command(name="reload_banter", description="Reload banter.json without restarting.")
    @app_commands.guild_only()
    async def reload_banter(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        global BANTER
        BANTER = load_banter()
        await interaction.response.send_message("✅ Banter reloaded.", ephemeral=True)

    @app_commands.command(name="ticket_diag", description="Show Prizo's effective permissions in the ticket category.")
    @app_commands.guild_only()
    async def ticket_diag(self, interaction: discord.Interaction):
        st = get_state(interaction.guild_id)
        cat_id = st["ticket_category_id"]
        cat = interaction.guild.get_channel(cat_id) if cat_id else None

        me = interaction.guild.me
        g = me.guild_permissions
        lines = [
            f"Guild perms: manage_channels={g.manage_channels}, view_channel={g.view_channel}, send_messages={g.send_messages}"
        ]

        if not cat:
            lines.append("No ticket category set or I can't see it. Use /set_ticket_category.")
            return await interaction.response.send_message("\n".join(lines), ephemeral=True)

        p = cat.permissions_for(me)
        lines.append(f"Category perms ({cat.name}): manage_channels={p.manage_channels}, view_channel={p.view_channel}, send_messages={p.send_messages}")

        try:
            tmp = await interaction.guild.create_text_channel(name="prizo-perm-test", category=cat, reason="diagnostic")
            await tmp.delete(reason="diagnostic cleanup")
            lines.append("Create/delete test: ✅ success")
        except discord.Forbidden:
            lines.append("Create/delete test: ❌ FORBIDDEN (missing Manage Channels or category override)")
        except Exception as e:
            lines.append(f"Create/delete test: ❌ {type(e).__name__}: {e}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

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
