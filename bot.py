import asyncio
import json
import logging
import os
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional, List

import aiohttp
import discord
from discord.ext import commands

token = os.getenv("DISCORD_TOKEN")
api_url = "https://api.geode-sdk.org/v1/mods/{}"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("geode")


def normalize_single_mod_response(data: Any) -> dict:
    """Safely extracts a single mod object regardless of how the API wraps it."""
    if isinstance(data, dict):
        payload = data.get("payload", data)
        if isinstance(payload, dict):
            # Sometimes the payload itself contains a 'data' object
            if "data" in payload and isinstance(payload["data"], dict):
                return payload["data"]
            return payload
    return {}


def normalize_list_response(data: Any) -> dict:
    """Safely extracts a list of mods and count from the API response."""
    if isinstance(data, list):
        return {"count": len(data), "data": data}
    if isinstance(data, dict):
        payload = data.get("payload", data)
        if isinstance(payload, list):
            return {"count": len(payload), "data": payload}
        if isinstance(payload, dict):
            if "data" in payload and isinstance(payload["data"], list):
                return payload
    return {"count": 0, "data": []}


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
    if status:
        return status not in ("accepted", "approved")
    return False


def find_version(d: dict) -> Optional[str]:
    if not isinstance(d, dict):
        return None
        
    # Check top-level properties first (API V1 standard)
    if "version" in d and isinstance(d["version"], str):
        return d["version"]
    if "latest_version" in d and isinstance(d["latest_version"], str):
        return d["latest_version"]
        
    # Fallback to versions array
    versions = d.get("versions")
    if isinstance(versions, list) and len(versions) > 0:
        first_version = versions[0]
        if isinstance(first_version, dict):
            return first_version.get("version")
            
    return None


