# -*- coding: utf-8 -*-
"""LabBot — Discord intel bot with Firebase Realtime Database persistence."""

import discord, os, io, datetime, asyncio
from collections import Counter
from discord.ext import commands
import anthropic
import pandas as pd
import folium
from aiohttp import web

# --- Firebase / database setup ---

import firebase_admin
from firebase_admin import credentials, db

DB_PATH = "intel.db"   # legacy SQLite fallback; primary storage is Firebase
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

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

cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
cursor.execute("CREATE TABLE IF NOT EXISTS watches (keyword TEXT PRIMARY KEY, channel_id TEXT)")
conn.commit()
DEFAULT_ORG = "The Lab"
print("Database prepared (SQLite legacy schema).")

# --- Firebase init (primary storage) ---

_firebase_ok = False
try:
    import json as _json
    _svc = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if _svc:
        _cred = credentials.Certificate(_json.loads(_svc))
    elif os.path.exists("firebase-service-account.json"):
        _cred = credentials.Certificate("firebase-service-account.json")
    else:
        raise RuntimeError("No Firebase credentials found (set FIREBASE_SERVICE_ACCOUNT env var or add firebase-service-account.json).")

    _db_url = os.environ.get("FIREBASE_DATABASE_URL", "https://labbot-c5047-default-rtdb.firebaseio.com")
    firebase_admin.initialize_app(_cred, {"databaseURL": _db_url})
    _firebase_ok = True
    print("Firebase Realtime Database connected.")
except Exception as e:
    print(f"⚠️ Firebase init failed: {e} — falling back to SQLite only.")

def get_reports_dict():
    """Return all reports as {id: report_dict} from Firebase (or SQLite fallback)."""
    if _firebase_ok:
        return db.reference("reports").get() or {}
    rows = conn.execute("SELECT * FROM reports").fetchall()
    cols = [d[1] for d in cursor.execute("PRAGMA table_info(reports)").fetchall()]
    return {str(r[0]): dict(zip(cols, r)) for r in rows}

def get_reports_sorted(limit=20):
    """Newest-first list of (id, report) tuples."""
    reports = get_reports_dict()
    items = sorted(reports.items(), key=lambda kv: str(kv[1].get("timestamp", "")), reverse=True)
    return items[:limit]

def save_report(report_dict):
    """Insert a report; returns its Firebase key (or SQLite id as string)."""
    if _firebase_ok:
        ref = db.reference("reports").push(report_dict)
        return ref.key
    ts = report_dict.get("timestamp")
    org = report_dict.get("org", DEFAULT_ORG)
    action = report_dict.get("action")
    location = report_dict.get("location", "unknown")
    notes = report_dict.get("notes", "")
    reported_by = report_dict.get("reported_by", "anonymous")
    anonymous = int(report_dict.get("anonymous", 0))
    tags = report_dict.get("tags", "")
    lat = report_dict.get("lat")
    lon = report_dict.get("lon")
    verified = int(report_dict.get("verified", 0))
    cur = conn.execute(
        "INSERT INTO reports (org, action, location, notes, reported_by, anonymous, tags, lat, lon, verified, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (org, action, location, notes, reported_by, anonymous, tags, lat, lon, verified, ts)
    )
    conn.commit()
    return str(cur.lastrowid)

def get_setting(key):
    if _firebase_ok:
        return db.reference(f"settings/{key}").get()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None

def set_setting(key, value):
    if _firebase_ok:
        db.reference(f"settings/{key}").set(value)
        return
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()

def check_access(interaction):
    required = get_setting("required_role")
    if required and not any(r.name.lower() == required.lower() for r in interaction.user.roles):
        return False
    return True

# --- Colab compatibility ---

try:
    from google.colab import userdata, drive, files
    HAS_COLAB = True
except ImportError:
    HAS_COLAB = False
    class UserdataMock:
        def get(self, key):
            return os.environ.get(key)
    userdata = UserdataMock()

# --- Bot setup ---

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
anthropic_api_key = userdata.get("ANTHROPIC_API_KEY")
client = anthropic.AsyncAnthropic(api_key=anthropic_api_key) if anthropic_api_key else None

# --- Core report / track ---

