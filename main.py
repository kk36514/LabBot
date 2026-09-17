

import discord, sqlite3, os, csv, io, datetime, asyncio
from collections import Counter
from discord.ext import commands
import anthropic

# Removed Google Colab imports and Drive mounting since they are incompatible with Render.



import sqlite3
import os

# Using local project directory instead of Google Drive
DB_PATH = "intel.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

# Ensure the base table exists first
cursor.execute("""CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org TEXT NOT NULL,
    action TEXT NOT NULL,
    location TEXT DEFAULT 'unknown',
    notes TEXT DEFAULT '',
    reported_by TEXT,
    anonymous INTEGER DEFAULT 0,
    timestamp TEXT NOT NULL
)""")

# Check for missing columns and add them one by one
cursor.execute("PRAGMA table_info(reports)")
existing_cols = [row[1] for row in cursor.fetchall()]

migrations = [
    ('verified', 'INTEGER DEFAULT 0'),
    ('tags', 'TEXT DEFAULT ""'),
    ('lat', 'REAL'),
    ('lon', 'REAL')
]

for col_name, col_type in migrations:
    if col_name not in existing_cols:
        print(f"Migrating: Adding column {col_name}")
        cursor.execute(f"ALTER TABLE reports ADD COLUMN {col_name} {col_type}")

# Settings and Watches tables
cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
cursor.execute("CREATE TABLE IF NOT EXISTS watches (keyword TEXT PRIMARY KEY, channel_id TEXT)")

conn.commit()
DEFAULT_ORG = "The Lab"
print("Database prepared and migrations applied.")

import discord
from discord.ext import commands
import anthropic
import datetime
import asyncio
import sqlite3
import csv
import io
import folium
import os
from collections import Counter

# Safely handle google.colab imports for local/Render deployment compatibility
try:
    from google.colab import userdata, drive, files
    HAS_COLAB = True
except ImportError:
    HAS_COLAB = False
    class UserdataMock:
        def get(self, key):
            return os.environ.get(key)
    userdata = UserdataMock()

# Define intents BEFORE creating the bot instance
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

anthropic_api_key = userdata.get("ANTHROPIC_API_KEY")
client = anthropic.AsyncAnthropic(api_key=anthropic_api_key) if anthropic_api_key else None

