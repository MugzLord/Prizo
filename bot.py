# bot.py

import os
import re
import json
import asyncio
import contextlib
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

import discord
from discord.ext import commands
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN in your env")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------------------------------
# load banter.json
# -------------------------------------------------
BANTER: Dict[str, List[str]] = {}
if os.path.exists("banter.json"):
    try:
        with open("banter.json", "r", encoding="utf-8") as f:
            BANTER = json.load(f)
        print("[banter] loaded.")
    except Exception as e:
        print(f"[banter] failed to load: {e}")


def pick_banter(key: str, default: str = "") -> str:
    arr = BANTER.get(key) or []
    if not arr:
        return default
    return random.choice(arr)


# -------------------------------------------------
# load banter.json (only for word numbers)
# -------------------------------------------------
COUNT_CFG: Dict[str, Any] = {
    "word_numbers": {}
}
if os.path.exists("banter.json"):
    try:
        with open("banter.json", "r", encoding="utf-8") as f:
            loaded = json.load(f)
            COUNT_CFG["word_numbers"] = loaded.get("word_numbers", {})
        print("[banter] loaded.")
    except Exception as e:
        print(f"[banter] failed to load: {e}")

WORD_NUMBERS: Dict[str, int] = COUNT_CFG.get("word_numbers", {})

# -------------------------------------------------
# in-memory state
# -------------------------------------------------
GUILDS: Dict[int, Dict[str, Any]] = {}
TICKET_CFG: Dict[int, Dict[str, Optional[int]]] = {}          # guild_id -> {category_id, staff_role_id}
ai_helper_enabled: Dict[int, bool] = {}
ai_idle_minutes: Dict[int, int] = {}

INT_STRICT = re.compile(r"^\s*(-?\d+)\s*$")
INT_LOOSE = re.compile(r"^\s*(-?\d+)\b")


def get_state(gid: int) -> Dict[str, Any]:
    if gid not in GUILDS:
        # defaults
        GUILDS[gid] = {
            "current_number": 0,
            "last_user_id": None,
            "words_only": False,
            "ban_minutes": 5,
            "wrong_streak": {},       # (channel_id, user_id) -> int
            "locks": {},              # user_id -> datetime
            "tickets": [],            # user IDs who won mini-games
            "lucky_prize": "Lucky number mini-game prize",

            # dynamic lucky
            "lucky_min": 10,
            "lucky_max": 100,
            "lucky_target": None,

            # dynamic milestone
            "milestone_min": 20,
            "milestone_max": 150,
            "next_milestone": None,
        }
    st = GUILDS[gid]

    # ensure targets exist
    if st.get("lucky_target") is None:
        st["lucky_target"] = random.randint(st["lucky_min"], st["lucky_max"])
    if st.get("next_milestone") is None:
        st["next_milestone"] = random.randint(st["milestone_min"], st["milestone_max"])

    return st


def get_ticket_cfg(gid: int) -> Tuple[Optional[int], Optional[int]]:
    cfg = TICKET_CFG.get(gid) or {}
    return (cfg.get("category_id"), cfg.get("staff_role_id"))


def set_ticket_cfg(gid: int, category_id: Optional[int] = None, staff_role_id: Optional[int] = None) -> None:
    cfg = TICKET_CFG.get(gid) or {}
    if category_id is not None:
        cfg["category_id"] = category_id
    if staff_role_id is not None:
        cfg["staff_role_id"] = staff_role_id
    TICKET_CFG[gid] = cfg


def extract_int(text: str, strict: bool) -> Optional[int]:
    m = (INT_STRICT if strict else INT_LOOSE).match(text)
    return int(m.group(1)) if m else None