@bot.tree.command(name="report", description="File a report on activity")
async def report(interaction: discord.Interaction, action: str, org: str = DEFAULT_ORG, location: str = "unknown", notes: str = "", anonymous: bool = False, tags: str = "", lat: float = None, lon: float = None):
    await interaction.response.defer(ephemeral=True)
    if not check_access(interaction):
        await interaction.followup.send("❌ Permission denied.", ephemeral=True)
        return
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    reported_by = "anonymous" if anonymous else str(interaction.user)
    try:
        report_id = save_report({
            "org": org, "action": action, "location": location, "notes": notes,
            "reported_by": reported_by, "anonymous": int(anonymous), "tags": tags or "",
            "lat": lat, "lon": lon, "verified": 0, "timestamp": ts
        })
        await interaction.followup.send(f"✅ Report logged as `#{report_id}` for **{org}**.", ephemeral=True)

        # Check watches
        if _firebase_ok:
            w = db.reference("watches").get() or {}
            watches = [(k, v.get("channel_id", "")) for k, v in w.items()]
        else:
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
        await interaction.followup.send("❌ No permission.", ephemeral=True)
        return
    reports = get_reports_dict()
    rows = [(rid, r) for rid, r in reports.items() if str(r.get("org", "")).lower() == org.lower()]
    rows.sort(key=lambda kv: str(kv[1].get("timestamp", "")), reverse=True)
    rows = rows[:limit]
    if not rows:
        await interaction.followup.send(f"No reports on **{org}** yet.", ephemeral=True)
        return
    lines = [f"• `#{rid}` **{r.get('action')}** — {r.get('location')} ({str(r.get('timestamp', ''))[:16]})" for rid, r in rows]
    await interaction.followup.send(embed=discord.Embed(title=f"Intel on {org}", description="\n".join(lines), color=discord.Color.red()))

# --- Analytics & Intel ---

@bot.tree.command(name="summary", description="Count actions per org")
async def summary(interaction: discord.Interaction, org: str = DEFAULT_ORG):
    await interaction.response.defer(ephemeral=True)
    if not check_access(interaction):
        return
    reports = get_reports_dict()
    counts = Counter(r.get("action", "?") for r in reports.values() if str(r.get("org", "")).lower() == org.lower())
    if not counts:
        await interaction.followup.send(f"No data for {org}.")
        return
    desc = "\n".join([f"**{action}**: {count}" for action, count in counts.most_common()])
    await interaction.followup.send(embed=discord.Embed(title=f"Activity Summary: {org}", description=desc, color=discord.Color.blue()))

@bot.tree.command(name="location", description="List sightings at a specific place")
async def location(interaction: discord.Interaction, loc: str):
    await interaction.response.defer(ephemeral=True)
    if not check_access(interaction):
        return
    reports = get_reports_dict()
    rows = [(rid, r) for rid, r in reports.items() if loc.lower() in str(r.get("location", "")).lower()]
    rows.sort(key=lambda kv: str(kv[1].get("timestamp", "")), reverse=True)
    lines = [f"`#{rid}` **{r.get('org')}**: {r.get('action')} ({str(r.get('timestamp', ''))[:10]})" for rid, r in rows[:15]]
    if not lines:
        await interaction.followup.send(f"No reports found for: {loc}")
        return
    await interaction.followup.send(embed=discord.Embed(title=f"Sightings at {loc}", description="\n".join(lines)))

@bot.tree.command(name="trend", description="Reports per day over the last N days")
async def trend(interaction: discord.Interaction, days: int = 7):
    await interaction.response.defer(ephemeral=True)
    reports = get_reports_dict()
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
    days_count = Counter()
    for r in reports.values():
        ts = str(r.get("timestamp", ""))
        if ts >= cutoff:
            days_count[ts[:10]] += 1
    if not days_count:
        await interaction.followup.send("No recent data for trends.")
        return
    msg = "\n".join([f"`{d}`: {c} reports" for d, c in sorted(days_count.items())])
    await interaction.followup.send(embed=discord.Embed(title=f"Intel Trend (Last {days} days)", description=msg))

