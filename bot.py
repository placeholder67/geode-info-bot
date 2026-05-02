import asyncio
import json
import logging
import os
import re
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("geode-version-checker")

version_re = re.compile(r"^\s*v?(\d+(?:\.\d+)+(?:[-+][\w.]+)?)\s*$", re.IGNORECASE)

BOT_INFO = (
    "tracking geode mods:\n"
    "- axiom.echochoke\n"
    "- axiom.echoclip\n"
    "- axiom.voice control\n"
    "- axiom.cube-abuse\n"
)


@dataclass(frozen=True)
class TrackedMod:
    id: str
    label: str
    emoji: str


tracked_mods: tuple[TrackedMod, ...] = (
    TrackedMod("axiom.echochoke", "echochoke", "🟣"),
    TrackedMod("axiom.echoclip", "echoclip", "🔴"),
    TrackedMod("axiom.voicecontrol", "voice control", "🔵"),
    TrackedMod("axiom.cube-abuse", "cube abuse", "🟡"),
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


def version_from_changelog(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    for line in strip_tags(text).splitlines():
        m = version_re.match(line.strip())
        if m:
            return m.group(1)
    return None


def first_text(data: Any, keys: tuple[str, ...]) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    for key in keys:
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float, bool)):
            return str(v)
    return None


def first_bool(data: Any, keys: tuple[str, ...]) -> Optional[bool]:
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key not in data:
            continue
        v = data.get(key)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            t = v.strip().lower()
            if t in {"true", "1", "yes"}:
                return True
            if t in {"false", "0", "no"}:
                return False
        if isinstance(v, (int, float)):
            return bool(v)
    return None


def unwrap_payload(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        payload = data.get("payload")
        if isinstance(payload, dict):
            return payload
        return data
    return {}


def compare_versions(saved: Optional[dict[str, Any]], current: dict[str, Any]) -> str:
    cur = current.get("display_version") or current.get("version") or "unknown"
    if not saved:
        return "new"
    old = saved.get("display_version") or saved.get("version") or "unknown"
    if old == cur:
        return "same"
    return f"{old} → {cur}"


def load_state() -> dict[str, Any]:
    if not state_file.exists():
        return {"mods": {}}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"mods": {}}
        data.setdefault("mods", {})
        return data
    except Exception:
        return {"mods": {}}


def save_state(state: dict[str, Any]) -> None:
    state_file.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def compact_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": snapshot.get("version"),
        "display_version": snapshot.get("display_version"),
        "saved_at": utc_now_iso(),
    }


def extract_snapshot(mod: TrackedMod, data: dict[str, Any]) -> dict[str, Any]:
    name = first_text(data, ("name", "title", "displayName", "display_name")) or mod.label
    author = first_text(data, ("author", "developer", "creator", "owner"))

    version = (
        first_text(data, ("version", "latestVersion", "latest_version", "currentVersion", "current_version"))
        or version_from_changelog(first_text(data, ("changelog",)))
    )

    pending = first_bool(data, ("pending", "isPending", "is_pending")) or False
    released = first_bool(data, ("released", "isReleased", "is_released"))
    if released is None:
        released = bool(version) and not pending

    return {
        "id": mod.id,
        "label": mod.label,
        "emoji": mod.emoji,
        "name": name,
        "author": author,
        "version": version,
        "display_version": f"{version} (pending)" if pending and version else (version or "unknown"),
        "pending": pending,
        "released": released,
        "raw": data,
        "parse_failed": not bool(version),
    }


class GeodeVersionBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.session: Optional[aiohttp.ClientSession] = None
        self.state = load_state()
        self.last_snapshot: dict[str, dict[str, Any]] = {}

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"User-Agent": "geode-version-checker/1.0"},
        )

        try:
            synced = await self.tree.sync()
            log.info("synced %d commands", len(synced))
        except Exception:
            log.exception("sync failed")

        log.info(BOT_INFO)
        self.poll_versions.start()

    async def close(self) -> None:
        if self.poll_versions.is_running():
            self.poll_versions.cancel()
        if self.session:
            await self.session.close()
        await super().close()

    async def fetch_one(self, mod: TrackedMod) -> tuple[str, dict[str, Any]]:
        try:
            async with self.session.get(api_url.format(mod.id)) as res:
                if res.status != 200:
                    return mod.id, {"parse_failed": True, "error": f"http {res.status}"}

                data = unwrap_payload(await res.json(content_type=None))
                snap = extract_snapshot(mod, data)
                snap["raw"] = data
                return mod.id, snap

        except Exception as e:
            return mod.id, {"parse_failed": True, "error": str(e)}

    async def fetch_snapshots(self) -> dict[str, dict[str, Any]]:
        pairs = await asyncio.gather(*(self.fetch_one(m) for m in tracked_mods))
        return dict(pairs)

    def apply_snapshot_to_state(self, snapshots: dict[str, dict[str, Any]]) -> list[str]:
        changed = []
        mods = self.state.setdefault("mods", {})

        for mod_id, snap in snapshots.items():
            if snap.get("parse_failed"):
                continue

            saved = mods.get(mod_id)
            if not saved or saved.get("version") != snap.get("version"):
                mods[mod_id] = compact_state(snap)
                changed.append(mod_id)

        if changed:
            self.state["last_updated"] = utc_now_iso()
            save_state(self.state)

        return changed

    async def build_report(self):
        snaps = await self.fetch_snapshots()
        return snaps, None

    @tasks.loop(minutes=check_interval_minutes)
    async def poll_versions(self):
        snaps = await self.fetch_snapshots()
        changed = self.apply_snapshot_to_state(snaps)
        if changed:
            log.info("updated: %s", ", ".join(changed))

    @poll_versions.before_loop
    async def before_poll_versions(self):
        await self.wait_until_ready()

    def make_check_embed(self, snapshots: dict[str, dict[str, Any]], error=None):
        embed = discord.Embed(
            title="geode version checker",
            description=BOT_INFO + "\n\nstatus:",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        saved = self.state.get("mods", {})
        lines = []

        for mod in tracked_mods:
            snap = snapshots.get(mod.id)
            if not snap:
                lines.append(f"{mod.emoji} {mod.label} — failed")
                continue

            if snap.get("parse_failed"):
                lines.append(f"{mod.emoji} {mod.label} — parse failed")
                continue

            current = snap.get("display_version") or "unknown"
            change = compare_versions(saved.get(mod.id) if isinstance(saved, dict) else None, snap)

            lines.append(f"{mod.emoji} {snap.get('name')} — `{current}` • {change}")

        embed.description = "\n".join(lines)
        return embed


bot = GeodeVersionBot()


async def safe_defer(interaction):
    if not interaction.response.is_done():
        await interaction.response.defer()


@bot.tree.command(name="checkforupdates")
async def checkforupdates(interaction):
    await safe_defer(interaction)
    snaps, _ = await bot.build_report()
    await interaction.followup.send(embed=bot.make_check_embed(snaps))


@bot.event
async def on_ready():
    log.info("logged in as %s", bot.user)


def main():
    if not token:
        raise RuntimeError("missing DISCORD_TOKEN")
    bot.run(token)


if __name__ == "__main__":
    main()
