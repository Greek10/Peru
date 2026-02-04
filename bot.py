import os
import asyncio
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands

import aiohttp
from aiohttp import web

# ================= CONFIG =================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "0"))
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))
POLL_SECONDS = 30

# ================= BOT SETUP =================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")

# ================= OWNER CHECK =================
def is_owner(user_id: int) -> bool:
    return BOT_OWNER_ID != 0 and user_id == BOT_OWNER_ID

async def deny_if_not_owner(interaction: discord.Interaction) -> bool:
    if is_owner(interaction.user.id):
        return False
    await interaction.response.send_message(
        "❌ Owner-only command.",
        ephemeral=True
    )
    return True

# ================= CHANNEL HELPER =================
async def get_text_channel(channel_id: int) -> discord.TextChannel:
    if not channel_id:
        raise RuntimeError("SOURCE_CHANNEL_ID not set.")
    ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if not isinstance(ch, discord.TextChannel):
        raise RuntimeError("Source channel is not a text channel.")
    return ch

# ================= IMAGE FINDER =================
async def find_latest_image(channel: discord.TextChannel):
    async for msg in channel.history(limit=30, oldest_first=False):
        edited = msg.edited_at.timestamp() if msg.edited_at else 0

        for a in msg.attachments:
            if (a.content_type or "").startswith("image/"):
                sig = f"{msg.id}:{edited}:{a.url}"
                return sig, a.url

        for e in msg.embeds:
            if e.image and e.image.url:
                sig = f"{msg.id}:{edited}:{e.image.url}"
                return sig, e.image.url

    return None, None

# ================= /ARCHIEVED =================
_active_archives: dict[tuple[int, int], asyncio.Task] = {}

@bot.tree.command(name="archieved", description="(Owner) Live image archiver")
@app_commands.describe(
    mode="start or stop",
    output_channel="Where to post images (defaults to current channel)"
)
async def archieved(
    interaction: discord.Interaction,
    mode: str,
    output_channel: discord.TextChannel | None = None
):
    if await deny_if_not_owner(interaction):
        return

    mode = mode.lower().strip()
    if mode not in ("start", "stop"):
        await interaction.response.send_message(
            "Mode must be `start` or `stop`.",
            ephemeral=True
        )
        return

    key = (interaction.guild_id or 0, interaction.user.id)

    # ---------- STOP ----------
    if mode == "stop":
        task = _active_archives.pop(key, None)
        if task and not task.done():
            task.cancel()
            await interaction.response.send_message("🛑 /archieved stopped.", ephemeral=True)
        else:
            await interaction.response.send_message("No active /archieved running.", ephemeral=True)
        return

    # ---------- START ----------
    try:
        source_channel = await get_text_channel(SOURCE_CHANNEL_ID)
    except Exception as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    out_ch = output_channel or interaction.channel
    if not isinstance(out_ch, discord.TextChannel):
        await interaction.response.send_message("Output must be a text channel.", ephemeral=True)
        return

    old = _active_archives.pop(key, None)
    if old and not old.done():
        old.cancel()

    await interaction.response.send_message(
        f"✅ /archieved started\n"
        f"• Source: {source_channel.mention}\n"
        f"• Output: {out_ch.mention}\n"
        f"• Poll: {POLL_SECONDS}s\n"
        f"Stop with `/archieved stop`",
        ephemeral=True
    )

    async def runner():
        last_sig = None  # persists across loop iterations

        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    sig, url = await find_latest_image(source_channel)

                    # Nothing found
                    if not sig or not url:
                        await asyncio.sleep(POLL_SECONDS)
                        continue

                    # Image unchanged → do nothing
                    if sig == last_sig:
                        await asyncio.sleep(POLL_SECONDS)
                        continue

                    # Image changed → download & send
                    async with session.get(url) as r:
                        r.raise_for_status()
                        img = await r.read()

                    await out_ch.send(
                        content="🖼️ **Archived image (updated)**",
                        file=discord.File(BytesIO(img), filename="archived.png")
                    )

                    # Update signature ONLY after successful send
                    last_sig = sig

                except asyncio.CancelledError:
                    break

                except Exception as e:
                    try:
                        await out_ch.send(
                            f"⚠️ /archieved error: `{type(e).__name__}: {e}`"
                        )
                    except Exception:
                        pass

                await asyncio.sleep(POLL_SECONDS)

    task = asyncio.create_task(runner())
    _active_archives[key] = task

# ================= KEEP-ALIVE WEB SERVER =================
async def handle(request):
    return web.Response(text="OK")

async def start_web():
    app = web.Application()
    app.router.add_get("/", handle)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ================= MAIN =================
async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set.")
    await start_web()
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())