def get_setting(key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None

def set_setting(key, value):
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()

def check_access(interaction):
    required = get_setting("required_role")
    if required and not any(r.name.lower() == required.lower() for r in interaction.user.roles):
        return False
    return True

# --- core report / track ---
@bot.tree.command(name="report", description="File a report on activity")
async def report(interaction: discord.Interaction, action: str, org: str = DEFAULT_ORG, location: str = "unknown", notes: str = "", anonymous: bool = False, tags: str = "", lat: float = None, lon: float = None):
    await interaction.response.defer(ephemeral=True)
    if not check_access(interaction):
        await interaction.followup.send("❌ Permission denied.", ephemeral=True)
        return
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    reported_by = "anonymous" if anonymous else str(interaction.user)
    try:
        params = (org, action, location, notes, reported_by, int(anonymous), tags or "", lat, lon, ts)
        cursor = conn.execute("INSERT INTO reports (org, action, location, notes, reported_by, anonymous, tags, lat, lon, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", params)
        conn.commit()
        report_id = cursor.lastrowid
        await interaction.followup.send(f"✅ Report logged as `#{report_id}` for **{org}**.", ephemeral=True)

        # Check watches
        watches = conn.execute("SELECT keyword, channel_id FROM watches").fetchall()
        for kw, ch_id in watches:
            if kw.lower() in action.lower() or kw.lower() in notes.lower():
                try:
                    channel = bot.get_channel(int(ch_id)) or await bot.fetch_channel(int(ch_id))
                    if channel:
                        await channel.send(f"🚨 **Watch Alert!** Keyword `{kw}` matched in report `#{report_id}` ({org} - {action})")
                except Exception:
                    pass
    except Exception as e:
        await interaction.followup.send(f"❌ Database Error: {e}", ephemeral=True)

@bot.tree.command(name="track", description="Show recent tracked activity")
async def track(interaction: discord.Interaction, org: str = DEFAULT_ORG, limit: int = 10):
    await interaction.response.defer(ephemeral=True)
    if not check_access(interaction):
        await interaction.followup.send("❌ No permission.", ephemeral=True); return
    rows = conn.execute("SELECT id, action, location, timestamp FROM reports WHERE lower(org) = lower(?) ORDER BY id DESC LIMIT ?", (org, limit)).fetchall()
    if not rows:
        await interaction.followup.send(f"No reports on **{org}** yet.", ephemeral=True); return
    lines = [f"• `#{r[0]}` **{r[1]}** — {r[2]} ({r[3][:16]})" for r in rows]
    await interaction.followup.send(embed=discord.Embed(title=f"Intel on {org}", description="\n".join(lines), color=discord.Color.red()))

import pandas as pd
import io
import folium

# --- Clear existing commands from the tree to prevent registration errors ---
bot.tree.clear_commands(guild=None)

# --- Analytics & Intel ---

@bot.tree.command(name="summary", description="Count actions per org")
async def summary(interaction: discord.Interaction, org: str = DEFAULT_ORG):
    await interaction.response.defer(ephemeral=True)
    if not check_access(interaction): return
    rows = conn.execute("SELECT action, COUNT(*) FROM reports WHERE lower(org) = lower(?) GROUP BY action", (org,)).fetchall()
    if not rows:
        await interaction.followup.send(f"No data for {org}."); return
    desc = "\n".join([f"**{action}**: {count}" for action, count in rows])
    await interaction.followup.send(embed=discord.Embed(title=f"Activity Summary: {org}", description=desc, color=discord.Color.blue()))

@bot.tree.command(name="location", description="List sightings at a specific place")
async def location(interaction: discord.Interaction, loc: str):
    await interaction.response.defer(ephemeral=True)
    if not check_access(interaction): return
    rows = conn.execute("SELECT id, org, action, timestamp FROM reports WHERE location LIKE ? ORDER BY id DESC", (f"%{loc}%",)).fetchall()
    if not rows:
        await interaction.followup.send(f"No reports found for: {loc}"); return
    lines = [f"`#{r[0]}` **{r[1]}**: {r[2]} ({r[3][:10]})" for r in rows]
    await interaction.followup.send(embed=discord.Embed(title=f"Sightings at {loc}", description="\n".join(lines[:15])))

@bot.tree.command(name="timeline", description="Full chronological view of an org")
async def timeline(interaction: discord.Interaction, org: str = DEFAULT_ORG):
    await interaction.response.defer(ephemeral=True)
    rows = conn.execute("SELECT timestamp, action, notes FROM reports WHERE lower(org) = lower(?) ORDER BY timestamp DESC LIMIT 20", (org,)).fetchall()
    if not rows:
        await interaction.followup.send("No timeline data."); return
    msg = "\n".join([f"`{r[0][:16]}`: {r[1]} - {r[2]}" for r in rows])
    await interaction.followup.send(embed=discord.Embed(title=f"Timeline: {org}", description=msg))

# --- Management ---

@bot.tree.command(name="delete_report", description="Delete a report")
async def delete_report(interaction: discord.Interaction, report_id: int):
    if not check_access(interaction): return
    conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    conn.commit()
    await interaction.response.send_message(f"🗑️ Report `#{report_id}` deleted.")

@bot.tree.command(name="verify", description="Mark a report as verified")
async def verify(interaction: discord.Interaction, report_id: int):
    if not check_access(interaction): return
    conn.execute("UPDATE reports SET verified = 1 WHERE id = ?", (report_id,))
    conn.commit()
    await interaction.response.send_message(f"✅ Report `#{report_id}` verified.")

# --- Alerts & AI ---

@bot.tree.command(name="watch", description="Alert a channel when a keyword appears")
async def watch(interaction: discord.Interaction, keyword: str, channel: discord.TextChannel = None):
    if not check_access(interaction): return
    ch_id = str(channel.id) if channel else str(interaction.channel_id)
    conn.execute("INSERT OR REPLACE INTO watches (keyword, channel_id) VALUES (?, ?)", (keyword.lower(), ch_id))
    conn.commit()
    await interaction.response.send_message(f"👀 Watching for `{keyword}` in <#{ch_id}>.")

@bot.tree.command(name="brief", description="Generate a full AI brief on an org")
async def brief(interaction: discord.Interaction, org: str = DEFAULT_ORG):
    await interaction.response.defer()
    if not client:
        await interaction.followup.send("AI not configured."); return
    rows = conn.execute("SELECT action, notes, timestamp FROM reports WHERE lower(org) = lower(?) LIMIT 10", (org,)).fetchall()
    data = "\n".join([str(r) for r in rows])
    resp = await client.messages.create(
        model="claude-3-haiku-20240307", max_tokens=1000,
        messages=[{"role": "user", "content": f"Provide a strategic brief for the organization '{org}' based on these logs: {data}"}]
    )
    await interaction.followup.send(resp.content[0].text)

# --- Utility ---

@bot.tree.command(name="help", description="List all commands")
async def help_cmd(interaction: discord.Interaction):
    h = "**Intel**: /report, /track, /summary, /location, /trend, /timeline, /stats, /export, /heatmap\n" \
        "**Alerts**: /watch, /unwatch, /digest_channel, /quiet_hours\n" \
        "**Management**: /verify, /tag, /edit_report, /delete_report, /role_required\n" \
        "**AI**: /ask, /analyze, /brief"
    await interaction.response.send_message(embed=discord.Embed(title="LabBot Command List", description=h))

GUILD_ID = 1549521458320113684  # your server ID

@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    bot.tree.clear_commands(guild=guild)   # wipe the server-side copies
    await bot.tree.sync(guild=guild)        # push the empty list to the guild
    await bot.tree.sync()                   # keep the global set
    print(f"Online as {bot.user}")


import asyncio
import os

# The bot, database, and events are already configured in previous cells.
# This cell serves as the final runtime entry point.

try:
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(reports)")
    columns = [row[1] for row in cursor.fetchall()]

    # Migration check to ensure schema consistency
    for col, ctype in [('tags', 'TEXT'), ('verified', 'INTEGER'), ('lat', 'REAL'), ('lon', 'REAL')]:
        if col not in columns:
            conn.execute(f"ALTER TABLE reports ADD COLUMN {col} {ctype}")
    conn.commit()
    print("Database schema verified for runtime.")
except Exception as e:
    print(f"Database runtime check failed: {e}")

async def main():
    # Priority: Env variable 'BOT_API' -> Env variable 'DISCORD_BOT_TOKEN' -> Colab Secret
    token = os.environ.get("BOT_API") or os.environ.get("DISCORD_BOT_TOKEN")
    
    if not token and HAS_COLAB:
        try:
            from google.colab import userdata
            token = userdata.get("DISCORD_BOT_TOKEN")
        except Exception:
            pass

    if token:
        print("Starting bot instance...")
        async with bot:
            await bot.start(token)
    else:
        print("Error: No Discord token found in environment variables or Colab secrets.")

if __name__ == "__main__":
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # For Colab: Use existing loop
            asyncio.ensure_future(main())
        else:
            # For Render/Local: Run new loop
            asyncio.run(main())
    except Exception as e:
        print(f"Failed to start the bot runner loop: {e}")
