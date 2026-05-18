import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
import discord
from discord.ext import commands

token = os.getenv("DISCORD_TOKEN")
api_url = "https://api.geode-sdk.org/v1/mods/{}"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("geode")


@dataclass(frozen=True)
class Mod:
    id: str
    name: str
    emoji: str


MODS = [
    Mod("axiom.echochoke", "EchoChoke", "🟣"),
    Mod("axiom.echoclip", "EchoClip", "🔵"),
    Mod("axiom.voicecontrol", "Voice Control", "⚫"),
    Mod("axiom.cube-abuse", "Cube Abuse", "🟡"),
    Mod("axiom.100-days", "100 Days", "🔴"),
]

ALLOWED_USER_IDS = {
    1470498076325314571,
    1411637140101660692,
}


def unwrap(data: Any) -> dict:
    if isinstance(data, dict) and isinstance(data.get("payload"), dict):
        return data["payload"]
    return data if isinstance(data, dict) else {}


def is_pending(d: dict) -> bool:
    if not isinstance(d, dict):
        return False

    versions = d.get("versions")
    if not isinstance(versions, list) or not versions:
        return False

    first_version = versions[0]
    if not isinstance(first_version, dict):
        return False

    status = first_version.get("status")
    if status == "accepted":
        return False
    else:
        return True


# find latest ver
def find_version(d: dict) -> Optional[str]:
    if not isinstance(d, dict):
        return None

    versions = d.get("versions")
    if not isinstance(versions, list) or not versions:
        return None

    first_version = versions[0]
    if not isinstance(first_version, dict):
        return None

    version = first_version.get("version")
    return version if isinstance(version, str) else None


def find_downloads(d: dict) -> Optional[int]:
    if not isinstance(d, dict):
        return None

    candidates = [
        d.get("downloads"),
        d.get("download_count"),
        d.get("downloads_total"),
    ]

    stats = d.get("stats")
    if isinstance(stats, dict):
        candidates.extend([
            stats.get("downloads"),
            stats.get("download_count"),
            stats.get("downloads_total"),
        ])

    for value in candidates:
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)

    return None


def find_mod_url(d: dict, mod: Mod) -> str:
    if isinstance(d, dict):
        for key in ("url", "page", "website", "mod_url"):
            value = d.get(key)
            if isinstance(value, str) and value:
                return value

        links = d.get("links")
        if isinstance(links, dict):
            for key in ("website", "page", "url"):
                value = links.get(key)
                if isinstance(value, str) and value:
                    return value

    return f"https://geode-sdk.org/mods/{mod.id}"