# -------------------------------------------------
# ticket creation
# -------------------------------------------------
async def create_winner_ticket(
    guild: discord.Guild,
    winner: discord.Member,
    prize: str,
    n_hit: int
) -> Optional[discord.TextChannel]:
    cat_id, staff_role_id = get_ticket_cfg(guild.id)
    category = guild.get_channel(cat_id) if cat_id else None
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None

    if not isinstance(winner, discord.Member):
        with contextlib.suppress(Exception):
            winner = await guild.fetch_member(winner.id)

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
    chan = await guild.create_text_channel(
        name=name,
        category=category,
        overwrites=overwrites,
        reason="Prizo prize ticket",
    )
    with contextlib.suppress(Exception):
        await chan.edit(sync_permissions=False)

    claim_text = pick_banter("claim", "Please provide your IMVU link and prize details.")
    em = discord.Embed(
        title="🎟️ Prize Ticket",
        description=(
            f"🎟 Ticket for {winner.mention}\n\n"
            f"Please provide:\n"
            f"• **IMVU Account Link**\n"
            f"• **Lucky Number Won:** {n_hit}\n"
            f"• **Prize:** {prize}\n\n"
            f"{claim_text}"
        ),
        colour=discord.Colour.green()
    )
    em.set_footer(text=f"{guild.name} • Ticket")
    await chan.send(embed=em)
    return chan


# -------------------------------------------------
# mini-game: quick math
# -------------------------------------------------
async def run_quick_math(channel: discord.TextChannel, trigger_user: discord.Member, number_hit: int):
    """First correct answer wins ticket + ticket channel."""
    a = random.randint(2, 9)
    b = random.randint(2, 9)
    answer = a + b

    em = discord.Embed(
        title="🧠 Lucky Number Mini Game!",
        description=(
            f"{trigger_user.mention} hit **{number_hit}** 🎯\n"
            f"First to answer **{a} + {b}** wins a ticket!"
        ),
        colour=discord.Colour.gold(),
    )
    await channel.send(embed=em)

    def check(m: discord.Message):
        if m.author.bot:
            return False
        if m.channel.id != channel.id:
            return False
        try:
            val = int(m.content.strip())
        except ValueError:
            return False
        return val == answer

    try:
        winner_msg = await bot.wait_for("message", timeout=15.0, check=check)
    except asyncio.TimeoutError:
        await channel.send("⏱️ No one solved it. Mini game over.")
        return

    guild = channel.guild
    st = get_state(guild.id)
    st["tickets"].append(winner_msg.author.id)
    prize_text = st.get("lucky_prize", "Lucky number mini-game prize")

    ticket_chan = None
    with contextlib.suppress(Exception):
        ticket_chan = await create_winner_ticket(guild, winner_msg.author, prize_text, number_hit)

    winner_banter = pick_banter("winner", "We have a winner!")
    claim_banter = pick_banter("claim", "Open your ticket to claim.")

    extra = f" Ticket: {ticket_chan.mention}" if ticket_chan else " (no ticket category set)"
    await channel.send(
        f"🏆 {winner_msg.author.mention} {winner_banter} **{a} + {b} = {answer}**\n"
        f"🎟️ {claim_banter}{extra}"
    )

    # re-arm a new lucky number in same range
    st["lucky_target"] = random.randint(st["lucky_min"], st["lucky_max"])
    await channel.send(f"🧨 New lucky number armed. Keep counting.")


# -------------------------------------------------
# slash commands
# -------------------------------------------------
@bot.event
async def on_ready():
    print(f"[boot] logged in as {bot.user} ({bot.user.id})")

    # try per-guild sync first
    if bot.guilds:
        for g in bot.guilds:
            try:
                await bot.tree.sync(guild=g)
                print(f"[slash] synced to guild: {g.name} ({g.id})")
            except Exception as e:
                print(f"[slash] FAILED to sync to guild: {g.name} ({g.id}) -> {e}")

    # also try global sync (sometimes per-guild is blocked)
    try:
        await bot.tree.sync()
        print("[slash] global sync ok")
    except Exception as e:
        print(f"[slash] global sync failed -> {e}")

@bot.tree.command(name="set_ticket_category", description="Set the category where winner tickets will be created.")
@app_commands.guild_only()
async def set_ticket_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
    set_ticket_cfg(interaction.guild_id, category_id=category.id)
    await interaction.response.send_message(f"📂 Ticket category set to **{category.name}**.", ephemeral=True)


@bot.tree.command(name="set_ticket_staff", description="(Optional) Set staff role that can see prize tickets.")
@app_commands.guild_only()
async def set_ticket_staff(interaction: discord.Interaction, role: discord.Role):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
    set_ticket_cfg(interaction.guild_id, staff_role_id=role.id)
    await interaction.response.send_message(f"🛡️ Ticket staff set to **{role.name}**.", ephemeral=True)


