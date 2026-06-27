import discord
from discord.ext import commands
import aiohttp
import urllib.parse
import json
import os

# Load .env file automatically if python-dotenv is installed.
# Install with: pip install python-dotenv
# Then create a .env file containing:  DISCORD_TOKEN=your_token_here
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed; fall back to system environment variables

# Hard-stop if token is missing — catches misconfigured environments early.
if not os.getenv("DISCORD_TOKEN"):
    raise RuntimeError(
        "[STARTUP ERROR] DISCORD_TOKEN is not set.\n"
        "Set it as a system environment variable or add it to a .env file."
    )

# ─────────────────────────────────────────────
#  GATEWAY INTENTS
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True   # Required to read prefix commands
intents.guilds = True            # Required to resolve channel objects

bot = commands.Bot(command_prefix="!", intents=intents, case_insensitive=True)

# ─────────────────────────────────────────────
#  IN-MEMORY CACHE  +  JSON PERSISTENCE LAYER
# ─────────────────────────────────────────────
JSON_FILE = "channels.json"
_channel_cache: dict = {}   # Live in-memory copy; disk is only touched on writes


def load_channels() -> dict:
    """
    Reads channels.json from disk into the in-memory cache.
    Called once at startup; afterwards the cache is the source of truth.
    """
    global _channel_cache
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, "r") as f:
                _channel_cache = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[DATABASE ERROR] Failed to parse {JSON_FILE}: {e}")
            _channel_cache = {}
    else:
        _channel_cache = {}
    return _channel_cache


def save_channels(data: dict) -> bool:
    """
    Writes the current channel map to disk atomically using a temp file
    so a crash mid-write never corrupts channels.json.
    Returns True on success, False on failure.
    """
    tmp = JSON_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=4)
        os.replace(tmp, JSON_FILE)   # Atomic on all major OSes
        return True
    except OSError as e:
        print(f"[DATABASE ERROR] Failed to write {JSON_FILE}: {e}")
        return False


def get_channels() -> dict:
    """Returns the live in-memory cache (no disk I/O)."""
    return _channel_cache


# ─────────────────────────────────────────────
#  PERSISTENT BUTTON VIEW
# ─────────────────────────────────────────────
class LecturePortalView(discord.ui.View):
    """
    A permanent link-button wrapper.
    timeout=None means the button never expires in memory.
    Discord link-buttons open the URL client-side, so they survive bot restarts.
    """
    def __init__(self, target_url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="▶️  Watch Lecture Video",
            style=discord.ButtonStyle.link,
            url=target_url
        ))


# ─────────────────────────────────────────────
#  STARTUP
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    load_channels()   # Populate in-memory cache once at startup
    print("=========================================")
    print(f"✅  DYNAMIC GATEWAY ONLINE")
    print(f"🤖  Active Identity : {bot.user}")
    print(f"📦  Channels Loaded : {sum(len(v) for v in _channel_cache.values())} mappings")
    print("=========================================")


# ─────────────────────────────────────────────
#  COMMAND 1 — REGISTER A CHANNEL MAPPING
# ─────────────────────────────────────────────
@bot.command()
@commands.has_permissions(administrator=True)
async def addchannel(ctx, batch: str, subject: str, channel_id: int):
    """
    Registers or updates a batch → subject → channel_id mapping.
    Usage : !addchannel neet physics 111222333444555666

    Validates that the channel actually exists before saving.
    """
    batch   = batch.lower().strip()
    subject = subject.lower().strip()

    # ── Validate the channel ID before saving ───────────────────────────────
    target_channel = bot.get_channel(channel_id)
    if target_channel is None:
        await ctx.send(
            f"❌ **Error:** Channel ID `{channel_id}` was not found.\n"
            f"Make sure the bot has access to that channel and the ID is correct."
        )
        return

    # ── Update in-memory cache ───────────────────────────────────────────────
    if batch not in _channel_cache:
        _channel_cache[batch] = {}
    _channel_cache[batch][subject] = channel_id

    # ── Persist to disk ──────────────────────────────────────────────────────
    if not save_channels(_channel_cache):
        await ctx.send(
            "⚠️ **Warning:** Mapping registered in memory but failed to save to disk. "
            "The entry will be lost if the bot restarts. Please check server storage."
        )
        return

    await ctx.send(
        f"✅ **Registered:** `{batch.upper()} → {subject.capitalize()}` "
        f"mapped to **#{target_channel.name}** (`{channel_id}`)."
    )