def format_error_reason(error: Any) -> str:
    text = str(error).strip() if error is not None else "unknown error"
    text = " ".join(text.split())
    if not text:
        text = "unknown error"
    return text[:180]


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

    # api call
    async def fetch_mod(self, mod: Mod):
        try:
            async with self.session.get(api_url.format(mod.id)) as r:
                raw_text = await r.text()

                if r.status == 404:
                    return {
                        "mod": mod,
                        "version": "not_found",
                        "pending": False,
                        "downloads": None,
                        "url": f"https://geode-sdk.org/mods/{mod.id}",
                        "error": "❌Not on the index",
                    }

                if r.status != 200:
                    return {
                        "mod": mod,
                        "version": "error",
                        "pending": False,
                        "downloads": None,
                        "url": f"https://geode-sdk.org/mods/{mod.id}",
                        "error": f"HTTP {r.status} {r.reason}" + (
                            f" — {raw_text.strip()[:120]}" if raw_text.strip() else ""
                        ),
                    }

                try:
                    raw_data = json.loads(raw_text)
                except json.JSONDecodeError as e:
                    return {
                        "mod": mod,
                        "version": "error",
                        "pending": False,
                        "downloads": None,
                        "url": f"https://geode-sdk.org/mods/{mod.id}",
                        "error": f"invalid json response: {format_error_reason(e)}",
                    }

                data = unwrap(raw_data)

                version = find_version(data)
                pending = is_pending(data)
                downloads = find_downloads(data)
                url = find_mod_url(data, mod)

                return {
                    "mod": mod,
                    "version": version or "unknown",
                    "pending": pending,
                    "downloads": downloads,
                    "url": url,
                }

        except asyncio.TimeoutError as e:
            return {
                "mod": mod,
                "version": "error",
                "pending": False,
                "downloads": None,
                "url": f"https://geode-sdk.org/mods/{mod.id}",
                "error": f"timeout: {format_error_reason(e)}",
            }
        except aiohttp.ClientError as e:
            return {
                "mod": mod,
                "version": "error",
                "pending": False,
                "downloads": None,
                "url": f"https://geode-sdk.org/mods/{mod.id}",
                "error": f"network error: {format_error_reason(e)}",
            }
        except Exception as e:
            return {
                "mod": mod,
                "version": "error",
                "pending": False,
                "downloads": None,
                "url": f"https://geode-sdk.org/mods/{mod.id}",
                "error": format_error_reason(e),
            }

    async def fetch_all(self):
        return await asyncio.gather(*(self.fetch_mod(m) for m in MODS))

    # embeds
    def build_embed(self, results):
        e = discord.Embed(
            title="geode version checker",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        lines = []

        for r in results:
            m = r["mod"]

            if r["version"] == "error":
                error_reason = r.get("error", "unknown error")
                status = f"❌ error: {error_reason}"
            elif r["version"] == "not_found":
                status = "❌Not on the index"
            elif r["pending"]:
                status = "⏳ Pending"
            else:
                status = "✅ On the index"

            downloads = (
                f"{r['downloads']:,}"
                if isinstance(r.get("downloads"), int)
                else "unknown"
            )

            lines.append(
                f"{m.emoji} [{m.name}]({r['url']}) — `{r['version']}` • "
                f"{downloads} downloads • {status}"
            )

        e.description = "\n".join(lines)
        return e


bot = Bot()


# cmds

@bot.tree.command(name="checkforupdates", description="live geode mod status")
@discord.app_commands.describe(mod="choose one mod to check")
async def checkforupdates(interaction: discord.Interaction, mod: Optional[str] = None):
    if mod is not None:
        selected_mod = next((m for m in MODS if m.id == mod), None)
        if selected_mod is None:
            await interaction.response.send_message("❌Not on the index")
            return

    await interaction.response.defer()

    if mod is None:
        data = await bot.fetch_all()
    else:
        selected_mod = next((m for m in MODS if m.id == mod), None)
        data = [await bot.fetch_mod(selected_mod)]

        if data[0].get("version") == "not_found":
            await interaction.followup.send("❌Not on the index")
            return

    await interaction.followup.send(embed=bot.build_embed(data))


@checkforupdates.autocomplete("mod")
async def checkforupdates_mod_autocomplete(interaction: discord.Interaction, current: str):
    current = current.lower()
    return [
        discord.app_commands.Choice(name=f"{m.emoji} {m.name}", value=m.id)
        for m in MODS
        if current in m.id.lower() or current in m.name.lower()
    ][:25]


@bot.tree.command(name="addnewmodid", description="add a new mod id to the index")
@discord.app_commands.describe(mod_id="new mod id", name="display name", emoji="emoji")
async def addnewmodid(
    interaction: discord.Interaction,
    mod_id: str,
    name: Optional[str] = None,
    emoji: str = "⚪",
):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("❌ Access Denied")
        return

    clean_mod_id = mod_id.strip()
    clean_name = name.strip() if isinstance(name, str) and name.strip() else clean_mod_id
    clean_emoji = emoji.strip() if isinstance(emoji, str) and emoji.strip() else "⚪"

    existing = next((m for m in MODS if m.id == clean_mod_id), None)
    if existing is None:
        MODS.append(Mod(clean_mod_id, clean_name, clean_emoji))
    else:
        index = MODS.index(existing)
        MODS[index] = Mod(clean_mod_id, clean_name, clean_emoji)

    await interaction.response.send_message(f"added {clean_mod_id}")


@bot.tree.command(name="editmodid", description="edit an existing mod id in the index")
@discord.app_commands.describe(old_mod_id="current mod id", new_mod_id="new mod id")
async def editmodid(
    interaction: discord.Interaction,
    old_mod_id: str,
    new_mod_id: str,
):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("❌ Access Denied")
        return

    clean_old_mod_id = old_mod_id.strip()
    clean_new_mod_id = new_mod_id.strip()

    target_index = next(
        (i for i, m in enumerate(MODS) if m.id == clean_old_mod_id),
        None,
    )

    if target_index is None:
        await interaction.response.send_message("❌Not on the index")
        return

    current_mod = MODS[target_index]
    MODS[target_index] = Mod(clean_new_mod_id, current_mod.name, current_mod.emoji)

    await interaction.response.send_message(
        f"edited {clean_old_mod_id} to {clean_new_mod_id}"
    )


def main():
    if not token:
        raise RuntimeError("DISCORD_TOKEN missing")
    bot.run(token)


if __name__ == "__main__":
    main()