@bot.tree.command(
    name="set_lucky_prize",
    description="Set the prize text for lucky-number winners."
)
@discord.app_commands.guild_only()
async def set_lucky_prize(interaction: discord.Interaction, prize: str):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message(
            "You need **Manage Server** permission.", ephemeral=True
        )
    st = get_state(interaction.guild_id)
    st["lucky_prize"] = prize  # <- "2WL" goes here
    await interaction.response.send_message(
        f"🏅 Lucky prize set to: **{prize}**", ephemeral=True
    )


@bot.tree.command(
    name="set_lucky_range",
    description="Set min/max for random lucky number and optional prize."
)
@discord.app_commands.describe(
    min_value="Smallest number the bot can arm as lucky",
    max_value="Biggest number the bot can arm as lucky",
    prize="Optional prize text, e.g. 2WL"
)
@discord.app_commands.guild_only()
async def set_lucky_range(
    interaction: discord.Interaction,
    min_value: discord.app_commands.Range[int, 1, 100000],
    max_value: discord.app_commands.Range[int, 1, 100000],
    prize: str | None = None,
):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message(
            "You need **Manage Server** permission.", ephemeral=True
        )
    if min_value >= max_value:
        return await interaction.response.send_message(
            "Min must be **less** than max.", ephemeral=True
        )

    st = get_state(interaction.guild_id)
    st["lucky_min"] = int(min_value)
    st["lucky_max"] = int(max_value)
    if prize is not None:
        st["lucky_prize"] = prize  # <- "2WL" goes here

    import random
    st["lucky_target"] = random.randint(st["lucky_min"], st["lucky_max"])

    await interaction.response.send_message(
        (
            f"🎯 Lucky range set to **{min_value}–{max_value}**.\n"
            f"Armed lucky number: **{st['lucky_target']}**.\n"
            f"Prize: **{st['lucky_prize']}**"
        ),
        ephemeral=True,
    )

@bot.tree.command(name="set_milestone_range", description="Set min/max for random milestone (announce only).")
@app_commands.describe(min_value="Minimum milestone", max_value="Maximum milestone")
@app_commands.guild_only()
async def set_milestone_range(
    interaction: discord.Interaction,
    min_value: app_commands.Range[int, 1, 100000],
    max_value: app_commands.Range[int, 1, 100000],
):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("You need **Manage Server** permission.", ephemeral=True)
    if min_value >= max_value:
        return await interaction.response.send_message("Min must be less than max.", ephemeral=True)

    st = get_state(interaction.guild_id)
    st["milestone_min"] = int(min_value)
    st["milestone_max"] = int(max_value)
    st["next_milestone"] = random.randint(st["milestone_min"], st["milestone_max"])

    await interaction.response.send_message(
        f"📢 Milestone range set to **{min_value}–{max_value}**. Next milestone: **{st['next_milestone']}**.",
        ephemeral=True
    )


# AI toggles (stored only)
@bot.tree.command(name="aibanter_on", description="Enable AI banter in counting channel.")
@app_commands.guild_only()
async def aibanter_on(interaction: discord.Interaction):
    ai_helper_enabled[interaction.guild_id] = True
    await interaction.response.send_message("✅ AI banter enabled.", ephemeral=True)


@bot.tree.command(name="aibanter_off", description="Disable AI banter in counting channel.")
@app_commands.guild_only()
async def aibanter_off(interaction: discord.Interaction):
    ai_helper_enabled[interaction.guild_id] = False
    await interaction.response.send_message("✅ AI banter disabled.", ephemeral=True)


@bot.tree.command(name="aibanter_idle", description="Set minutes of silence before AI speaks.")
@app_commands.describe(minutes="1–60")
@app_commands.guild_only()
async def aibanter_idle(interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 60]):
    ai_idle_minutes[interaction.guild_id] = int(minutes)
    await interaction.response.send_message(f"⏱️ AI banter idle set to **{int(minutes)} min**.", ephemeral=True)


# -------------------------------------------------
# prefix commands
# -------------------------------------------------
@bot.command(name="words")
@commands.has_permissions(manage_guild=True)
async def cmd_words(ctx: commands.Context):
    st = get_state(ctx.guild.id)
    st["words_only"] = True
    await ctx.reply("🗣️ Words-only mode enabled. Use `one, two, three...`", mention_author=False)


