import os
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN env var")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    # Global sync (most reliable)
    try:
        synced = await bot.tree.sync()
        print(f"Slash commands synced globally: {len(synced)}")
    except Exception as e:
        print("Sync error:", e)
    print(f"Logged in as {bot.user} ({bot.user.id})")

@bot.tree.command(name="ping", description="Test that slash commands work")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong ✅", ephemeral=True)

bot.run(TOKEN)