def find_developer(mod_data: dict) -> str:
    if not isinstance(mod_data, dict):
        return "Unknown Developer"
        
    dev = mod_data.get("developer")
    if dev and isinstance(dev, str):
        return dev
        
    developers = mod_data.get("developers")
    if isinstance(developers, list) and len(developers) > 0:
        first = developers[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("display_name") or first.get("username") or "Unknown Developer"
            
    owner = mod_data.get("owner")
    if owner and isinstance(owner, str):
        return owner
        
    return "Unknown Developer"


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


def find_mod_url(d: dict, mod_id: str) -> str:
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
    return f"https://geode-sdk.org/mods/{mod_id}"


def format_error_reason(error: Any) -> str:
    text = str(error).strip() if error is not None else "unknown error"
    text = " ".join(text.split())
    if not text:
        text = "unknown error"
    return text[:180]


def build_single_mod_embed(mod_data: dict) -> discord.Embed:
    mod_id = mod_data.get("id") or "unknown.id"
    name = mod_data.get("name") or mod_id
    dev = find_developer(mod_data)
    desc = mod_data.get("description") or "No description provided."
    version = find_version(mod_data) or "Unknown"
    downloads = find_downloads(mod_data)
    pending = is_pending(mod_data)
    url = find_mod_url(mod_data, mod_id)

    color = discord.Color.gold() if pending else discord.Color.brand_green()

    embed = discord.Embed(
        title=f"{name} ({version})",
        description=desc,
        color=color,
        url=url,
        timestamp=datetime.now(timezone.utc)
    )
    
    embed.set_author(name=f"Created by {dev}")
    
    dl_text = f"{downloads:,}" if downloads is not None else "Unknown"
    status_text = "⏳ Pending" if pending else "✅ On the index"

    embed.add_field(name="Mod ID", value=f"`{mod_id}`", inline=True)
    embed.add_field(name="Downloads", value=dl_text, inline=True)
    embed.add_field(name="Status", value=status_text, inline=True)

    tags = mod_data.get("tags", [])
    if tags:
        embed.add_field(name="Tags", value=", ".join(tags), inline=False)
        
    embed.set_footer(text="Geode Index")
    return embed


def build_list_embed(title: str, mods: list, page: int, total_pages: int) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    lines = []
    
    for i, m in enumerate(mods, 1):
        mod_id = m.get("id") or "unknown.id"
        name = m.get("name") or mod_id
        dev = find_developer(m)
        dl = find_downloads(m) or 0
        lines.append(f"**{i}. [{name}](https://geode-sdk.org/mods/{mod_id})** by {dev}\n> 📦 `{mod_id}` • ⬇️ {dl:,} downloads")

    if not lines:
        embed.description = "*No mods found. Try a different search!*"
    else:
        embed.description = "\n\n".join(lines)

    embed.set_footer(text=f"Page {page} of {max(1, total_pages)} • Use the dropdown below to view details")
    return embed


class ModSelect(discord.ui.Select):
    def __init__(self, mods: list):
        options = []
        for m in mods:
            mod_id = (m.get("id") or "unknown.id")[:90]
            name = (m.get("name") or mod_id)[:90]
            options.append(discord.SelectOption(label=name, description=mod_id, value=mod_id))
            
        super().__init__(
            placeholder="Select a mod from this page to see its info...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        mod_id = self.values[0]
        # Defer response to handle API fetch latency safely
        await interaction.response.defer(ephemeral=True)
        mod_data = await interaction.client.fetch_single_mod(mod_id)
        
        if "error" in mod_data:
            await interaction.followup.send(f"❌ Oops, ran into an issue fetching that mod: {mod_data['error']}", ephemeral=True)
            return
            
        embed = build_single_mod_embed(mod_data)
        await interaction.followup.send(embed=embed, ephemeral=True)


class ModSearchView(discord.ui.View):
    def __init__(self, bot, query: str = None, is_trending: bool = False):
        super().__init__(timeout=300)
        self.bot = bot
        self.query = query
        self.is_trending = is_trending
        self.page = 1
        self.per_page = 5
        self.total_pages = 1
        self.mods = []

    async def load_data(self):
        data = await self.bot.fetch_mods_list(query=self.query, sort="downloads", page=self.page, per_page=self.per_page)
        self.mods = data.get("data", [])
        count = data.get("count", 0)
        self.total_pages = max(1, (count + self.per_page - 1) // self.per_page)

    def update_items(self):
        self.clear_items()
        
        self.btn_prev.disabled = self.page <= 1
        self.btn_next.disabled = self.page >= self.total_pages
        
        self.add_item(self.btn_prev)
        self.add_item(self.btn_next)

        if self.mods:
            self.add_item(ModSelect(self.mods))

    async def generate_view(self):
        await self.load_data()
        self.update_items()
        title = "🔥 Trending Geode Mods" if self.is_trending else f"🔍 Search Results: {self.query}"
        return build_list_embed(title, self.mods, self.page, self.total_pages)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="prev")
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        embed = await self.generate_view()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary, custom_id="next")
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        embed = await self.generate_view()
        await interaction.response.edit_message(embed=embed, view=self)


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=discord.Intents.default(),
        )
        self.session: Optional[aiohttp.ClientSession] = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.tree.sync()
        log.info("slash commands synced")

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def fetch_single_mod(self, mod_id: str) -> dict:
        try:
            async with self.session.get(api_url.format(mod_id)) as r:
                if r.status == 404:
                    return {"error": "Mod not found."}
                r.raise_for_status()
                data = await r.json()
                return normalize_single_mod_response(data)
        except Exception as e:
            return {"error": format_error_reason(e)}

    async def fetch_mods_list(self, query: str = None, sort: str = "downloads", page: int = 1, per_page: int = 5) -> dict:
        url = "https://api.geode-sdk.org/v1/mods"
        params = {"page": page, "per_page": per_page}
        if query:
            params["query"] = query
        if sort:
            params["sort"] = sort

        try:
            async with self.session.get(url, params=params) as r:
                if r.status == 200:
                    data = await r.json()
                    return normalize_list_response(data)
                return {"count": 0, "data": []}
        except Exception:
            return {"count": 0, "data": []}


bot = Bot()

# ==========================================
# GEODE COMMANDS
# ==========================================

@bot.tree.command(
    name="checkforupdates",
    description="Browse trending Geode mods, search the index, or view a specific mod's status",
)
@discord.app_commands.describe(
    mod_id="Select a specific mod to view (autocompletes from the API)",
    search="Search for a mod by its name"
)
async def checkforupdates(
    interaction: discord.Interaction,
    mod_id: Optional[str] = None,
    search: Optional[str] = None,
):
    await interaction.response.defer()

    if mod_id:
        mod_data = await bot.fetch_single_mod(mod_id)
        if "error" in mod_data:
            return await interaction.followup.send(f"❌ {mod_data['error']}")
        
        embed = build_single_mod_embed(mod_data)
        await interaction.followup.send(embed=embed)
        
    elif search:
        view = ModSearchView(bot, query=search, is_trending=False)
        embed = await view.generate_view()
        await interaction.followup.send(embed=embed, view=view)
        
    else:
        view = ModSearchView(bot, query=None, is_trending=True)
        embed = await view.generate_view()
        await interaction.followup.send(embed=embed, view=view)


