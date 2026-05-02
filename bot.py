import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# =========================
# CONFIG
# =========================

token = os.getenv("DISCORD_TOKEN")
api_url = "https://api.geode-sdk.org/v1/mods/{}"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("geode")

version_re = re.compile(r"v?(\d+(?:\.\d+)+)", re.IGNORECASE)

# =========================
# MODS (ONLY 4)
# =========================

@dataclass(frozen=True)
class Mod:
    id: str
    name: str
    emoji: str


MODS = (
    Mod("axiom.echochoke", "EchoChoke", "🟣"),
    Mod("axiom.echoclip", "EchoClip", "🔴"),
    Mod("axiom.voicecontrol", "Voice Control", "🔵"),
    Mod("axiom.cube-abuse", "Cube Abuse", "🟡"),
)

# =========================
# HELPERS
# =========================

def now():
    return datetime.now(timezone.utc).isoformat()


def strip(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")


def get_text(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def unwrap(data: Any) -> dict:
    if isinstance(data, dict) and isinstance(data.get("payload"), dict):
        return data["payload"]
    return data if isinstance(data, dict) else {}


# =========================
# PENDING DETECTION (FIXED)
# =========================

def is_pending(d: dict) -> bool:
    if not isinstance(d, dict):
        return False

    for k in ("pending", "isPending", "is_pending"):
        if isinstance(d.get(k), bool):
            return d[k]

    status = get_text(d, ("status",))
    if status and "pending" in status.lower():
        return True

    text = get_text(d, ("description", "changelog", "notes"))
    if text and "pending" in text.lower():
        return True

    return False


# =========================
# VERSION EXTRACTION (ROBUST)
# =========================

def find_version(d: dict) -> Optional[str]:
    if not isinstance(d, dict):
        return None

    for k in ("version", "latestVersion", "currentVersion"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # nested search
    for k, v in d.items():
        if isinstance(v, dict):
            res = find_version(v)
            if res:
                return res

    text = get_text(d, ("changelog", "description"))
    if text:
        m = version_re.search(text)
        if m:
            return m.group(1)

    return None


# =========================
# BOT
# =========================

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.session: Optional[aiohttp.ClientSession] = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.tree.sync()
        log.info("slash commands synced")

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    # =========================
    # LIVE FETCH (ALWAYS FRESH)
    # =========================

    async def fetch_mod(self, mod: Mod):
        try:
            async with self.session.get(api_url.format(mod.id)) as r:
                data = unwrap(await r.json(content_type=None))
                version = find_version(data)
                pending = is_pending(data)

                released = bool(version) and not pending

                return {
                    "mod": mod,
                    "version": version or "unknown",
                    "pending": pending,
                    "released": released,
                    "raw": data,
                }
        except Exception as e:
            return {
                "mod": mod,
                "version": "error",
                "pending": False,
                "released": False,
                "raw": {"error": str(e)},
            }

    async def fetch_all(self):
        return await asyncio.gather(*(self.fetch_mod(m) for m in MODS))

    # =========================
    # EMBED
    # =========================

    def build_embed(self, results):
        e = discord.Embed(
            title="geode version checker",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        lines = []

        for r in results:
            m = r["mod"]

            if r["pending"]:
                status = "⏳ pending"
            elif r["version"] in ("unknown", "error") or not r["version"]:
                status = "❓ unknown"
            else:
                status = "✅ released"

            lines.append(
                f"{m.emoji} **{m.name}** — `{r['version']}` • {status}"
            )

        e.description = "\n".join(lines)
        e.set_footer(text="live api data (no cache)")
        return e


bot = Bot()

# =========================
# SLASH COMMANDS (FIXED)
# =========================

@bot.tree.command(name="checkforupdates", description="live geode mod status")
async def checkforupdates(interaction: discord.Interaction):
    await interaction.response.defer()

    data = await bot.fetch_all()
    await interaction.followup.send(embed=bot.build_embed(data))


@bot.tree.command(name="debugmods", description="raw api output (live)")
async def debugmods(interaction: discord.Interaction):
    await interaction.response.defer()

    data = await bot.fetch_all()

    out = ""
    for r in data:
        out += f"\n\n=== {r['mod'].name} ===\n{r['raw']}"

    await interaction.followup.send(out[:1900])


@bot.tree.command(name="debugstate", description="not used anymore (live only)")
async def debugstate(interaction: discord.Interaction):
    await interaction.response.defer()
    await interaction.followup.send("state disabled — everything is live now")


# =========================
# RUN
# =========================

def main():
    if not token:
        raise RuntimeError("DISCORD_TOKEN missing")
    bot.run(token)


if __name__ == "__main__":
    main()