# ─────────────────────────────────────────────
#  COMMAND 2 — POST A LECTURE VIDEO
# ─────────────────────────────────────────────
@bot.command()
@commands.has_permissions(administrator=True)
async def postvideo(ctx, batch: str, subject: str, raw_link: str, *, video_title: str):
    """
    Shortens the raw URL via TinyURL and posts a professional embed with
    a persistent Watch button to the correct batch/subject channel.
    Usage : !postvideo neet biology https://studypanda.in/... Cell Division - Chapter 3
    """
    batch   = batch.lower().strip()
    subject = subject.lower().strip()

    # ── Lookup from in-memory cache (no disk I/O) ───────────────────────────
    channels = get_channels()

    if batch not in channels:
        await ctx.send(
            f"❌ **Error:** Batch `{batch}` does not exist. "
            f"Run `!addchannel` to register it first."
        )
        return

    if subject not in channels[batch]:
        await ctx.send(
            f"❌ **Error:** Subject `{subject}` is not mapped under `{batch}`. "
            f"Run `!addchannel {batch} {subject} <channel_id>` to add it."
        )
        return

    processing_msg = await ctx.send("⏳ Shortening link and preparing lecture card…")

    try:
        # ── Step 1: Encode URL correctly ─────────────────────────────────────
        # safe= preserves the characters that are legally part of a URL structure
        # so that https:// and query strings (?key=value&...) are not mangled.
        safe_url = urllib.parse.quote(raw_link, safe=':/?=&#@%+')
        api_url  = f"https://tinyurl.com/api-create.php?url={safe_url}"

        # ── Step 2: Async HTTP — never blocks the bot event loop ─────────────
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(api_url) as response:
                short_link = (await response.text()).strip()

        # ── Step 3: Validate TinyURL response ────────────────────────────────
        if not short_link.startswith("http"):
            raise ValueError(f"TinyURL returned an unexpected response: {short_link!r}")

        # ── Step 4: Resolve target channel from cache ─────────────────────────
        channel_id     = channels[batch][subject]
        target_channel = bot.get_channel(channel_id)

        if target_channel is None:
            await processing_msg.edit(
                content=(
                    f"❌ **Error:** Channel ID `{channel_id}` for "
                    f"`{batch} → {subject}` could not be resolved.\n"
                    f"The bot may have lost access. Re-run `!addchannel` with a valid ID."
                )
            )
            return

        # ── Step 5: Verify bot has Send Messages + Embed Links permissions ────
        bot_member = target_channel.guild.me
        perms = target_channel.permissions_for(bot_member)
        if not perms.send_messages or not perms.embed_links:
            missing = []
            if not perms.send_messages: missing.append("Send Messages")
            if not perms.embed_links:   missing.append("Embed Links")
            await processing_msg.edit(
                content=(
                    f"❌ **Permission Error:** Bot is missing the following permissions "
                    f"in **#{target_channel.name}**: `{'`, `'.join(missing)}`.\n"
                    f"Fix this in **Server Settings → Roles** or the channel's permission overrides."
                )
            )
            return

        # ── Step 6: Build the announcement embed ─────────────────────────────
        embed = discord.Embed(
            title=f"📚  {batch.upper()} — New Lecture Available",
            description=f"A new video has been added to **{subject.capitalize()}**.",
            color=discord.Color.brand_green()
        )
        embed.add_field(name="📌  Topic", value=video_title, inline=False)
        embed.set_footer(text="Click the button below to start watching.")

        # ── Step 7: Attach the permanent button and dispatch ─────────────────
        view = LecturePortalView(short_link)
        await target_channel.send(embed=embed, view=view)

        # ── Step 8: Confirm to admin ──────────────────────────────────────────
        await processing_msg.edit(
            content=(
                f"✅ **Done!** Lecture posted to "
                f"**#{target_channel.name}** (`{batch.upper()} → {subject.capitalize()}`)."
            )
        )

    except aiohttp.ClientError as e:
        await processing_msg.edit(
            content=f"❌ **Network Error:** Could not reach TinyURL. Details: `{e}`"
        )
    except ValueError as e:
        await processing_msg.edit(
            content=f"❌ **Link Error:** {e}"
        )
    except discord.Forbidden:
        await processing_msg.edit(
            content=(
                f"❌ **Permission Error:** The bot does not have permission to send "
                f"messages in **#{target_channel.name}**. Check the channel role settings."
            )
        )
    except Exception as e:
        await processing_msg.edit(
            content=f"❌ **Unexpected Error:** Operation aborted safely. Details: `{e}`"
        )


# ─────────────────────────────────────────────
#  SHARED PERMISSION-ERROR HANDLER
# ─────────────────────────────────────────────
@addchannel.error
@postvideo.error
async def permission_error_handler(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ **Access Denied:** Administrator privileges are required to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            f"❌ **Missing Argument:** `{error.param.name}` is required.\n"
            f"Run `!help {ctx.invoked_with}` to see correct usage."
        )
    elif isinstance(error, commands.BadArgument):
        await ctx.send(
            "❌ **Invalid Argument:** Make sure `channel_id` is a plain integer (no `<#>` brackets)."
        )


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────
bot.run(os.getenv("DISCORD_TOKEN"))