@bot.tree.command(name="timeline", description="Full chronological view of an org")
async def timeline(interaction: discord.Interaction, org: str = DEFAULT_ORG):
    await interaction.response.defer(ephemeral=True)
    reports = get_reports_dict()
    rows = [(rid, r) for rid, r in reports.items() if str(r.get("org", "")).lower() == org.lower()]
    rows.sort(key=lambda kv: str(kv[1].get("timestamp", "")), reverse=True)
    msg = "\n".join([f"`{str(r.get('timestamp', ''))[:16]}`: {r.get('action')} - {r.get('notes', '')}" for rid, r in rows[:20]])
    if not msg:
        await interaction.followup.send("No timeline data.")
        return
    await interaction.followup.send(embed=discord.Embed(title=f"Timeline: {org}", description=msg))

@bot.tree.command(name="keywords", description="Search notes and actions for keywords")
async def keywords(interaction: discord.Interaction, search_term: str):
    await interaction.response.defer(ephemeral=True)
    if not check_access(interaction):
        return
    reports = get_reports_dict()
    rows = [(rid, r) for rid, r in reports.items()
            if search_term.lower() in str(r.get("notes", "")).lower() or search_term.lower() in str(r.get("action", "")).lower()]
    rows.sort(key=lambda kv: str(kv[1].get("timestamp", "")), reverse=True)
    lines = [f"`#{rid}` **{r.get('org')}**: {r.get('action')} - {str(r.get('notes', ''))[:50]}..." for rid, r in rows[:15]]
    if not lines:
        await interaction.followup.send(f"No reports found for keywords: {search_term}")
        return
    await interaction.followup.send(embed=discord.Embed(title=f"Keyword Search: {search_term}", description="\n".join(lines)))