@checkforupdates.autocomplete("mod_id")
async def checkforupdates_mod_autocomplete(interaction: discord.Interaction, current: str):
    if not current:
        return []
    
    data = await bot.fetch_mods_list(query=current, sort="downloads", page=1, per_page=15)
    mods = data.get("data", [])
    
    choices = []
    for m in mods:
        mod_id = m.get('id') or "unknown"
        name = m.get('name') or mod_id
        choices.append(discord.app_commands.Choice(name=f"{name} ({mod_id})", value=mod_id))
        
    return choices[:25]


@bot.tree.command(
    name="erymanthus", 
    description="Have a mod idea? Check if someone has already made it on the Geode index!"
)
@discord.app_commands.describe(
    search="Describe your mod idea to see if it exists"
)
async def erymanthus(interaction: discord.Interaction, search: str):
    await interaction.response.defer()

    data = await bot.fetch_mods_list(query=search, sort="downloads", page=1, per_page=5)
    mods = data.get("data", [])

    if not mods:
        embed = discord.Embed(
            title="💡 Idea Check: Clear!",
            description=(
                f"Great news! We couldn't find any existing mods matching: **{search}**.\n\n"
                "*Note: This just checks existing mod names and descriptions. Someone might have made your idea but named it differently, so it never hurts to ask around!*"
            ),
            color=discord.Color.brand_green()
        )
    else:
        embed = discord.Embed(
            title="🤔 Idea Check: Similar Mods Found",
            description=f"We found some existing mods that might match your idea for **{search}**:\n\n",
            color=discord.Color.orange()
        )

        for m in mods:
            mod_id = m.get("id") or "unknown.id"
            name = m.get("name") or mod_id
            desc = m.get("description", "No description.")[:100]
            
            if len(m.get("description", "")) > 100:
                desc += "..."
                
            embed.description += f"**[{name}](https://geode-sdk.org/mods/{mod_id})** (`{mod_id}`)\n> {desc}\n\n"

        embed.description += "\n*Note: This just searches current mod descriptions and titles. If none of these match what you're thinking, go for it!*"

    await interaction.followup.send(embed=embed)


# ==========================================
# GEODE DEVELOPER TOOLS (/dev command)
# ==========================================