@bot.command(name="numbers")
@commands.has_permissions(manage_guild=True)
async def cmd_numbers(ctx: commands.Context):
    st = get_state(ctx.guild.id)
    st["words_only"] = False
    await ctx.reply("🔢 Plain number mode enabled. Use `1, 2, 3...`", mention_author=False)


@bot.command(name="tickets")
@commands.has_permissions(manage_guild=True)
async def cmd_tickets(ctx: commands.Context):
    st = get_state(ctx.guild.id)
    if not st["tickets"]:
        await ctx.reply("🎟️ No tickets yet.", mention_author=False)
        return

    counts: Dict[int, int] = {}
    for uid in st["tickets"]:
        counts[uid] = counts.get(uid, 0) + 1

    lines = []
    for uid, cnt in counts.items():
        member = ctx.guild.get_member(uid)
        name = member.mention if member else f"<@{uid}>"
        lines.append(f"{name}: **{cnt}** ticket(s)")
    await ctx.reply("🎟️ Tickets so far:\n" + "\n".join(lines), mention_author=False)


# -------------------------------------------------
# counting handler
# -------------------------------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # allow prefix commands
    await bot.process_commands(message)

    gid = message.guild.id
    st = get_state(gid)

    # check locks
    locks = st["locks"]
    now = datetime.utcnow()
    if message.author.id in locks:
        if now < locks[message.author.id]:
            with contextlib.suppress(Exception):
                await message.delete()
            return
        else:
            del locks[message.author.id]

    # ----- extract posted number -----
    if st["words_only"]:
        raw = message.content.strip().lower()
        posted = WORD_NUMBERS.get(raw)
    else:
        posted = extract_int(message.content, strict=False)

    # ignore non-number chat
    if posted is None:
        return

    expected = st["current_number"] + 1
    last_user = st["last_user_id"]

    # ----- no two in a row -----
    if last_user == message.author.id:
        banter_line = pick_banter("wrong", "Not two in a row.")
        with contextlib.suppress(Exception):
            await message.add_reaction("⛔")
        await message.channel.send(
            f"{message.author.mention} {banter_line} Next is **{expected}** for someone else."
        )
        return

    # ----- WRONG NUMBER -----
    if posted != expected:
        key = (message.channel.id, message.author.id)
        st["wrong_streak"][key] = st["wrong_streak"].get(key, 0) + 1

        # reset back to 1
        st["current_number"] = 0
        st["last_user_id"] = None

        wrong_line = pick_banter("wrong", "Wrong number.")
        await message.channel.send(
            f"❌ {wrong_line} {message.author.mention} Count is back to **1**."
        )

        # optional 3-wrong bench
        if st["wrong_streak"][key] >= 3:
            st["wrong_streak"][key] = 0
            ban_minutes = st["ban_minutes"]
            until = datetime.utcnow() + timedelta(minutes=ban_minutes)
            st["locks"][message.author.id] = until
            roast = pick_banter("roast", "Have a sit-down and count sheep, not numbers.")
            await message.channel.send(
                f"🚫 {message.author.mention} benched for **{ban_minutes} minutes**. {roast}"
            )
        return  # <- important, stop here for wrong

    # ----- SUCCESS -----
    st["current_number"] = expected
    st["last_user_id"] = message.author.id
    st["wrong_streak"][(message.channel.id, message.author.id)] = 0

    with contextlib.suppress(Exception):
        await message.add_reaction("✅")

    # milestone (dynamic)
    if expected == st.get("next_milestone"):
        mile_line = pick_banter("milestone", f"Milestone {expected} smashed!")
        em = discord.Embed(
            title="🎉 Milestone!",
            description=f"{mile_line}\nCount reached **{expected}** by {message.author.mention}",
            colour=discord.Colour.gold()
        )
        await message.channel.send(embed=em)
        # re-arm next milestone
        st["next_milestone"] = random.randint(st["milestone_min"], st["milestone_max"])

    # lucky number → mini game
    if expected == st.get("lucky_target"):
        with contextlib.suppress(Exception):
            await run_quick_math(message.channel, message.author, expected)

bot.run(TOKEN)