@bot.tree.command(name="stats", description="Overall intel stats")
async def stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    reports = get_reports_dict()
    total = len(reports)
    verified = sum(1 for r in reports.values() if r.get("verified"))
    top = Counter(str(r.get("reported_by", "?")) for r in reports.values()).most_common(1)
    embed = discord.Embed(title="Global Intel Stats", color=discord.Color.gold())
    embed.add_field(name="Total Reports", value=str(total))
    embed.add_field(name="Verified", value=str(verified))
    if top:
        embed.add_field(name="Top Reporter", value=f"{top[0][0]} ({top[0][1]})")
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="heatmap", description="Render a folium map of geotagged reports")
async def heatmap(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    reports = get_reports_dict()
    rows = [(rid, r) for rid, r in reports.items() if r.get("lat") is not None and r.get("lon") is not None]
    if not rows:
        await interaction.followup.send("No geotagged reports available to map.")
        return
    map_center = [float(rows[0][1]["lat"]), float(rows[0][1]["lon"])]
    m = folium.Map(location=map_center, zoom_start=2)
    for rid, r in rows:
        try:
            folium.Marker([float(r["lat"]), float(r["lon"])], popup=f"{r.get('org')}: {r.get('action')}").add_to(m)
        except (ValueError, TypeError):
            continue
    html_path = "map.html"
    m.save(html_path)
    await interaction.followup.send("Generated Heatmap:", file=discord.File(html_path))

# --- Management ---

@bot.tree.command(name="edit_report", description="Edit a field of a report")
async def edit_report(interaction: discord.Interaction, report_id: str, field: str, new_value: str):
    await interaction.response.defer(ephemeral=True)
    if not check_access(interaction):
        return
    valid_fields = ["org", "action", "location", "notes", "tags", "lat", "lon"]
    if field not in valid_fields:
        await interaction.followup.send(f"Invalid field. Choose from: {', '.join(valid_fields)}")
        return
    if field in ["lat", "lon"]:
        try:
            new_value = float(new_value)
        except ValueError:
            await interaction.followup.send(f"Invalid value for {field}. Must be a number.")
            return
    if _firebase_ok:
        db.reference(f"reports/{report_id}/{field}").set(new_value)
    else:
        conn.execute(f"UPDATE reports SET {field} = ? WHERE id = ?", (new_value, int(report_id)))
        conn.commit()
    await interaction.followup.send(f"✅ Report `#{report_id}` {field} updated to `{new_value}`.")

@bot.tree.command(name="delete_report", description="Delete a report")
async def delete_report(interaction: discord.Interaction, report_id: str):
    if not check_access(interaction):
        return
    if _firebase_ok:
        db.reference(f"reports/{report_id}").delete()
    else:
        conn.execute("DELETE FROM reports WHERE id = ?", (int(report_id),))
        conn.commit()
    await interaction.response.send_message(f"🗑️ Report `#{report_id}` deleted.")

@bot.tree.command(name="verify", description="Mark a report as verified")
async def verify(interaction: discord.Interaction, report_id: str):
    if not check_access(interaction):
        return
    if _firebase_ok:
        db.reference(f"reports/{report_id}/verified").set(1)
    else:
        conn.execute("UPDATE reports SET verified = 1 WHERE id = ?", (int(report_id),))
        conn.commit()
    await interaction.response.send_message(f"✅ Report `#{report_id}` verified.")

@bot.tree.command(name="tag", description="Add tags to a report")
async def tag(interaction: discord.Interaction, report_id: str, tags: str):
    if not check_access(interaction):
        return
    if _firebase_ok:
        db.reference(f"reports/{report_id}/tags").set(tags)
    else:
        conn.execute("UPDATE reports SET tags = ? WHERE id = ?", (tags, int(report_id)))
        conn.commit()
    await interaction.response.send_message(f"🏷️ Tags updated for `#{report_id}`.")

# --- Alerts & Access ---

@bot.tree.command(name="watch", description="Alert a channel when a keyword appears")
async def watch(interaction: discord.Interaction, keyword: str, channel: discord.TextChannel = None):
    if not check_access(interaction):
        return
    ch_id = str(channel.id) if channel else str(interaction.channel_id)
    if _firebase_ok:
        db.reference(f"watches/{keyword.lower()}").set({"channel_id": ch_id})
    else:
        conn.execute("INSERT OR REPLACE INTO watches (keyword, channel_id) VALUES (?, ?)", (keyword.lower(), ch_id))
        conn.commit()
    await interaction.response.send_message(f"👀 Watching for `{keyword}` in <#{ch_id}>.")

@bot.tree.command(name="unwatch", description="Remove a keyword watch")
async def unwatch(interaction: discord.Interaction, keyword: str):
    if not check_access(interaction):
        return
    if _firebase_ok:
        db.reference(f"watches/{keyword.lower()}").delete()
    else:
        conn.execute("DELETE FROM watches WHERE lower(keyword) = lower(?)", (keyword,))
        conn.commit()
    await interaction.response.send_message(f"🚫 Stopped watching for `{keyword}`.")

@bot.tree.command(name="digest_channel", description="Set the channel for daily intel digests")
async def digest_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not check_access(interaction):
        return
    set_setting("digest_channel_id", str(channel.id))
    await interaction.response.send_message(f"✅ Daily intel digests will be sent to {channel.mention}.")

@bot.tree.command(name="quiet_hours", description="Suppress digests between set hours (e.g., '22:00-06:00')")
async def quiet_hours(interaction: discord.Interaction, hours_range: str = ""):
    if not check_access(interaction):
        return
    set_setting("quiet_hours", hours_range)
    if hours_range:
        await interaction.response.send_message(f"✅ Quiet hours set to {hours_range}.")
    else:
        await interaction.response.send_message("✅ Quiet hours disabled.")

@bot.tree.command(name="role_required", description="Restrict commands to a role (blank = no restriction)")
async def role_required(interaction: discord.Interaction, role_name: str = ""):
    if not check_access(interaction):
        return
    set_setting("required_role", role_name.lower())
    if role_name:
        await interaction.response.send_message(f"✅ Commands now restricted to users with the '{role_name}' role.")
    else:
        await interaction.response.send_message("✅ Command restrictions removed.")

# --- AI Commands ---

@bot.tree.command(name="ask", description="Ask LabBot anything (context-aware)")
async def ask(interaction: discord.Interaction, question: str):
    await interaction.response.defer()
    if not client:
        await interaction.followup.send("AI Client not configured.")
        return
    reports = get_reports_dict()
    recent = sorted(reports.items(), key=lambda kv: str(kv[1].get("timestamp", "")), reverse=True)[:5]
    context = "Recent Reports:\n" + "\n".join([f"{r.get('org')} did {r.get('action')}: {r.get('notes')}" for _, r in recent])
    resp = await client.messages.create(
        model="claude-3-haiku-20240307", max_tokens=500,
        messages=[{"role": "user", "content": f"Context: {context}\n\nQuestion: {question}"}]
    )
    await interaction.followup.send(resp.content[0].text)

@bot.tree.command(name="analyze", description="Analyze recent intel with context")
async def analyze(interaction: discord.Interaction, prompt: str = "Summarize recent activity."):
    await interaction.response.defer()
    if not client:
        await interaction.followup.send("AI Client not configured.")
        return
    reports = get_reports_dict()
    recent = sorted(reports.items(), key=lambda kv: str(kv[1].get("timestamp", "")), reverse=True)[:10]
    report_str = "\n".join([f"Org: {r.get('org')}, Action: {r.get('action')}, Notes: {r.get('notes')}, Timestamp: {r.get('timestamp')}" for _, r in recent])
    full_prompt = f"Based on the following recent intel reports, {prompt}:\n\n{report_str}\n\nAnalysis:"
    resp = await client.messages.create(
        model="claude-3-haiku-20240307", max_tokens=1000,
        messages=[{"role": "user", "content": full_prompt}]
    )
    await interaction.followup.send(resp.content[0].text)

@bot.tree.command(name="brief", description="Generate a full AI brief on an org")
async def brief(interaction: discord.Interaction, org: str = DEFAULT_ORG):
    await interaction.response.defer()
    if not client:
        await interaction.followup.send("AI not configured.")
        return
    reports = get_reports_dict()
    rows = [(rid, r) for rid, r in reports.items() if str(r.get("org", "")).lower() == org.lower()]
    rows.sort(key=lambda kv: str(kv[1].get("timestamp", "")), reverse=True)
    data = "\n".join([str(r) for _, r in rows[:10]])
    resp = await client.messages.create(
        model="claude-3-haiku-20240307", max_tokens=1000,
        messages=[{"role": "user", "content": f"Provide a strategic brief for the organization '{org}' based on these logs: {data}"}]
    )
    await interaction.followup.send(resp.content[0].text)

# --- System & Utility ---

@bot.tree.command(name="export", description="Export all intel to CSV")
async def export_intel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    reports = get_reports_dict()
    df = pd.DataFrame.from_dict(reports, orient="index")
    if df.empty:
        df = pd.DataFrame(columns=["org", "action", "location", "notes", "reported_by", "timestamp"])
    buf = io.BytesIO()
    df.to_csv(buf, index=True, index_label="id")
    buf.seek(0)
    await interaction.followup.send("Full Export:", file=discord.File(buf, "intel_export.csv"))

@bot.tree.command(name="help", description="List all commands")
async def help_cmd(interaction: discord.Interaction):
    h = ("**Intel**: /report, /track, /summary, /location, /trend, /timeline, /keywords, /stats, /export, /heatmap\n"
         "**Alerts**: /watch, /unwatch, /digest_channel, /quiet_hours\n"
         "**Management**: /verify, /tag, /edit_report, /delete_report, /role_required\n"
         "**AI**: /ask, /analyze, /brief")
    await interaction.response.send_message(embed=discord.Embed(title="LabBot Command List", description=h))

GUILD_ID = 1549521458320113684  # your server ID

@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    bot.tree.clear_commands(guild=guild)   # wipe the server-side copies
    await bot.tree.sync(guild=guild)        # push the empty list to the guild
    await bot.tree.sync()                   # keep the global set
    print(f"Online as {bot.user}")

# --- Web server: dashboard + keep-alive ---

async def handle_ping(request):
    return web.Response(text="pong")

def render_dashboard():
    """Build the polished dashboard HTML from Firebase data."""
    reports = get_reports_dict()
    total = len(reports)
    verified = sum(1 for r in reports.values() if r.get("verified"))
    geotagged = sum(1 for r in reports.values()
                     if r.get("lat") is not None and r.get("lon") is not None)

    org_counts = {}
    action_counts = {}
    for r in reports.values():
        org_counts[r.get("org", "unknown")] = org_counts.get(r.get("org", "unknown"), 0) + 1
        action_counts[r.get("action", "?")] = action_counts.get(r.get("action", "?"), 0) + 1

    top_orgs = sorted(org_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
    top_actions = sorted(action_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
    recent = get_reports_sorted(limit=15)

    max_org = max((c for _, c in top_orgs), default=1)
    max_action = max((c for _, c in top_actions), default=1)

    org_bars = "".join(
        f'<div class="bar-row"><span class="bar-label">{org}</span>'
        f'<div class="bar-track"><div class="bar" style="width:{int(count / max_org * 100)}%"></div></div>'
        f'<span class="bar-count">{count}</span></div>'
        for org, count in top_orgs
    ) or '<div class="empty">No data yet</div>'

    action_bars = "".join(
        f'<div class="bar-row"><span class="bar-label">{action}</span>'
        f'<div class="bar-track"><div class="bar alt" style="width:{int(count / max_action * 100)}%"></div></div>'
        f'<span class="bar-count">{count}</span></div>'
        for action, count in top_actions
    ) or '<div class="empty">No data yet</div>'

    recent_rows = "".join(
        f'<tr><td class="dim">#{rid}</td><td>{r.get("org", "")}</td><td>{r.get("action", "")}</td>'
        f'<td>{r.get("location", "")}</td>'
        f'<td>{"<span class=\"badge ok\">✓ Verified</span>" if r.get("verified") else "<span class=\"badge\">Pending</span>"}</td>'
        f'<td class="dim">{str(r.get("timestamp", ""))[:16]}</td></tr>'
        for rid, r in recent
    ) or '<tr><td colspan="6" class="empty">No reports yet</td></tr>'

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LabBot Intel Dashboard</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0e14; color: #e8e8e8; min-height: 100vh; padding: 2rem; }}
    .wrap {{ max-width: 1100px; margin: auto; }}
    header {{ display: flex; align-items: center; gap: 1rem; margin-bottom: 2rem; }}
    .logo {{ width: 44px; height: 44px; border-radius: 12px; background: linear-gradient(135deg, #ff4d4d, #ff8a3d); display: flex; align-items: center; justify-content: center; font-size: 1.4rem; box-shadow: 0 4px 20px rgba(255, 77, 77, 0.35); }}
    h1 {{ font-size: 1.5rem; font-weight: 700; letter-spacing: 0.5px; }}
    h1 span {{ color: #ff4d4d; }}
    .updated {{ margin-left: auto; color: #666; font-size: 0.8rem; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
    .card {{ background: #151922; border: 1px solid #232836; border-radius: 14px; padding: 1.1rem 1.2rem; transition: transform 0.15s; }}
    .card:hover {{ transform: translateY(-2px); }}
    .card .num {{ font-size: 2rem; font-weight: 800; }}
    .card .label {{ color: #8a8f9a; font-size: 0.8rem; margin-top: 0.2rem; }}
    .num.red {{ color: #ff4d4d; }} .num.green {{ color: #3ddc84; }} .num.blue {{ color: #4da3ff; }} .num.amber {{ color: #ffb84d; }}
    .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem; }}
    .panel {{ background: #151922; border: 1px solid #232836; border-radius: 14px; padding: 1.2rem; }}
    .panel h2 {{ font-size: 0.95rem; color: #c8c8d0; margin-bottom: 1rem; letter-spacing: 0.3px; }}
    .bar-row {{ display: flex; align-items: center; gap: 0.7rem; margin-bottom: 0.55rem; }}
    .bar-label {{ width: 110px; font-size: 0.85rem; color: #cfd2da; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .bar-track {{ flex: 1; background: #1e232e; border-radius: 6px; height: 10px; overflow: hidden; }}
    .bar {{ height: 100%; background: linear-gradient(90deg, #ff4d4d, #ff8a3d); border-radius: 6px; min-width: 2px; }}
    .bar.alt {{ background: linear-gradient(90deg, #4da3ff, #3ddc84); }}
    .bar-count {{ width: 30px; text-align: right; font-size: 0.85rem; color: #8a8f9a; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 0.65rem 0.8rem; text-align: left; font-size: 0.88rem; border-bottom: 1px solid #1e232e; }}
    th {{ color: #ff4d4d; font-weight: 600; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.4px; }}
    tr:hover td {{ background: #191e28; }}
    .dim {{ color: #6a6f7a; }}
    .badge {{ display: inline-block; padding: 0.15rem 0.55rem; border-radius: 999px; font-size: 0.72rem; font-weight: 600; background: #2a2e38; color: #9aa0ae; }}
    .badge.ok {{ background: rgba(61, 220, 132, 0.15); color: #3ddc84; }}
    .empty {{ color: #5a5f6a; text-align: center; padding: 1.5rem; }}
    .footer {{ margin-top: 2rem; color: #4a4f5a; font-size: 0.78rem; text-align: center; }}
    @media (max-width: 800px) {{ .grid2 {{ grid-template-columns: 1fr; }} body {{ padding: 1rem; }} .bar-label {{ width: 80px; }} }}
</style>
</head>
<body>
<div class="wrap">
    <header>
        <div class="logo">🛰️</div>
        <h1>LabBot <span>Intel</span></h1>
        <div class="updated">Updated {now}</div>
    </header>

    <div class="cards">
        <div class="card"><div class="num red">{total}</div><div class="label">Total Reports</div></div>
        <div class="card"><div class="num green">{verified}</div><div class="label">Verified</div></div>
        <div class="card"><div class="num blue">{geotagged}</div><div class="label">Geotagged</div></div>
        <div class="card"><div class="num amber">{len(org_counts)}</div><div class="label">Organizations</div></div>
    </div>

    <div class="grid2">
        <div class="panel">
            <h2>Activity by Organization</h2>
            {org_bars}
        </div>
        <div class="panel">
            <h2>Top Actions</h2>
            {action_bars}
        </div>
    </div>

    <div class="panel">
        <h2>Recent Reports</h2>
        <table>
            <thead><tr><th>ID</th><th>Org</th><th>Action</th><th>Location</th><th>Status</th><th>Timestamp</th></tr></thead>
            <tbody>{recent_rows}</tbody>
        </table>
    </div>

    <div class="footer">LabBot Intel — Firebase Realtime Database · auto-refreshes every 60s</div>
</div>
<script>setTimeout(() => location.reload(), 60000);</script>
</body>
</html>"""

async def handle_dashboard(request):
    try:
        return web.Response(text=render_dashboard(), content_type="text/html")
    except Exception as e:
        body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>LabBot Intel</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0b0e14;color:#e8e8e8;padding:2rem;max-width:640px;margin:auto}}
h1{{color:#ff4d4d}} code{{background:#1a1d24;padding:2px 6px;border-radius:4px;color:#ffb84d}}
</style></head>
<body>
<h1>🛰️ Dashboard unavailable</h1>
<p>Firebase isn't configured on this server yet.</p>
<p>Set the <code>FIREBASE_SERVICE_ACCOUNT</code> env var on Render (the full service-account JSON from Firebase Console → Project settings → Service accounts), then redeploy.</p>
<pre>{e}</pre>
</body></html>"""
        return web.Response(text=body, content_type="text/html", status=503)

async def start_keepalive_server():
    app = web.Application()
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/dashboard", handle_dashboard)
    port = int(os.environ.get("PORT", 8000))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"Web server (dashboard + keep-alive) listening on port {port}")

# --- Runtime entry point ---

async def main():
    # Priority: BOT_API -> DISCORD_BOT_TOKEN -> Colab secret
    token = os.environ.get("BOT_API") or os.environ.get("DISCORD_BOT_TOKEN")

    if not token and HAS_COLAB:
        try:
            from google.colab import userdata
            token = userdata.get("DISCORD_BOT_TOKEN")
        except Exception:
            pass

    if token:
        print("Starting bot instance...")
        # Start the web server (dashboard + keep-alive) but never let a problem here kill the bot
        try:
            await start_keepalive_server()
        except NameError:
            print("⚠️ start_keepalive_server not found — continuing without web server.")
        except Exception as e:
            print(f"⚠️ Web server failed to start: {e} — continuing without it.")
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
