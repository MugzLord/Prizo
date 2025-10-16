# bot.py — Prizo (fresh build)
# Features: numbers/letters counting, random & fixed jackpots + ticketing,
# tournaments (cap + silent-after-cap), AI idle banter, maths facts/badges,
# user/guild stats, hardened SQLite (WAL + busy_timeout), hot-reload banter.

import os
import re
import json
import math
import random
import sqlite3
import contextlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ==================== ENV & CONSTANTS ====================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN")

DB_PATH = os.getenv("DB_PATH", "counting_fun.db")
APP_DIR = Path(__file__).resolve().parent
BANTER_PATH = Path(os.getenv("BANTER_PATH", APP_DIR / "banter.json"))
FUNFACTS_PATH = Path(os.getenv("FUNFACTS_PATH", APP_DIR / "funfacts.json"))

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# ==================== RUNTIME (per guild) ====================
wrong_entries = defaultdict(int)                      # 5 wrongs => reset
_wrong_streak = defaultdict(int)                      # key=(gid,cid,uid) 3 wrongs => bench
ai_helper_enabled     = defaultdict(lambda: True)
AI_DEFAULT_IDLE_MIN = 3
AI_MAX_REPLIES_PER_BANTER = 2
AI_REPLY_COOLDOWN_SEC = 5
ai_idle_minutes       = defaultdict(lambda: AI_DEFAULT_IDLE_MIN)
last_count_activity   = defaultdict(lambda: datetime.now(timezone.utc))
last_ai_message       = defaultdict(lambda: None)
ai_reply_counts       = defaultdict(int)
ai_reply_next_allowed = defaultdict(lambda: datetime.now(timezone.utc))

# Idle banter rotation buckets
_idle_bucket = defaultdict(list)

