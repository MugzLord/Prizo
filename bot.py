# prizo_bot.py — FULL Prizo counting bot with guaranteed-range giveaways
# - Winner ALWAYS within armed range (e.g., 5–25)
# - No "steps left" jumps after resets (DB triggers re-arm target on decreases / range edits)
# - Multi-guild, per-channel; SQLite persistence
# - Slash commands listed in the header above

import os
import re
import random
import sqlite3
from datetime import datetime, timedelta

import discord
from discord.ext import commands
from discord import app_commands

# =========================
# Config / Intents
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN in your environment.")

BOT_PREFIX = "!"  # not used for commands (we use slash), but left for compatibility

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.guilds = True
INTENTS.members = False

DB_PATH = "prizo.db"

# =========================
# DB Helpers
# =========================
def db():
    """Open connection, ensure schema & triggers exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # --- tables ---
    conn.execute("""
    CREATE TABLE IF NOT EXISTS guild_state (
        guild_id              INTEGER PRIMARY KEY,
        counting_channel_id   INTEGER,
        current_number        INTEGER NOT NULL DEFAULT 0,
        last_user_id          INTEGER,
        last_msg_id           INTEGER,
        last_updated_at       TEXT,

        -- Giveaway config/state
        giveaway_range_min    INTEGER NOT NULL DEFAULT 10,
        giveaway_range_max    INTEGER NOT NULL DEFAULT 120,
        giveaway_prize        TEXT    NOT NULL DEFAULT '💎 1000 VU Credits',
        giveaway_target       INTEGER,
        last_giveaway_n       INTEGER NOT NULL DEFAULT 0,
        ticket_url            TEXT,
        giveaway_mode         TEXT    DEFAULT 'random'  -- 'random' | 'fixed'
    );
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS count_log (
        guild_id   INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        n          INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        msg_id     INTEGER NOT NULL,
        created_at TEXT    NOT NULL,
        PRIMARY KEY (guild_id, n)
    );
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS winners (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        prize      TEXT    NOT NULL,
        n_won_at   INTEGER NOT NULL,
        created_at TEXT    NOT NULL
    );
    """)

    # --- triggers: re-arm on reset / range change ---
    conn.execute("""
    CREATE TRIGGER IF NOT EXISTS trg_prizo_rearm_on_reset
    AFTER UPDATE OF current_number ON guild_state
    WHEN NEW.current_number < OLD.current_number
    BEGIN
      UPDATE guild_state
      SET giveaway_target =
          NEW.current_number
          + (
              abs(random()) % (
                CASE
                  WHEN (CASE WHEN (NEW.giveaway_range_min < 5) THEN 5 ELSE NEW.giveaway_range_min END) >= NEW.giveaway_range_max
                    THEN 1
                  ELSE (NEW.giveaway_range_max - (CASE WHEN (NEW.giveaway_range_min < 5) THEN 5 ELSE NEW.giveaway_range_min END) + 1)
                END
              )
            )
          + (CASE WHEN (NEW.giveaway_range_min < 5) THEN 5 ELSE NEW.giveaway_range_min END),
          giveaway_mode = 'random'
      WHERE guild_id = NEW.guild_id;
    END;
    """)

    conn.execute("""
    CREATE TRIGGER IF NOT EXISTS trg_prizo_rearm_on_range_change
    AFTER UPDATE OF giveaway_range_min, giveaway_range_max ON guild_state
    BEGIN
      UPDATE guild_state
      SET giveaway_target =
          current_number
          + (
              abs(random()) % (
                CASE
                  WHEN (CASE WHEN (NEW.giveaway_range_min < 5) THEN 5 ELSE NEW.giveaway_range_min END) >= NEW.giveaway_range_max
                    THEN 1
                  ELSE (NEW.giveaway_range_max - (CASE WHEN (NEW.giveaway_range_min < 5) THEN 5 ELSE NEW.giveaway_range_min END) + 1)
                END
              )
            )
          + (CASE WHEN (NEW.giveaway_range_min < 5) THEN 5 ELSE NEW.giveaway_range_min END),
          giveaway_mode = 'random'
      WHERE guild_id = NEW.guild_id;
    END;
    """)

    return conn

def get_or_create_state(gid: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM guild_state WHERE guild_id=?", (gid,)).fetchone()
        if row:
            return row
        conn.execute("INSERT OR IGNORE INTO guild_state (guild_id) VALUES (?)", (gid,))
        return conn.execute("SELECT * FROM guild_state WHERE guild_id=?", (gid,)).fetchone()

def _roll_next_target_after(conn, guild_id: int, current_number: int):
    """Draw ONE target for this round: target = current_number + randint(min..max)."""
    row = conn.execute(
        "SELECT giveaway_range_min, giveaway_range_max FROM guild_state WHERE guild_id=?",
        (guild_id,)
    ).fetchone()

    lo = max(5, int(row["giveaway_range_min"] or 5))
    hi = int(row["giveaway_range_max"] or (lo + 1))
    if hi <= lo:
        hi = lo + 1

    delta = random.randint(lo, hi)
    target = int(current_number) + delta

    conn.execute(
        "UPDATE guild_state SET giveaway_target=?, giveaway_mode='random' WHERE guild_id=?",
        (target, guild_id)
    )

def get_ticket_url(gid: int):
    with db() as conn:
        r = conn.execute("SELECT ticket_url FROM guild_state WHERE guild_id=?", (gid,)).fetchone()
        return r["ticket_url"] if r and r["ticket_url"] else None

def pick_banter(key: str):
    # Placeholder for your banter system
    if key == "claim":
        return "To claim your prize, open a ticket within 48 hours. 🎫"
    return None

# =========================
# Bot
# =========================
class Prizo(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=BOT_PREFIX, intents=INTENTS)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

bot = Prizo()

# =========================
# Small utilities
# =========================
NUMERIC_RE = re.compile(r"^\d+$")

def is_int(text: str) -> bool:
    return NUMERIC_RE.match(text.strip()) is not None

def admin_only(inter: discord.Interaction) -> bool:
    return bool(inter.user.guild_permissions.manage_guild)

# =========================
# Slash Commands
# =========================
@bot.tree.command(name="counting_rules", description="Show the counting rules for this server.")
@app_commands.guild_only()
async def counting_rules(interaction: discord.Interaction):
    em = discord.Embed(
        title="Counting Rules",
        description=(
            "1) Count up by 1 each message.\n"
            "2) No same person twice in a row.\n"
            "3) Wrong number resets to 0.\n\n"
            "Hidden giveaways: winners are guaranteed within the armed range. 🎁"
        ),
        colour=discord.Colour.blurple()
    )
    await interaction.response.send_message(embed=em, ephemeral=True)

@bot.tree.command(name="set_counting_channel", description="Set the channel where counting happens.")
@app_commands.guild_only()
async def set_counting_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not admin_only(interaction):
        return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO guild_state (guild_id) VALUES (?)", (interaction.guild_id,))
        conn.execute("UPDATE guild_state SET counting_channel_id=? WHERE guild_id=?",
                     (channel.id, interaction.guild_id))
    await interaction.response.send_message(f"✅ Counting channel set to {channel.mention}.", ephemeral=True)

@bot.tree.command(name="reset_count", description="Admin: reset the count to 0 (auto re-arms jackpot).")
@app_commands.guild_only()
async def reset_count(interaction: discord.Interaction):
    if not admin_only(interaction):
        return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
    with db() as conn:
        row = conn.execute("SELECT last_giveaway_n FROM guild_state WHERE guild_id=?",
                           (interaction.guild_id,)).fetchone()
        conn.execute("""
            UPDATE guild_state
            SET current_number=0, last_user_id=NULL, last_msg_id=NULL, last_updated_at=?, last_giveaway_n=?
            WHERE guild_id=?;
        """, (datetime.utcnow().isoformat(), (row["last_giveaway_n"] + 1 if row else 0), interaction.guild_id))
        # trigger will re-arm automatically
    await interaction.response.send_message("🔁 Count reset to **0**. Jackpot target re-armed.", ephemeral=True)

# --- Giveaways (random config) ---
@bot.tree.command(name="giveaway_config", description="Set random giveaway range and prize label.")
@app_commands.describe(range_min="Min steps until a hidden giveaway (default 10)",
                       range_max="Max steps (default 120)",
                       prize="Prize label, e.g. '💎 1000 VU Credits'")
@app_commands.guild_only()
async def giveaway_config(interaction: discord.Interaction,
                          range_min: int = 10, range_max: int = 120,
                          prize: str = "💎 1000 VU Credits"):
    if not admin_only(interaction):
        return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
    if range_min < 5:
        range_min = 5
    if range_max <= range_min:
        range_max = range_min + 1

    with db() as conn:
        conn.execute("""
        INSERT OR IGNORE INTO guild_state (guild_id) VALUES (?);
        """, (interaction.guild_id,))
        conn.execute("""
        UPDATE guild_state
        SET giveaway_range_min=?, giveaway_range_max=?, giveaway_prize=?, giveaway_mode='random'
        WHERE guild_id=?;
        """, (range_min, range_max, prize, interaction.guild_id))
        cur = conn.execute("SELECT current_number FROM guild_state WHERE guild_id=?",
                           (interaction.guild_id,)).fetchone()
        _roll_next_target_after(conn, interaction.guild_id, cur["current_number"])

    await interaction.response.send_message(
        f"🎰 Giveaway armed. Range **{range_min}–{range_max}** • Prize: **{prize}** (target is secret).",
        ephemeral=True
    )

@bot.tree.command(name="set_ticket", description="Set the server ticket link for prize claims.")
@app_commands.guild_only()
async def set_ticket(interaction: discord.Interaction, url: str):
    if not admin_only(interaction):
        return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO guild_state (guild_id) VALUES (?)", (interaction.guild_id,))
        conn.execute("UPDATE guild_state SET ticket_url=? WHERE guild_id=?", (url, interaction.guild_id))
    await interaction.response.send_message(f"🎫 Ticket link set: {url}", ephemeral=True)

@bot.tree.command(name="giveaway_status", description="Peek giveaway info (admins only).")
@app_commands.guild_only()
async def giveaway_status(interaction: discord.Interaction):
    if not admin_only(interaction):
        return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
    with db() as conn:
        st = conn.execute("""
        SELECT current_number, giveaway_target, giveaway_range_min, giveaway_range_max,
               last_giveaway_n, giveaway_prize, ticket_url, giveaway_mode
        FROM guild_state WHERE guild_id=?;
        """,(interaction.guild_id,)).fetchone()
    left = None
    if st and st["giveaway_target"] is not None:
        left = max(0, int(st["giveaway_target"]) - int(st["current_number"]))
    turl = (st["ticket_url"] if st else None) or "— not set —"
    mode = (st["giveaway_mode"] or "random").lower() if st else "random"

    em = discord.Embed(title="Armed.", colour=discord.Colour.gold())
    em.add_field(name="Mode", value=mode, inline=False)
    em.add_field(name="Range", value=f"{st['giveaway_range_min']}–{st['giveaway_range_max']}", inline=False)
    em.add_field(name="Since last jackpot", value=str(st["last_giveaway_n"]), inline=False)
    em.add_field(name="Prize", value=f"{st['giveaway_prize']}", inline=False)
    em.add_field(name="Ticket link", value=turl, inline=False)
    em.add_field(name="≈Next in", value=f"{left} steps" if left is not None else "—", inline=False)

    await interaction.response.send_message(embed=em, ephemeral=True)

@bot.tree.command(name="giveaway_now", description="Instant draw among recent correct counters.")
@app_commands.describe(window="How many recent correct counts to include (default 80)")
@app_commands.guild_only()
async def giveaway_now(interaction: discord.Interaction, window: int = 80):
    if not admin_only(interaction):
        return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
    gid = interaction.guild_id
    with db() as conn:
        st = conn.execute("SELECT current_number, giveaway_prize FROM guild_state WHERE guild_id=?",
                          (gid,)).fetchone()
        current_n = int(st["current_number"])
        rows = conn.execute("""
        SELECT DISTINCT user_id FROM count_log
        WHERE guild_id=? AND n>? AND n<=?
        ORDER BY n DESC;
        """,(gid, max(0, current_n - window), current_n)).fetchall()
        pool = [r["user_id"] for r in rows]
    if not pool:
        return await interaction.response.send_message("No recent participants to draw from.", ephemeral=True)
    winner = random.choice(pool)

    ticket_url = get_ticket_url(gid)
    claim_text = pick_banter("claim") or "To claim your prize: open a ticket within 48 hours. 🎫"

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

# Fixed number on/off
@bot.tree.command(name="giveaway_fixed", description="Set a fixed winning number (the typer of that number wins).")
@app_commands.describe(number="Exact number that wins (e.g., 56)",
                       prize="Prize label, e.g. '💎 500 VU Credits'")
@app_commands.guild_only()
async def giveaway_fixed(interaction: discord.Interaction, number: int, prize: str = "💎 500 VU Credits"):
    if not admin_only(interaction):
        return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
    if number < 1:
        return await interaction.response.send_message("Number must be **≥ 1**.", ephemeral=True)

    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO guild_state (guild_id) VALUES (?);
        """, (interaction.guild_id,))
        conn.execute("""
            UPDATE guild_state
            SET giveaway_target=?, giveaway_prize=?, giveaway_mode='fixed'
            WHERE guild_id=?;
        """, (number, prize, interaction.guild_id))
    await interaction.response.send_message(
        f"🎯 Fixed milestone armed: **{number}** → prize **{prize}**.\n"
        f"➡️ Whoever types **{number}** correctly will win.",
        ephemeral=True
    )

