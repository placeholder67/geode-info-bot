import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
import discord
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")

API_URL = "https://api.geode-sdk.org/v1/mods"
STATE_FILE = Path("geode_version_state.json")
CHECK_INTERVAL_MINUTES = 15
TRACKED_MOD_IDS = (
    "axiom.echochoke",
    "axiom.echoclip",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("geode-version-checker")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def first_text(data: Any, keys: tuple[str, ...]) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
        elif isinstance(value, (int, float, bool)):
            return str(value)
    return None


def first_bool(data: Any, keys: tuple[str, ...]) -> Optional[bool]:
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"true", "1", "yes", "pending"}:
                return text in {"true", "1", "yes"}
        if isinstance(value, (int, float)):
            return bool(value)
    return None


def normalize_status_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    cleaned = text.strip().lower()
    if "pending" in cleaned:
        return "pending"
    if "release" in cleaned:
        return "released"
    return cleaned


def deep_iter_nodes(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from deep_iter_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from deep_iter_nodes(item)


def looks_like_mod_node(node: dict[str, Any], target_id: str) -> bool:
    candidate_ids = (
        "id",
        "mod_id",
        "modId",
        "identifier",
        "slug",
    )
    for key in candidate_ids:
        value = node.get(key)
        if isinstance(value, str) and value.strip().lower() == target_id.lower():
            return True
    return False


def find_mod_node(payload: Any, target_id: str) -> Optional[dict[str, Any]]:
    best: Optional[dict[str, Any]] = None

    for node in deep_iter_nodes(payload):
        if not isinstance(node, dict):
            continue
        if looks_like_mod_node(node, target_id):
            best = node
            versionish = any(
                key in node
                for key in (
                    "versions",
                    "version",
                    "latestVersion",
                    "currentVersion",
                    "releaseStatus",
                    "status",
                )
            )
            if versionish:
                return node

    return best


def collect_version_candidates(mod: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    root_version_fields = (
        "version",
        "latestVersion",
        "latest_version",
        "currentVersion",
        "current_version",
    )
    root_candidate: dict[str, Any] = {}
    found_root_version = False
    for key in root_version_fields:
        if key in mod and mod.get(key) is not None:
            root_candidate[key] = mod.get(key)
            found_root_version = True

    if found_root_version:
        for key in ("status", "releaseStatus", "release_status", "pending", "released"):
            if key in mod:
                root_candidate[key] = mod.get(key)
        candidates.append(root_candidate)

    version_list_keys = (
        "versions",
        "versionHistory",
        "version_history",
        "releases",
        "releaseHistory",
        "release_history",
        "items",
    )
    for key in version_list_keys:
        value = mod.get(key)
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    candidates.append(entry)
                elif isinstance(entry, str):
                    candidates.append({"version": entry})

    for key in ("latest", "current", "versionInfo"):
        value = mod.get(key)
        if isinstance(value, dict):
            candidates.append(value)

    if not candidates and isinstance(mod.get("version"), str):
        candidates.append({"version": mod["version"]})

    return candidates


def normalize_candidate(
    candidate: dict[str, Any],
    fallback_order: int,
) -> dict[str, Any]:
    version = first_text(candidate, ("version", "number", "tag", "name", "value"))
    status = normalize_status_text(
        first_text(candidate, ("status", "releaseStatus", "release_status", "state"))
    )

    pending_flag = first_bool(candidate, ("pending", "isPending", "is_pending"))
    released_flag = first_bool(candidate, ("released", "isReleased", "is_released"))

    if pending_flag is None and status == "pending":
        pending_flag = True
    if released_flag is None and status == "released":
        released_flag = True

    if pending_flag is None and released_flag is None:
        pending_flag = False

    if status is None:
        for key in ("note", "label", "kind", "type"):
            text = first_text(candidate, (key,))
            if text and "pending" in text.lower():
                status = "pending"
                pending_flag = True
                released_flag = False
                break

    release_date = first_text(
        candidate,
        ("releasedAt", "released_at", "releaseDate", "release_date", "createdAt", "created_at"),
    )

    return {
        "version": version,
        "status": status,
        "pending": bool(pending_flag),
        "released": bool(released_flag if released_flag is not None else not pending_flag),
        "release_date": release_date,
        "fallback_order": fallback_order,
        "raw": candidate,
    }


def choose_current_candidate(candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not candidates:
        return None

    normalized = [normalize_candidate(c, i) for i, c in enumerate(candidates)]

    for cand in normalized:
        raw = cand["raw"]
        for key in ("latest", "isLatest", "is_latest", "current", "isCurrent", "is_current"):
            value = raw.get(key)
            if value is True or (isinstance(value, str) and value.lower() == "true"):
                return cand

    for cand in normalized:
        if cand["version"]:
            return cand

    return normalized[0]


def extract_mod_snapshot(mod_id: str, mod_node: dict[str, Any]) -> dict[str, Any]:
    candidates = collect_version_candidates(mod_node)
    chosen = choose_current_candidate(candidates)

    name = first_text(mod_node, ("name", "title", "displayName", "display_name")) or mod_id
    author = first_text(mod_node, ("author", "developer", "creator")) or first_text(
        mod_node, ("owner", "maintainer")
    )

    if chosen is None:
        current = {
            "version": None,
            "status": "unknown",
            "pending": False,
            "released": False,
            "release_date": None,
            "raw": {},
        }
    else:
        current = chosen

    version = current["version"]
    status = current["status"]
    pending = bool(current["pending"])
    released = bool(current["released"])

    display_version = version or "unknown"
    if pending and version:
        display_version = f"{version} (pending)"

    return {
        "id": mod_id,
        "name": name,
        "author": author,
        "version": version,
        "display_version": display_version,
        "pending": pending,
        "released": released,
        "status": status or ("pending" if pending else "released"),
        "release_date": current["release_date"],
        "raw": mod_node,
        "version_candidates": [normalize_candidate(c, i) for i, c in enumerate(candidates)],
    }


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"schema_version": 1, "mods": {}}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except json.JSONDecodeError:
        backup = STATE_FILE.with_suffix(f".corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        try:
            STATE_FILE.replace(backup)
            log.warning("state file was corrupt; moved to %s", backup)
        except Exception:
            log.exception("failed to move corrupt state file")
        return {"schema_version": 1, "mods": {}}
    except Exception:
        log.exception("failed to load state file")
        return {"schema_version": 1, "mods": {}}

    if not isinstance(data, dict):
        return {"schema_version": 1, "mods": {}}

    data.setdefault("schema_version", 1)
    data.setdefault("mods", {})
    if not isinstance(data["mods"], dict):
        data["mods"] = {}
    return data


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    payload = json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True)
    with tmp.open("w", encoding="utf-8") as fp:
        fp.write(payload)
    tmp.replace(STATE_FILE)


def compact_mod_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": snapshot["id"],
        "name": snapshot["name"],
        "version": snapshot["version"],
        "display_version": snapshot["display_version"],
        "pending": snapshot["pending"],
        "released": snapshot["released"],
        "status": snapshot["status"],
        "release_date": snapshot["release_date"],
        "saved_at": utc_now_iso(),
    }


def version_line(snapshot: Optional[dict[str, Any]]) -> str:
    if not snapshot:
        return "not saved"
    version = snapshot.get("version")
    display = snapshot.get("display_version") or version or "unknown"
    return str(display)


def compare_versions(saved: Optional[dict[str, Any]], current: dict[str, Any]) -> str:
    current_display = current["display_version"]
    if not saved:
        return f"new → {current_display}"
    saved_display = saved.get("display_version") or saved.get("version") or "unknown"
    if saved_display == current_display:
        return current_display
    return f"{saved_display} → {current_display}"


class GeodeVersionBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.session: Optional[aiohttp.ClientSession] = None
        self.state: dict[str, Any] = load_state()
        self.last_snapshot: dict[str, dict[str, Any]] = {}

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=20)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": "geode-version-checker/1.0"},
        )

        try:
            synced = await self.tree.sync()
            log.info("synced %d application commands", len(synced))
        except Exception:
            log.exception("failed to sync application commands")

        if not self.poll_versions.is_running():
            self.poll_versions.start()

    async def close(self) -> None:
        try:
            self.poll_versions.cancel()
        except Exception:
            pass
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

    async def fetch_api_payload(self) -> Any:
        if not self.session:
            raise RuntimeError("http session not ready")

        async with self.session.get(API_URL) as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    async def fetch_snapshots(self) -> dict[str, dict[str, Any]]:
        payload = await self.fetch_api_payload()
        snapshots: dict[str, dict[str, Any]] = {}

        for mod_id in TRACKED_MOD_IDS:
            mod_node = find_mod_node(payload, mod_id)
            if mod_node:
                snapshots[mod_id] = extract_mod_snapshot(mod_id, mod_node)

        return snapshots

    def apply_snapshot_to_state(self, snapshots: dict[str, dict[str, Any]]) -> list[str]:
        changed: list[str] = []
        mods = self.state.setdefault("mods", {})

        for mod_id, snapshot in snapshots.items():
            self.last_snapshot[mod_id] = snapshot

            if snapshot["pending"]:
                continue

            saved = mods.get(mod_id)
            saved_version = saved.get("version") if isinstance(saved, dict) else None
            if saved_version != snapshot["version"]:
                mods[mod_id] = compact_mod_state(snapshot)
                changed.append(mod_id)

        if changed:
            self.state["last_updated"] = utc_now_iso()
            save_state(self.state)

        return changed

    async def build_report(self) -> tuple[dict[str, dict[str, Any]], Optional[str]]:
        try:
            snapshots = await self.fetch_snapshots()
            return snapshots, None
        except Exception as exc:
            log.exception("failed to fetch geode snapshots")
            error = f"{type(exc).__name__}: {exc}"
            return self.last_snapshot.copy(), error

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def poll_versions(self) -> None:
        try:
            snapshots = await self.fetch_snapshots()
            changed = self.apply_snapshot_to_state(snapshots)
            if changed:
                log.info("updated saved state for: %s", ", ".join(changed))
            else:
                log.info("poll completed with no released changes")
        except Exception:
            log.exception("background poll failed")

    @poll_versions.before_loop
    async def before_poll_versions(self) -> None:
        await self.wait_until_ready()

    @poll_versions.error
    async def poll_versions_error(self, error: Exception) -> None:
        log.exception("poll loop error: %s", error)

    def make_check_embed(self, snapshots: dict[str, dict[str, Any]], error: Optional[str] = None) -> discord.Embed:
        embed = discord.Embed(
            title="geode version checker",
            description=f"live data from `{API_URL}`",
            colour=discord.Colour.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        if error:
            embed.add_field(name="warning", value=f"fetch error: `{error}`", inline=False)

        mods = self.state.get("mods", {})
        for mod_id in TRACKED_MOD_IDS:
            current = snapshots.get(mod_id)
            saved = mods.get(mod_id) if isinstance(mods, dict) else None

            if not current:
                embed.add_field(
                    name=mod_id,
                    value="could not parse this mod from the api response.",
                    inline=False,
                )
                continue

            change = compare_versions(saved if isinstance(saved, dict) else None, current)
            saved_line = version_line(saved if isinstance(saved, dict) else None)
            status_line = current["status"] or ("pending" if current["pending"] else "released")
            author = current.get("author") or "unknown"
            name = current.get("name") or mod_id

            field_value = (
                f"**name:** {name}\n"
                f"**id:** `{mod_id}`\n"
                f"**author:** {author}\n"
                f"**current:** {current['display_version']}\n"
                f"**saved:** {saved_line}\n"
                f"**status:** {status_line}\n"
                f"**change:** {change}"
            )
            embed.add_field(name=name, value=field_value, inline=False)

        embed.set_footer(text="pending versions are shown but never written to saved state")
        return embed

    def make_debugmods_embed(self, snapshots: dict[str, dict[str, Any]], error: Optional[str] = None) -> discord.Embed:
        embed = discord.Embed(
            title="parsed mod info",
            colour=discord.Colour.dark_teal(),
            timestamp=datetime.now(timezone.utc),
        )

        if error:
            embed.add_field(name="warning", value=f"fetch error: `{error}`", inline=False)

        for mod_id in TRACKED_MOD_IDS:
            snapshot = snapshots.get(mod_id)
            if not snapshot:
                embed.add_field(name=mod_id, value="not found in api payload.", inline=False)
                continue

            candidates = snapshot.get("version_candidates", [])
            candidate_lines = []
            for item in candidates[:6]:
                version = item.get("version") or "unknown"
                status = item.get("status") or ("pending" if item.get("pending") else "released")
                suffix = " (pending)" if item.get("pending") else ""
                candidate_lines.append(f"- {version}{suffix} | {status}")

            field_value = (
                f"**name:** {snapshot.get('name')}\n"
                f"**version:** {snapshot.get('version') or 'unknown'}\n"
                f"**display:** {snapshot.get('display_version')}\n"
                f"**status:** {snapshot.get('status')}\n"
                f"**pending:** {snapshot.get('pending')}\n"
                f"**candidates:**\n" + ("\n".join(candidate_lines) if candidate_lines else "- none parsed")
            )
            embed.add_field(name=mod_id, value=field_value[:1024], inline=False)

        return embed

    def make_debugstate_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="saved state",
            colour=discord.Colour.dark_gold(),
            timestamp=datetime.now(timezone.utc),
        )
        pretty = json.dumps(self.state, indent=2, ensure_ascii=False, sort_keys=True)
        if len(pretty) > 3900:
            pretty = pretty[:3900] + "\n..."
        embed.description = f"```json\n{pretty}\n```"
        return embed


bot = GeodeVersionBot()


async def safe_defer(interaction: discord.Interaction) -> None:
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
    except Exception:
        log.exception("failed to defer interaction")


@bot.tree.command(name="checkforupdates", description="check the tracked geode mods for version changes")
async def checkforupdates(interaction: discord.Interaction) -> None:
    await safe_defer(interaction)
    snapshots, error = await bot.build_report()
    embed = bot.make_check_embed(snapshots, error)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="debugmods", description="show parsed version info from the geode api")
async def debugmods(interaction: discord.Interaction) -> None:
    await safe_defer(interaction)
    snapshots, error = await bot.build_report()
    embed = bot.make_debugmods_embed(snapshots, error)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="debugstate", description="show the saved json state")
async def debugstate(interaction: discord.Interaction) -> None:
    await safe_defer(interaction)
    embed = bot.make_debugstate_embed()
    await interaction.followup.send(embed=embed)


@bot.event
async def on_ready() -> None:
    log.info("logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set")

    bot.run(TOKEN)


if __name__ == "__main__":
    main()