@bot.tree.command(name="dev", description="Developer utilities for the Geode SDK")
@discord.app_commands.describe(
    command="The developer utility command to run",
    topic="Fetch a specific topic/search term (only for 'docs')",
    mod_id="The ID of the mod (only for 'repo', e.g., geode.loader)"
)
@discord.app_commands.choices(command=[
    discord.app_commands.Choice(name="docs", value="docs"),
    discord.app_commands.Choice(name="cli", value="cli"),
    discord.app_commands.Choice(name="status", value="status"),
    discord.app_commands.Choice(name="template", value="template"),
    discord.app_commands.Choice(name="repo", value="repo"),
    discord.app_commands.Choice(name="help", value="help"),
])
async def dev(
    interaction: discord.Interaction, 
    command: discord.app_commands.Choice[str], 
    topic: Optional[str] = None, 
    mod_id: Optional[str] = None
):
    cmd = command.value

    # --- DOCS ---
    if cmd == "docs":
        base_url = "https://docs.geode-sdk.org/"
        if topic:
            query = urllib.parse.quote(topic)
            await interaction.response.send_message(f"📚 Search the Geode Docs for **{topic}**: {base_url}?q={query}")
        else:
            await interaction.response.send_message(f"📚 Official Geode SDK Documentation: {base_url}")

    # --- CLI ---
    elif cmd == "cli":
        embed = discord.Embed(title="Geode CLI Quick-Start", color=discord.Color.green())
        embed.add_field(name="`geode new`", value="Create a new Geode project with the setup wizard.", inline=False)
        embed.add_field(name="`geode build`", value="Configure and build the current project.", inline=False)
        embed.add_field(name="`geode package`", value="Package the compiled mod into a `.geode` file.", inline=False)
        embed.add_field(name="`geode run`", value="Run Geometry Dash with Geode.", inline=False)
        embed.add_field(name="`geode profile`", value="Manage your Geometry Dash profiles.", inline=False)
        await interaction.response.send_message(embed=embed)

    # --- STATUS ---
    elif cmd == "status":
        await interaction.response.defer()
        
        api_status = "Unknown"
        loader_ver = "Unknown"

        try:
            async with bot.session.get("https://api.geode-sdk.org/") as r:
                if r.status in (200, 404):
                    api_status = "✅ Online"
                else:
                    api_status = f"⚠️ HTTP {r.status}"
        except Exception:
            api_status = "❌ Offline / Unreachable"

        try:
            async with bot.session.get(api_url.format("geode.loader")) as r:
                if r.status == 200:
                    data = await r.json()
                    mod_obj = normalize_single_mod_response(data)
                    loader_ver = find_version(mod_obj) or "Unknown"
        except Exception:
            pass

        embed = discord.Embed(title="Geode Index & Server Status", color=discord.Color.blurple())
        embed.add_field(name="Geode Index API", value=api_status, inline=True)
        embed.add_field(name="Latest Loader Ver", value=loader_ver, inline=True)
        embed.add_field(name="API Documentation", value="[Swagger UI](https://api.geode-sdk.org/swagger/)", inline=False)
        
        await interaction.followup.send(embed=embed)

    # --- TEMPLATE ---
    elif cmd == "template":
        code = (
            "```cpp\n"
            "#include <Geode/Geode.hpp>\n"
            "#include <Geode/modify/MenuLayer.hpp>\n\n"
            "using namespace geode::prelude;\n\n"
            "class $modify(MyMenuLayer, MenuLayer) {\n"
            "    bool init() {\n"
            "        if (!MenuLayer::init()) return false;\n\n"
            "        FLAlertLayer::create(\"Geode\", \"Hello World from Geode!\", \"OK\")->show();\n\n"
            "        return true;\n"
            "    }\n"
            "};\n"
            "```"
        )
        await interaction.response.send_message(f"Here is a standard Geode `Hello World` boilerplate:\n{code}")

    # --- REPO ---
    elif cmd == "repo":
        if not mod_id:
            await interaction.response.send_message("❌ Please provide a `mod_id` to use the repo command.", ephemeral=True)
            return

        await interaction.response.defer()
        
        try:
            async with bot.session.get(api_url.format(mod_id)) as r:
                if r.status == 200:
                    data = await r.json()
                    mod_obj = normalize_single_mod_response(data)
                    
                    # Search multiple common JSON paths for the repository URL
                    source_url = None
                    links = mod_obj.get("links")
                    if isinstance(links, dict):
                        source_url = links.get("source") or links.get("repository")
                    if not source_url:
                        source_url = mod_obj.get("repository") or mod_obj.get("source")
                        
                    if source_url:
                        await interaction.followup.send(f"🔗 **Source code for `{mod_id}`:**\n{source_url}")
                    else:
                        await interaction.followup.send(f"❌ No source code link was found on the index for `{mod_id}`.")
                elif r.status == 404:
                    await interaction.followup.send(f"❌ Mod `{mod_id}` not found on the index.")
                else:
                    await interaction.followup.send(f"❌ API Error: HTTP {r.status}")
        except Exception as e:
            await interaction.followup.send(f"❌ Error fetching mod repository: {format_error_reason(e)}")

    # --- HELP ---
    elif cmd == "help":
        embed = discord.Embed(title="Geode Developer - Common Issues", color=discord.Color.red())
        embed.add_field(
            name="Missing Headers / Bindings Not Found", 
            value="Ensure you ran `geode build` (or your CMake configure step) to generate the GD bindings. If your IDE still warns, try reloading your CMake project.", 
            inline=False
        )
        embed.add_field(
            name="CMake Not Found", 
            value="Make sure CMake is installed and added to your system `PATH` variable.", 
            inline=False
        )
        embed.add_field(
            name="Linker Errors (LNK2001 / LNK2019)", 
            value="Usually caused by an incorrect function signature inside your `$modify` block, or missing a `GEODE_API` macro on an exported class.", 
            inline=False
        )
        embed.add_field(
            name="Game Crashes Immediately", 
            value="Double check your dependencies in `mod.json` and ensure you aren't trying to access layers before they are fully initialized.", 
            inline=False
        )
        embed.add_field(
            name="Need More Info?", 
            value="Check out the [Troubleshooting Guide](https://docs.geode-sdk.org/troubleshooting) in the official docs.", 
            inline=False
        )
        await interaction.response.send_message(embed=embed)


def main():
    if not token:
        raise RuntimeError("DISCORD_TOKEN missing")
    bot.run(token)

if __name__ == "__main__":
    main()