@bot.tree.command(name="giveaway_fixed_off", description="Disable fixed-number prize and return to random mode.")
@app_commands.guild_only()
async def giveaway_fixed_off(interaction: discord.Interaction):
    if not admin_only(interaction):
        return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
    with db() as conn:
        row = conn.execute("SELECT current_number FROM guild_state WHERE guild_id=?",
                           (interaction.guild_id,)).fetchone()
        conn.execute("UPDATE guild_state SET giveaway_mode='random', giveaway_target=NULL WHERE guild_id=?",
                     (interaction.guild_id,))
        _roll_next_target_after(conn, interaction.guild_id, int(row["current_number"]))
    await interaction.response.send_message("✅ Fixed-number mode **OFF**. Random jackpot re-armed.", ephemeral=True)

# =========================
# Counting Logic
# =========================
@bot.event
async def on_message(message: discord.Message):
    # Allow commands to work
    await bot.process_commands(message)
    if message.author.bot or not message.guild:
        return

    gid = message.guild.id
    st = get_or_create_state(gid)
    counting_channel_id = st["counting_channel_id"]
    if counting_channel_id is None or message.channel.id != counting_channel_id:
        return

    content = message.content.strip()
    if not is_int(content):
        return  # ignore non-numeric chatter in counting channel

    n = int(content)

    # RULES:
    #  - next number must be current_number + 1
    #  - same user cannot count twice consecutively
    with db() as conn:
        row = conn.execute("""
            SELECT current_number, last_user_id, last_giveaway_n, giveaway_target, giveaway_prize, giveaway_mode
            FROM guild_state WHERE guild_id=?;
        """, (gid,)).fetchone()

        current_number = int(row["current_number"])
        last_user_id = row["last_user_id"]
        last_giveaway_n = int(row["last_giveaway_n"])

        # Same user twice -> reset to 0 (trigger will re-arm)
        if last_user_id == message.author.id:
            conn.execute("""
                UPDATE guild_state
                SET current_number=0, last_user_id=?, last_msg_id=?, last_updated_at=?, last_giveaway_n=?
                WHERE guild_id=?;
            """, (message.author.id, message.id, datetime.utcnow().isoformat(), last_giveaway_n + 1, gid))
            return

        expected = current_number + 1
        if n != expected:
            # Wrong -> reset to 0 (trigger re-arms)
            conn.execute("""
                UPDATE guild_state
                SET current_number=0, last_user_id=?, last_msg_id=?, last_updated_at=?, last_giveaway_n=?
                WHERE guild_id=?;
            """, (message.author.id, message.id, datetime.utcnow().isoformat(), last_giveaway_n + 1, gid))
            return

        # Correct -> advance
        new_n = expected
        conn.execute("""
            UPDATE guild_state
            SET current_number=?, last_user_id=?, last_msg_id=?, last_updated_at=?
            WHERE guild_id=?;
        """, (new_n, message.author.id, message.id, datetime.utcnow().isoformat(), gid))

        # Log the correct count
        conn.execute("""
            INSERT OR REPLACE INTO count_log (guild_id, channel_id, n, user_id, msg_id, created_at)
            VALUES (?,?,?,?,?,?);
        """, (gid, message.channel.id, new_n, message.author.id, message.id, datetime.utcnow().isoformat()))

        # Check giveaway win
        target = row["giveaway_target"]
        prize_label = row["giveaway_prize"]
        mode = (row["giveaway_mode"] or "random").lower()

        is_win = False
        if target is not None:
            t = int(target)
            if mode == "fixed":
                is_win = (new_n == t)
            else:
                is_win = (new_n >= t)  # guaranteed within range since target drawn relative to current

        if is_win:
            # Record winner
            conn.execute("""
                INSERT INTO winners (guild_id, channel_id, user_id, prize, n_won_at, created_at)
                VALUES (?,?,?,?,?,?);
            """, (gid, message.channel.id, message.author.id, prize_label, new_n, datetime.utcnow().isoformat()))
            # Reset since_last and prepare next round AFTER this number
            conn.execute("UPDATE guild_state SET last_giveaway_n=0 WHERE guild_id=?", (gid,))
            _roll_next_target_after(conn, gid, new_n)

    # Announce the win after commit
    if 'is_win' in locals() and is_win:
        ticket_url = get_ticket_url(gid)
        claim_text = pick_banter("claim") or "To claim your prize, open a ticket within 48 hours. 🎫"
        em = discord.Embed(
            title="🎉 Jackpot!",
            description=f"Winner: <@{message.author.id}> — **{prize_label}**\n{claim_text}",
            colour=discord.Colour.purple()
        )
        if ticket_url:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="🎫 Open Ticket", url=ticket_url))
            await message.channel.send(embed=em, view=view)
        else:
            await message.channel.send(embed=em)

# =========================
# Run
# =========================
if __name__ == "__main__":
    bot.run(TOKEN)
