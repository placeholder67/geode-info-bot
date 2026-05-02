import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
import discord
from discord.ext import commands, tasks

token = os.getenv("DISCORD_TOKEN")

api_url = "https://api.geode-sdk.org/v1/mods/{}"
state_file = Path("geode_version_state.json")
check_interval_minutes = 15

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("geode-checker")


# ───────────────────────────────
# tracked mods (edit here only)
# ───────────────────────────────
@dataclass(frozen=True)
class Mod:
    id: str
    name: str
    emoji: str


tracked_mods = (
    Mod("axiom.echochoke", "echochoke", "🟣"),
    Mod("axiom.echoclip", "echoclip", "🔴"),
    Mod("axiom.voicecontrol", "voice control", "🔵"),
    Mod("axiom.cube-abuse", "cube abuse", "🟡"),
)


# ───────────────────────────────
# helpers
# ───────────────────────────────
def now():
    return datetime.now(timezone.utc).isoformat()


def clean(s: str | None):
    return (s or "").strip() or None


def load_state():
    if not state_file.exists():
        return {"mods": {}}
    try:
        return json.loads(state_file.read_text())
    except:
        return {"mods": {}}


def save_state(state):
    state_file.write_text(json.dumps(state, indent=2))


def compare(saved, current):
    if not saved:
        return "new"
    if saved.get("version") == current.get("version"):
        return "same"
    return f"{saved.get('version')} → {current.get('version')}"


# ───────────────────────────────
# bot
# ───────────────────────────────
class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.session = None
        self.state = load_state()

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.poll.start()

    async def fetch_mod(self, mod: Mod):
        try:
            async with self.session.get(api_url.format(mod.id)) as r:
                if r.status != 200:
                    return mod.id, {"error": r.status}

                data = await r.json(content_type=None)

                payload = data.get("payload", data)

                version = clean(payload.get("version") or payload.get("latest_version"))
                pending = "pending" in str(payload).lower()

                return mod.id, {
                    "name": mod.name,
                    "version": version or "unknown",
                    "pending": pending,
                    "raw": payload,
                    "parse_failed": version is None,
                }

        except Exception as e:
            return mod.id, {"error": str(e), "parse_failed": True}

    async def fetch_all(self):
        results = await asyncio.gather(*(self.fetch_mod(m) for m in tracked_mods))
        return dict(results)

    def make_embed(self, snaps):
        saved = self.state.get("mods", {})

        embed = discord.Embed(
            title="geode version tracker",
            description=(
                "**tracked mods:** axiom suite\n"
                f"**mods:** {len(tracked_mods)}\n"
                f"**updated:** <t:{int(datetime.now().timestamp())}:R>\n"
                "\n━━━━━━━━━━━━━━"
            ),
            color=discord.Color.blurple(),
        )

        lines = []

        for m in tracked_mods:
            s = snaps.get(m.id, {})

            if s.get("error"):
                lines.append(f"{m.emoji} **{m.name}** — error")
                continue

            version = s.get("version", "unknown")
            change = compare(saved.get(m.id), s)

            status = "⏳" if s.get("pending") else "✅"

            lines.append(
                f"{m.emoji} **{m.name}** `{version}` {status}\n"
                f"└ `{change}`"
            )

        embed.description += "\n" + "\n\n".join(lines)

        embed.set_footer(text="pending mods are not saved to state")
        return embed

    @tasks.loop(minutes=check_interval_minutes)
    async def poll(self):
        snaps = await self.fetch_all()

        for k, v in snaps.items():
            if not v.get("pending") and not v.get("parse_failed"):
                self.state["mods"][k] = {
                    "version": v.get("version"),
                    "saved_at": now(),
                }

        save_state(self.state)


bot = Bot()


@bot.tree.command(name="check")
async def check(interaction: discord.Interaction):
    await interaction.response.defer()
    snaps = await bot.fetch_all()
    await interaction.followup.send(embed=bot.make_embed(snaps))


@bot.event
async def on_ready():
    print(f"logged in as {bot.user}")


def main():
    if not token:
        raise RuntimeError("missing token")
    bot.run(token)


if __name__ == "__main__":
    main()