# ==================== DB ====================
def db():
    abs_path = os.path.abspath(DB_PATH)
    dir_path = os.path.dirname(abs_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    conn = sqlite3.connect(abs_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
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
            giveaway_fixed_max INTEGER,
            giveaway_open INTEGER NOT NULL DEFAULT 1,
            winner_user_id INTEGER,
            ticket_category_id INTEGER,
            ticket_staff_role_id INTEGER,
            count_mode TEXT NOT NULL DEFAULT 'numbers',
            current_letter TEXT NOT NULL DEFAULT 'A',
            ban_minutes INTEGER NOT NULL DEFAULT 1,
            count_paused INTEGER NOT NULL DEFAULT 0
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
        conn.execute("""
        CREATE TABLE IF NOT EXISTS tournaments(
            guild_id     INTEGER PRIMARY KEY,
            is_active    INTEGER NOT NULL DEFAULT 0,
            ends_at_utc  TEXT,
            fixed_reward INTEGER NOT NULL DEFAULT 1000,
            max_jackpots INTEGER NOT NULL DEFAULT 5,
            jackpots_hit INTEGER NOT NULL DEFAULT 0,
            silent_after_limit INTEGER NOT NULL DEFAULT 1
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS tournament_wins(
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            wins     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        """)

# ==================== UTILITIES ====================
def now_utc(): return datetime.now(timezone.utc)
def now_iso(): return datetime.utcnow().isoformat()
def iso(dt: datetime) -> str: return dt.astimezone(timezone.utc).isoformat()
def parse_iso(s: str | None):
    if not s: return None
    try: return datetime.fromisoformat(s)
    except Exception: return None

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
        conn.execute("UPDATE guild_state SET current_letter='A' WHERE guild_id=?", (gid,))

def set_numbers_only(gid: int, flag: bool):
    with db() as conn:
        conn.execute("UPDATE guild_state SET numbers_only=? WHERE guild_id=?", (1 if flag else 0, gid))

def set_count_paused(gid: int, flag: bool):
    with db() as conn:
        conn.execute("UPDATE guild_state SET count_paused=? WHERE guild_id=?", (1 if flag else 0, gid))

def set_facts_on(gid: int, flag: bool):
    with db() as conn:
        conn.execute("UPDATE guild_state SET facts_on=? WHERE guild_id=?", (1 if flag else 0, gid))

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
        """, (correct, wrong, new_streak, new_badges, now, gid, uid)

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
        conn.execute("UPDATE guild_state SET guild_streak=0 WHERE guild_id=?", (gid,)

def log_correct_count(gid: int, n: int, uid: int):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO count_log (guild_id, n, user_id, ts) VALUES (?,?,?,?)",
            (gid, n, uid, now_iso())
        )

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

# ==================== BANTER & FACTS ====================
BANTER = {}
FUNFACTS = {}
BANTER_VER = "unknown"
_BANTER_MTIME = 0.0

def _banter_ver(raw_bytes: bytes) -> str:
    import hashlib
    return hashlib.sha1(raw_bytes).hexdigest()[:8]

def load_banter():
    global BANTER, BANTER_VER, _BANTER_MTIME
    try:
        raw = BANTER_PATH.read_bytes()
        BANTER = json.loads(raw.decode("utf-8"))
        for k in ("wrong","winner","milestone","roast","nonnumeric","claim",
                  "idle_banter","idle_banter_replies","idle_replies"):
            BANTER.setdefault(k, [])
        BANTER_VER = _banter_ver(raw)
        _BANTER_MTIME = BANTER_PATH.stat().st_mtime
        print(f"[BANTER] Loaded {BANTER_PATH} v{BANTER_VER}")
    except Exception as e:
        print(f"[BANTER] FAILED: {e}")
        BANTER = { "wrong": [], "winner": [], "milestone": [], "roast": [],
                   "nonnumeric": [], "claim": [],
                   "idle_banter": [], "idle_banter_replies": [], "idle_replies": [] }
        BANTER_VER = "unknown"

def load_funfacts():
    global FUNFACTS
    try:
        FUNFACTS = json.loads(FUNFACTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        FUNFACTS = {}

def _banter_summary():
    return {
        "idle_banter": len(BANTER.get("idle_banter", [])),
        "idle_banter_replies": len(BANTER.get("idle_banter_replies", [])),
        "wrong": len(BANTER.get("wrong", [])),
        "milestone": len(BANTER.get("milestone", [])),
        "winner": len(BANTER.get("winner", [])),
        "ver": BANTER_VER,
    }

def pick_banter(cat: str) -> str:
    lines = BANTER.get(cat, [])
    return random.choice(lines) if lines else ""

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

def _idle_lines():
    return BANTER.get("idle_banter") or ["…silence…"]

def _next_idle_line(gid: int) -> str:
    bucket = _idle_bucket[gid]
    if not bucket:
        bucket[:] = list(_idle_lines())
        random.shuffle(bucket)
    return bucket.pop()

# ==================== LETTERS HELPERS ====================
LETTER_STRICT = re.compile(r"^\s*([A-Za-z])\s*$")
LETTER_LOOSE  = re.compile(r"^\s*([A-Za-z])")

def extract_letter(text: str, strict: bool) -> str | None:
    m = (LETTER_STRICT if strict else LETTER_LOOSE).match(text or "")
    return m.group(1).upper() if m else None

def next_letter_dir(gid: int, letter: str | None) -> str:
    try:
        is_active, ends_at, *_ = get_tourney(gid)
        rev = bool(is_active and parse_iso(ends_at) and now_utc() < parse_iso(ends_at))
    except Exception:
        rev = False
    if not letter:
        return "Z" if rev else "A"
    c = (letter or "A").upper()
    if rev:
        return "Z" if c <= "A" else chr(ord(c) - 1)
    else:
        return "A" if c >= "Z" else chr(ord(c) + 1)

def bump_ok_letter(gid: int, uid: int, expected_letter: str):
    with db() as conn:
        st = get_state(gid)
        new_streak = st["guild_streak"] + 1
        best = max(new_streak, st["best_guild_streak"])
        nxt = next_letter_dir(gid, expected_letter)
        conn.execute("""
            UPDATE guild_state
            SET current_letter=?,
                current_number = current_number + 1,
                last_user_id=?,
                guild_streak=?,
                best_guild_streak=?
            WHERE guild_id=?;
        """, (nxt, uid, new_streak, best, gid))
        _touch_user(conn, gid, uid, correct=1, streak_best=new_streak)

def reset_letters(gid: int, start: str = "A"):
    with db() as conn:
        conn.execute("UPDATE guild_state SET current_letter=?, last_user_id=NULL, guild_streak=0 WHERE guild_id=?",
                     (start.upper(), gid))

# ==================== TOURNAMENTS ====================
def get_tourney(gid: int):
    import time
    with db() as con:
        row = con.execute("""
            SELECT is_active, ends_at_utc, fixed_reward, max_jackpots, jackpots_hit, silent_after_limit
            FROM tournaments WHERE guild_id=?
        """, (gid,)).fetchone()
        if not row:
            for _ in range(5):
                try:
                    con.execute("INSERT OR IGNORE INTO tournaments(guild_id) VALUES(?)", (gid,))
                    break
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower():
                        time.sleep(0.2); continue
                    raise
            return (0, None, 1000, 5, 0, 1)
        return (row["is_active"], row["ends_at_utc"], row["fixed_reward"],
                row["max_jackpots"], row["jackpots_hit"], row["silent_after_limit"])

def set_tourney(gid: int, **fields):
    if not fields: return
    cols = ", ".join(f"{k}=?" for k in fields.keys())
    vals = list(fields.values()) + [gid]
    with db() as con:
        con.execute(f"UPDATE tournaments SET {cols} WHERE guild_id=?", vals)

def add_tourney_win(gid: int, uid: int, n: int = 1):
    with db() as con:
        con.execute("""
            INSERT INTO tournament_wins(guild_id, user_id, wins) VALUES(?,?,?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET wins = wins + excluded.wins
        """, (gid, uid, n))

def reset_tourney_wins(gid: int):
    with db() as con:
        con.execute("DELETE FROM tournament_wins WHERE guild_id=?", (gid,))

def top_wins(gid: int, limit: int = 10):
    with db() as con:
        return con.execute("""
            SELECT user_id, wins FROM tournament_wins
            WHERE guild_id=? ORDER BY wins DESC, user_id ASC LIMIT ?
        """, (gid, limit)).fetchall()

# ==================== SAFE REACT ====================
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

# ==================== GIVEAWAYS ====================
def _roll_next_target_after(conn, gid: int, current_number: int):
    row = conn.execute(
        "SELECT giveaway_range_min, giveaway_range_max FROM guild_state WHERE guild_id=?",
        (gid,)
    ).fetchone()
    lo = int(row["giveaway_range_min"] or 1)
    hi = int(row["giveaway_range_max"] or (lo + 1))
    if lo < 1: lo = 1
    if hi < lo: hi = lo
    delta = random.randint(lo, hi)
    target = int(current_number) + delta
    conn.execute(
        "UPDATE guild_state SET giveaway_target=?, giveaway_mode='random' WHERE guild_id=?",
        (target, gid)
    )

def ensure_giveaway_target(gid: int):
    with db() as conn:
        row = conn.execute(
            "SELECT current_number, giveaway_target FROM guild_state WHERE guild_id=?",
            (gid,)
        ).fetchone()
        if row["giveaway_target"] is None or row["giveaway_target"] < row["current_number"]:
            _roll_next_target_after(conn, gid, row["current_number"])
            return conn.execute(
                "SELECT giveaway_target FROM guild_state WHERE guild_id=?",
                (gid,)
            ).fetchone()["giveaway_target"]
        return row["giveaway_target"]

def get_ticket_url(gid: int) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT ticket_url FROM guild_state WHERE guild_id=?", (gid,)).fetchone()
        return row["ticket_url"] if row else None

def get_ticket_cfg(gid: int) -> tuple[int | None, int | None]:
    with db() as conn:
        row = conn.execute("SELECT ticket_category_id, ticket_staff_role_id FROM guild_state WHERE guild_id=?", (gid,)).fetchone()
        if not row: return None, None
        return row["ticket_category_id"], row["ticket_staff_role_id"]

class OpenTicketPersistent(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 Open Ticket", style=discord.ButtonStyle.green, custom_id="prizo_open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
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

        m_prize = re.search(r"Winner:\s*<@\d+>\s*—\s*(.+?)\s*(?:\n|$)", desc)
        prize = (m_prize.group(1).strip() if m_prize else "🎁 Surprise Gift")

        name = f"ticket-{interaction.user.name.lower()}-{n_hit}"
        existing = discord.utils.get(interaction.guild.text_channels, name=name)
        if existing:
            await interaction.response.send_message(f"✅ Ticket already exists: {existing.mention}", ephemeral=True)
            with contextlib.suppress(Exception):
                button.disabled = True
                await interaction.message.edit(view=self)
            return

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

        await interaction.response.send_message(f"✅ Ticket created: {chan.mention}", ephemeral=True)
        with contextlib.suppress(Exception):
            button.disabled = True
            await interaction.message.edit(view=self)

async def create_winner_ticket(guild: discord.Guild, winner: discord.Member, prize: str, n_hit: int) -> discord.TextChannel:
    cat_id, staff_role_id = get_ticket_cfg(guild.id)
    category = guild.get_channel(cat_id) if cat_id else None
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None
    if not isinstance(winner, discord.Member):
        winner = guild.get_member(winner.id) or await guild.fetch_member(winner.id)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        winner: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, manage_messages=True
        )
    name = f"ticket-{winner.name.lower()}-{n_hit}"
    chan = await guild.create_text_channel(name=name, category=category, overwrites=overwrites, reason="Prizo prize ticket")
    with contextlib.suppress(Exception):
        await chan.edit(sync_permissions=False)

    em = discord.Embed(
        title="🎟️ Prize Ticket",
        description=(
            f"🎟 Ticket for {winner.mention}\n\n"
            f"Please provide:\n"
            f"• **IMVU Account Link**\n"
            f"• **Lucky Number Won:** {n_hit}\n"
            f"• **Prize Claim Notes**\n\n"
            "Mikey.Moon will review and deliver your prize."
        ),
        colour=discord.Colour.green()
    )
    em.set_footer(text=f"{guild.name} • Ticket")
    await chan.send(embed=em)
    return chan

async def try_giveaway_draw(message: discord.Message, reached_n: int):
    gid = message.guild.id
    # Phase 1: read (no side-effects)
    with db() as conn:
        st = conn.execute("""
            SELECT giveaway_target, last_giveaway_n, giveaway_prize, giveaway_mode
            FROM guild_state WHERE guild_id=?
        """,(gid,)).fetchone()
        target = st["giveaway_target"]
        mode = (st["giveaway_mode"] or "random").lower()
        if target is None or reached_n != target:
            if target is None:
                _roll_next_target_after(conn, gid, reached_n)
            return False
        prize = st["giveaway_prize"] or "🎁 Surprise Gift"
        winner_id = message.author.id

    # Phase 2: lock & record
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute("SELECT 1 FROM winners WHERE guild_id=? AND n_won_at=?", (gid, reached_n)).fetchone()
        if exists:
            conn.execute("COMMIT"); return False
        conn.execute("""
            INSERT INTO winners (guild_id, channel_id, user_id, prize, n_won_at, created_at)
            VALUES (?,?,?,?,?,?)
        """, (gid, message.channel.id, winner_id, prize, reached_n, now_iso()))
        if mode == "fixed":
            conn.execute("UPDATE guild_state SET last_giveaway_n=?, giveaway_mode='random', giveaway_target=NULL WHERE guild_id=?",
                         (reached_n, gid))
        else:
            conn.execute("UPDATE guild_state SET last_giveaway_n=? WHERE guild_id=?", (reached_n, gid))
        _roll_next_target_after(conn, gid, reached_n)
        conn.execute("COMMIT")

    # Phase 3: announce
    winner_mention = f"<@{winner_id}>"
    winner_banter = pick_banter("winner") or "Legend behaviour. Take a bow. 👑"
    title = "🎯 Fixed Milestone Win!" if mode == "fixed" else "🎲 Random Giveaway!"
    claim_text = "Click **Open Ticket** below to claim within 48h. 💬"
    embed = discord.Embed(
        title=title,
        description=(f"Target **{reached_n}** hit!\n"
                     f"Winner: {winner_mention} — {prize} 🥳\n\n"
                     f"**{winner_banter}**\n{claim_text}\n\n"
                     f"*New jackpot is armed… keep counting.*"),
        colour=discord.Colour.gold()
    )
    embed.set_footer(text="Jackpot Announcement")
    await message.channel.send(embed=embed, view=OpenTicketPersistent())

    with contextlib.suppress(Exception):
        user = message.guild.get_member(winner_id) or await bot.fetch_user(winner_id)
        await user.send(f"🎉 You won in {message.channel.mention} at **{reached_n}**!\nPrize: {prize}\nUse the **Open Ticket** button in the channel to claim.")
    return True

# ==================== REWARD TOKEN PARSER (tournaments) ====================
def parse_reward_token(token: str):
    s = str(token).strip().lower()
    if s.isdigit(): return ('credits', int(s))
    m = re.fullmatch(r'(\d+)\s*k', s)
    if m: return ('credits', int(m.group(1))*1000)
    if s == 'xwl': return ('xwl', 1)
    m = re.fullmatch(r'(\d+)\s*xwl', s)
    if m: return ('xwl', int(m.group(1)))
    raise ValueError("Reward must be an integer (e.g., 1000), '2k', 'xWL', or '3xWL'.")

# ==================== EVENTS ====================
@bot.event
async def on_ready():
    init_db()
    load_banter()
    load_funfacts()

    if not banter_file_watch.is_running():
        banter_file_watch.start()

    try:
        bot.add_view(OpenTicketPersistent())
    except Exception as e:
        print("Failed to add persistent view:", e)

    bot.locked_players = {}   # {gid: {uid: unlock_dt}}
    bot.last_poster = {}      # {gid: {"user_id": int|None, "count": int}}

    try:
        synced = await bot.tree.sync()
        print(f"Globally synced {len(synced)} commands ✅")
    except Exception as e:
        print("Global sync error:", e)
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print("DB_PATH:", DB_PATH)
    print(f"BANTER_PATH: {BANTER_PATH} • loaded {_banter_summary()}")

    if not ai_banter_watchdog.is_running():
        ai_banter_watchdog.start()

@bot.event
async def on_guild_join(guild: discord.Guild):
    with contextlib.suppress(Exception):
        await bot.tree.sync(guild=guild)
        print(f"Per-guild synced commands to {guild.id}")

@tasks.loop(seconds=30)
async def banter_file_watch():
    global _BANTER_MTIME
    try:
        m = BANTER_PATH.stat().st_mtime
        if m != _BANTER_MTIME:
            load_banter()
            _idle_bucket.clear()
            print(f"[BANTER] Auto-reloaded to v{BANTER_VER}")
    except Exception as e:
        print(f"[BANTER] watch error: {e}")

# ==================== MESSAGE HANDLER ====================
@bot.event
async def on_message(message: discord.Message):
    try:
        if message.author.bot or not message.guild:
            return

        st = get_state(message.guild.id)
        if not st["channel_id"] or message.channel.id != st["channel_id"]:
            return

        try:
            paused = int(st["count_paused"] or 0)
        except Exception:
            paused = 0
        if paused == 1:
            return

        gid = message.guild.id
        last_count_activity[gid] = datetime.now(timezone.utc)

        # Allow replies to the last AI banter thread only
        if message.reference and message.reference.message_id == last_ai_message[gid]:
            if not message.author.bot:
                now = datetime.now(timezone.utc)
                if ai_reply_counts[gid] < AI_MAX_REPLIES_PER_BANTER and now >= ai_reply_next_allowed[gid]:
                    reply_pool = BANTER.get("idle_banter_replies") or BANTER.get("idle_replies") or BANTER.get("idle_banter") or ["Alright, back to counting."]
                    line = random.choice(reply_pool)
                    with contextlib.suppress(Exception):
                        await message.channel.send(f"🤖 {line}", reference=message)
                    ai_reply_counts[gid] += 1
                    ai_reply_next_allowed[gid] = now + timedelta(seconds=AI_REPLY_COOLDOWN_SEC)
                last_count_activity[gid] = now
            return

        mode = (st["count_mode"] or "numbers").lower()

        # ===== LETTERS MODE =====
        if mode == "letters":
            posted_letter = extract_letter(message.content, strict=bool(st["numbers_only"]))
            if posted_letter is None:
                banter = (pick_banter("nonnumeric") or "Letters only in here, mate.").replace("{user}", message.author.mention)
                with contextlib.suppress(Exception):
                    await message.reply(banter)
                return

            expected_letter = (st["current_letter"] or "A").upper()
            last_user = st["last_user_id"]

            locks = bot.locked_players.setdefault(gid, {})
            lp = bot.last_poster.setdefault(gid, {"user_id": None, "count": 0})

            now = datetime.utcnow()
            if message.author.id in locks:
                if now < locks[message.author.id]:
                    with contextlib.suppress(Exception):
                        await message.delete()
                    return
                else:
                    del locks[message.author.id]

            if lp["user_id"] == message.author.id:
                lp["count"] += 1
            else:
                lp["user_id"] = message.author.id
                lp["count"] = 1

            if last_user == message.author.id:
                mark_wrong(gid, message.author.id)
                await safe_react(message, "⛔")
                banter = (pick_banter("wrong") or "Not two in a row. Behave. 😅").replace("{n}", expected_letter)
                with contextlib.suppress(Exception):
                    await message.reply(f"Not two in a row, {message.author.mention}. {banter} Next is **{expected_letter}** for someone else.")
                return

            if posted_letter != expected_letter:
                mark_wrong(gid, message.author.id)
                key = (gid, message.channel.id, message.author.id)
                _wrong_streak[key] += 1
                wrong_entries[gid] += 1
                if wrong_entries[gid] >= 5:
                    wrong_entries[gid] = 0
                    reset_letters(gid, "A")
                    with contextlib.suppress(Exception):
                        await message.channel.send("⚠️ Five wrong entries — starting again at **A**. Keep it tidy, team.")
                    lp["user_id"] = None; lp["count"] = 0
                    return

                try:
                    ban_minutes = int(st["ban_minutes"])
                except Exception:
                    ban_minutes = 1

                if _wrong_streak[key] >= 3:
                    _wrong_streak[key] = 0
                    locks[message.author.id] = now + timedelta(minutes=ban_minutes)
                    roast = pick_banter("roast") or "Have a sit-down and recite the alphabet. 🛋️"
                    with contextlib.suppress(Exception):
                        await safe_react(message, "⛔")
                        await message.reply(f"🚫 {message.author.mention} three wrong on the trot — benched for **{ban_minutes} minutes**. {roast}")
                    lp["user_id"] = None; lp["count"] = 0
                    return

                await safe_react(message, "❌")
                banter = pick_banter("wrong") or "Oof—phonics says ‘nah’. 🔤"
                with contextlib.suppress(Exception):
                    await message.reply(f"{banter} Next up is **{expected_letter}**.")
                return

            # success
            bump_ok_letter(gid, message.author.id, expected_letter)
            await safe_react(message, "✅")
            _wrong_streak[(gid, message.channel.id, message.author.id)] = 0
            wrong_entries[gid] = 0

            # Giveaway step (hidden numeric advances inside bump_ok_letter)
            try:
                st_now = get_state(gid)
                reached_step = st_now["current_number"]
                ensure_giveaway_target(gid)
                target_now = st_now["giveaway_target"]
                if target_now is not None and int(target_now) == int(reached_step):
                    did_announce = await try_giveaway_draw(message, reached_step)
                    # tournament
                    is_active, ends_at, fixed_reward, max_jp, jp_hit, silent = get_tourney(gid)
                    if is_active and parse_iso(ends_at) and now_utc() < parse_iso(ends_at):
                        if jp_hit < max_jp:
                            add_tourney_win(gid, message.author.id, 1)
                            set_tourney(gid, jackpots_hit=jp_hit + 1)
                            await message.channel.send(f"🏁 Tournament win for {message.author.mention}! (+1) • {fixed_reward} creds/xWL")
                        elif not silent:
                            await message.channel.send(f"{message.author.mention} hit another jackpot, but the tournament cap of {max_jp} is reached!")
                    with contextlib.suppress(Exception):
                        await safe_react(message, "🎉")
                    await message.channel.send("🎉 Jackpot bagged! New jackpot is armed… keep going through the alphabet.")
            except Exception:
                pass
            return

        # ===== NUMBERS MODE =====
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

        locks = bot.locked_players.setdefault(gid, {})
        lp = bot.last_poster.setdefault(gid, {"user_id": None, "count": 0})

        now = datetime.utcnow()
        if message.author.id in locks:
            if now < locks[message.author.id]:
                with contextlib.suppress(Exception):
                    await message.delete()
                return
            else:
                del locks[message.author.id]

        if lp["user_id"] == message.author.id:
            lp["count"] += 1
        else:
            lp["user_id"] = message.author.id
            lp["count"] = 1

        if last_user == message.author.id:
            mark_wrong(gid, message.author.id)
            await safe_react(message, "⛔")
            banter = (pick_banter("wrong") or "Not two in a row. Behave. 😅").replace("{n}", str(expected))
            with contextlib.suppress(Exception):
                await message.reply(f"Not two in a row, {message.author.mention}. {banter} Next is **{expected}** for someone else.")
            return

        if posted != expected:
            mark_wrong(gid, message.author.id)
            key = (gid, message.channel.id, message.author.id)
            _wrong_streak[key] += 1
            wrong_entries[gid] += 1
            if wrong_entries[gid] >= 5:
                wrong_entries[gid] = 0
                reset_count(gid)
                with contextlib.suppress(Exception):
                    await message.channel.send("⚠️ Five wrong entries — counting has been reset to **1**. Keep it tidy, team.")
                lp["user_id"] = None; lp["count"] = 0
                return

            try:
                ban_minutes = int(st["ban_minutes"])
            except Exception:
                ban_minutes = 1
            if _wrong_streak[key] >= 3:
                _wrong_streak[key] = 0
                locks[message.author.id] = now + timedelta(minutes=ban_minutes)
                roast = pick_banter("roast") or "Have a sit-down and count sheep, not numbers. 🛋️"
                with contextlib.suppress(Exception):
                    await safe_react(message, "⛔")
                    await message.reply(f"🚫 {message.author.mention} three wrong on the trot — benched for **{ban_minutes} minutes**. {roast}")
                lp["user_id"] = None; lp["count"] = 0
                return

            await safe_react(message, "❌")
            banter = pick_banter("wrong") or "Oof—maths says ‘nah’. 📏"
            with contextlib.suppress(Exception):
                await message.reply(f"{banter} Next up is **{expected}**.")
            return

        # success
        bump_ok(gid, message.author.id)
        await safe_react(message, "✅")
        _wrong_streak[(gid, message.channel.id, message.author.id)] = 0
        wrong_entries[gid] = 0

        with contextlib.suppress(Exception):
            log_correct_count(gid, expected, message.author.id)

        # milestones
        MILESTONES = {10,20,25,30,40,50,69,75,80,90,100,111,123,150,200,250,300,333,369,400,420,500,600,666,700,750,800,900,999,1000}
        if expected in MILESTONES:
            em = discord.Embed(
                title="🎉 Party Mode 🎉",
                description=f"**{expected}** smashed by {message.author.mention}!",
                colour=discord.Colour.gold()
            )
            em.set_footer(text=f"Guild streak: {get_state(gid)['guild_streak']} • Keep it rolling!")
            with contextlib.suppress(Exception):
                await message.channel.send(embed=em)
                mb = pick_banter("milestone")
                if mb: await message.channel.send(mb)

        # facts + badges
        add_badge = (is_prime(expected) or is_palindrome(expected) or (expected % 100 == 0) or funny_number(expected))
        if add_badge or st["facts_on"]:
            fact = maths_fact(expected)
            if fact:
                with contextlib.suppress(Exception):
                    await message.channel.send(f"✨ {fact}")
        if add_badge:
            with db() as conn:
                _touch_user(conn, gid, message.author.id, correct=0, wrong=0,
                            streak_best=get_state(gid)["guild_streak"], add_badge=True)

        ensure_giveaway_target(gid)

        st_now = get_state(gid)
        target_now = st_now["giveaway_target"]
        if target_now is not None and expected == int(target_now):
            did_announce = await try_giveaway_draw(message, expected)
            # tournament hook
            is_active, ends_at, fixed_reward, max_jp, jp_hit, silent = get_tourney(gid)
            if is_active and parse_iso(ends_at) and now_utc() < parse_iso(ends_at):
                if jp_hit < max_jp:
                    add_tourney_win(gid, message.author.id, 1)
                    set_tourney(gid, jackpots_hit=jp_hit + 1)
                    await message.channel.send(f"🏁 Tournament win for {message.author.mention}! (+1) • {fixed_reward} creds/xWL")
                elif not silent:
                    await message.channel.send(f"{message.author.mention} hit another jackpot, but the tournament cap of {max_jp} is already reached!")
            with contextlib.suppress(Exception):
                await safe_react(message, "🎉")
            await message.channel.send("🎉 Jackpot bagged! New jackpot is armed… keep counting.")
            if did_announce:
                return
    except Exception as e:
        print(f"[on_message] {type(e).__name__}: {e}")

# ==================== COG: SLASH COMMANDS ====================
class FunCounting(commands.Cog):
    def __init__(self, b: commands.Bot):
        self.bot = b

    # Setup
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

    @app_commands.command(name="count_pause", description="Pause or resume counting in the configured channel.")
    @app_commands.describe(on="True to pause, False to resume")
    @app_commands.guild_only()
    async def count_pause(self, interaction: discord.Interaction, on: bool):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        set_count_paused(interaction.guild_id, on)
        if on:
            await interaction.response.send_message("⏸️ Counting is **paused**. Chat freely in the counting channel.", ephemeral=False)
        else:
            st = get_state(interaction.guild_id)
            mode = (st["count_mode"] or "numbers").lower()
            if mode == "letters":
                nxt = (st["current_letter"] or "A")
                await interaction.response.send_message(f"▶️ Counting **resumed** (letters). Next is **{nxt}**.", ephemeral=False)
            else:
                nxt = st["current_number"] + 1
                await interaction.response.send_message(f"▶️ Counting **resumed** (numbers). Next is **{nxt}**.", ephemeral=False)

    @app_commands.command(name="count_mode", description="Set counting mode: numbers or letters.")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Numbers (1,2,3…)", value="numbers"),
        app_commands.Choice(name="Letters (A,B,C…)", value="letters"),
    ])
    @app_commands.guild_only()
    async def count_mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=False)
        val = (mode.value or "numbers").lower()
        try:
            with db() as conn:
                conn.execute("UPDATE guild_state SET count_mode=? WHERE guild_id=?", (val, interaction.guild_id))
                if val == "letters":
                    is_active, ends_at, *_ = get_tourney(interaction.guild_id)
                    end_dt = parse_iso(ends_at)
                    start_letter = "Z" if (is_active and end_dt and now_utc() < end_dt) else "A"
                    conn.execute(
                        "UPDATE guild_state SET current_letter=?, last_user_id=NULL, guild_streak=0 WHERE guild_id=?",
                        (start_letter, interaction.guild_id)
                    )
                    msg = f"🔤 Mode set to **letters**. Next is **{start_letter}**."
                else:
                    conn.execute("UPDATE guild_state SET last_user_id=NULL, guild_streak=0 WHERE guild_id=?",
                                 (interaction.guild_id,))
                    st_now = get_state(interaction.guild_id)
                    next_n = int(st_now["current_number"]) + 1
                    msg = f"🔢 Mode set to **numbers**. Next is **{next_n}**."
        except Exception as e:
            print(f"[count_mode] error: {type(e).__name__}: {e}")
            return await interaction.followup.send(f"❌ Couldn’t switch mode: **{type(e).__name__}** — {e}", ephemeral=True)
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="numbers_only", description="Toggle numbers-only / single-letter-only mode.")
    @app_commands.guild_only()
    async def numbers_only_cmd(self, interaction: discord.Interaction, on: bool):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        set_numbers_only(interaction.guild_id, on)
        await interaction.response.send_message(f"Numbers-only (strict parsing) **{'ON' if on else 'OFF'}**", ephemeral=True)

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

    # Giveaway config
    @app_commands.command(name="giveaway_config", description="Set random giveaway range and prize label.")
    @app_commands.describe(range_min="Min steps until a hidden giveaway (e.g. 1)",
                           range_max="Max steps (e.g. 6)",
                           prize="Prize label, e.g. '💎 2 WL'")
    @app_commands.guild_only()
    async def giveaway_config(self, interaction: discord.Interaction,
                              range_min: int = 10, range_max: int = 120,
                              prize: str = "💎 1000 2 WL"):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
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

    @app_commands.command(name="ticket_diag", description="Show Prizo's effective permissions in the ticket category.")
    @app_commands.guild_only()
    async def ticket_diag(self, interaction: discord.Interaction):
        st = get_state(interaction.guild_id)
        cat_id = st["ticket_category_id"]
        cat = interaction.guild.get_channel(cat_id) if cat_id else None
        me = interaction.guild.me
        g = me.guild_permissions
        lines = [f"Guild perms: manage_channels={g.manage_channels}, view_channel={g.view_channel}, send_messages={g.send_messages}"]
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
        claim_text = pick_banter("claim") or "To claim your prize: **open a ticket** within 48 hours. 💬"

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
            row = conn.execute("SELECT COALESCE(current_number, 0) AS current_number FROM guild_state WHERE guild_id=?",
                               (interaction.guild_id,)).fetchone()
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
            f"🎲 Lucky number armed somewhere in the next **{number}** counts.\nFirst to hit it wins **{prize}**.",
            ephemeral=True
        )

    @app_commands.command(name="giveaway_fixed_off", description="Disable fixed-number prize and return to random mode.")
    @app_commands.guild_only()
    async def giveaway_fixed_off(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        with db() as conn:
            row = conn.execute("SELECT current_number FROM guild_state WHERE guild_id=?", (interaction.guild_id,)).fetchone()
            conn.execute("UPDATE guild_state SET giveaway_mode='random', giveaway_target=NULL WHERE guild_id=?",
                         (interaction.guild_id,))
            _roll_next_target_after(conn, interaction.guild_id, row["current_number"])
        await interaction.response.send_message("✅ Fixed-number mode **OFF**. Random jackpot re-armed.", ephemeral=True)

    # Benching
    @app_commands.command(name="set_ban_minutes", description="Set bench duration (minutes) after 3 wrong guesses in a row.")
    @app_commands.describe(minutes="Bench duration in minutes (default 1)")
    @app_commands.guild_only()
    async def set_ban_minutes(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 1440]):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        with db() as conn:
            conn.execute("UPDATE guild_state SET ban_minutes=? WHERE guild_id=?",
                         (int(minutes), interaction.guild_id))
        await interaction.response.send_message(f"✅ Bench duration set to **{int(minutes)} minutes**.", ephemeral=True)

    # Tournaments
    @app_commands.command(name="tournament_start", description="Start a Prizo tournament")
    @app_commands.describe(
        duration="How long it runs (minutes, default 30)",
        reward="Fixed reward per jackpot (accepts '1000', '2k', 'xWL', '3xWL')",
        cap="Max jackpots counted (default 5)",
        silent="Silent after cap (true/false, default true)"
    )
    async def tournament_start(self, interaction: discord.Interaction,
        duration: app_commands.Range[int, 1, 1440] = 30,
        reward: str = "1000",
        cap: app_commands.Range[int, 1, 100] = 5,
        silent: bool = True
    ):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        try:
            reward_kind, reward_amount = parse_reward_token(reward)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        reward_label = f"{reward_amount}xWL" if reward_kind == "xwl" else f"{reward_amount:,} credits"
        ends = now_utc() + timedelta(minutes=int(duration))
        set_tourney(interaction.guild_id, is_active=1, ends_at_utc=iso(ends),
                    fixed_reward=int(reward_amount), max_jackpots=int(cap),
                    jackpots_hit=0, silent_after_limit=1 if silent else 0)
        reset_tourney_wins(interaction.guild_id)
        await interaction.response.send_message(
            f"🏁 **Prizo Tournament started!**\n"
            f"Duration: **{duration}m** • Reward: **{reward_label}** • Cap: **{cap} jackpots**\n"
            f"_Jackpots after cap will be {'silent' if silent else 'announced'}._"
        )

    @app_commands.command(name="tournament_end", description="End the current Prizo tournament now")
    async def tournament_end(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        set_tourney(interaction.guild_id, is_active=0, ends_at_utc=None)
        winners = top_wins(interaction.guild_id, 10)
        medal = ["🥇","🥈","🥉"]
        lines = [(f"{medal[i] if i < 3 else f'{i+1}.'} <@{uid}> — {w} win(s)") for i,(uid,w) in enumerate(winners)]
        summary = "\n".join(lines) if lines else "_No winners logged._"
        await interaction.response.send_message(f"🏁 **Prizo Tournament** ended.\n{summary}")

    @app_commands.command(name="tournament_status", description="Show current tournament status")
    async def tournament_status(self, interaction: discord.Interaction):
        is_active, ends_at, fixed_reward, max_jp, jp_hit, silent = get_tourney(interaction.guild_id)
        if not is_active:
            return await interaction.response.send_message("No tournament is active.", ephemeral=True)
        end_dt = parse_iso(ends_at)
        mins = max(0, int(((end_dt - now_utc()).total_seconds() // 60) if end_dt else 0))
        reward_line = f"{fixed_reward:,} (credits or xWL amount)"
        await interaction.response.send_message(
            f"🏁 Tournament active — ends in **~{mins}m**\n"
            f"Reward (per jackpot): **{reward_line}** • Cap: **{jp_hit}/{max_jp}** • After cap: **{'silent' if silent else 'announce'}**.",
            ephemeral=False
        )

    # Banter/json utils
    @app_commands.command(name="banter_version", description="Show the loaded banter.json version and path.")
    @app_commands.guild_only()
    async def banter_version(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"banter.json v**{BANTER_VER}**\npath: `{BANTER_PATH}`\ncounts: {_banter_summary()}",
            ephemeral=True
        )

    @app_commands.command(name="reload_banter", description="Reload banter.json without restarting.")
    @app_commands.guild_only()
    async def reload_banter(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=False)
        try:
            load_banter()
            _idle_bucket.clear()
            await interaction.followup.send(f"✅ Reloaded banter v**{BANTER_VER}** from `{BANTER_PATH}` • {_banter_summary()}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Reload failed: {type(e).__name__}: {e}", ephemeral=True)

    # AI toggles
    @app_commands.command(name="aibanter_on", description="Enable AI banter in counting channel.")
    @app_commands.guild_only()
    async def aibanter_on(self, interaction: discord.Interaction):
        ai_helper_enabled[interaction.guild_id] = True
        await interaction.response.send_message("✅ AI banter enabled.", ephemeral=True)

    @app_commands.command(name="aibanter_off", description="Disable AI banter in counting channel.")
    @app_commands.guild_only()
    async def aibanter_off(self, interaction: discord.Interaction):
        ai_helper_enabled[interaction.guild_id] = False
        await interaction.response.send_message("✅ AI banter disabled.", ephemeral=True)

    @app_commands.command(name="aibanter_idle", description="Set minutes of silence before AI speaks.")
    @app_commands.describe(minutes="1–60")
    @app_commands.guild_only()
    async def aibanter_idle(self, interaction: discord.Interaction, minutes: app_commands.Range[int,1,60]):
        ai_idle_minutes[interaction.guild_id] = int(minutes)
        await interaction.response.send_message(f"⏱️ AI banter idle set to **{int(minutes)} min**.", ephemeral=True)

async def setup_cog():
    await bot.add_cog(FunCounting(bot))

@bot.event
async def setup_hook():
    await setup_cog()

# ==================== AI BANTER WATCHDOG ====================
@tasks.loop(seconds=30)
async def ai_banter_watchdog():
    now = datetime.now(timezone.utc)
    for guild in bot.guilds:
        gid = guild.id
        if not ai_helper_enabled[gid]:
            continue
        idle_for = now - last_count_activity[gid]
        if idle_for < timedelta(minutes=ai_idle_minutes[gid]):
            continue
        try:
            st = get_state(gid)
            counting_channel_id = st["channel_id"]
            if not counting_channel_id:
                continue
            channel = guild.get_channel(counting_channel_id)
            if not channel:
                continue
            line = _next_idle_line(gid)
            msg = await channel.send(f"🤖 {line}")
            last_ai_message[gid] = msg.id
            ai_reply_counts[gid] = 0
            ai_reply_next_allowed[gid] = now + timedelta(seconds=2)
            last_count_activity[gid] = now
        except Exception as e:
            print(f"[AI banter] guild {gid} error: {e}")

# ==================== MAIN ====================
if __name__ == "__main__":
    bot.run(TOKEN)
