# bot.py (minimal test)
import os
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))  # <-- set this in Railway Variables

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            synced = await bot.tree.sync(guild=guild)   # fast, per-guild
            print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await bot.tree.sync()              # global (can be slow the first time)
            print(f"Synced {len(synced)} commands globally")
    except Exception as e:
        print("Sync error:", e)
    print(f"Logged in as {bot.user} ({bot.user.id})")

@bot.tree.command(name="ping", description="Test that slash commands work")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong ✅", ephemeral=True)

# owner-only emergency sync you can run from Discord
@bot.tree.command(name="sync", description="Force re-sync commands (owner only)")
async def sync_cmd(interaction: discord.Interaction):
    if interaction.user.id != bot.owner_id and not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Nope.", ephemeral=True)
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            synced = await bot.tree.sync(guild=guild)
            await interaction.response.send_message(f"Resynced {len(synced)} commands to this guild.", ephemeral=True)
        else:
            synced = await bot.tree.sync()
            await interaction.response.send_message(f"Resynced {len(synced)} commands globally.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Sync error: {e}", ephemeral=True)

bot.run(TOKEN)
