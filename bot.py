import asyncio
import logging
import os

import discord
from discord import app_commands
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
SCORES_CHANNEL_ID = 1398149394288873572   # channel where users add points
SCOREBOARD_CHANNEL_ID = 1398149394288873572  # where the embed lives (can be same channel)
PAGE_SIZE = 10  # players per page

# Users who can use admin commands regardless of server permissions
ADMIN_IDS = {353320170196041749, 590038326631858196}
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── MongoDB setup ─────────────────────────────────────────────────────────────
mongo = MongoClient(MONGO_URI)
db = mongo["scoreboard"]
scores_col = db["scores"]

# Application commands use the interaction payload and Discord REST API. They do
# not need message events, member-list tracking, or message content.
intents = discord.Intents.none()
intents.guilds = True


class ScoreboardBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Sync once per login. Commands persist on Discord and are not re-synced
        # for each gateway reconnect.
        synced = await self.tree.sync()
        logger.info("Synced %d guild-only application commands.", len(synced))


bot = ScoreboardBot()


# ── Auth and interaction helpers ──────────────────────────────────────────────

def is_admin(interaction: discord.Interaction) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    return interaction.user.id in ADMIN_IDS or bool(
        permissions and permissions.administrator
    )


def is_score_channel(interaction: discord.Interaction) -> bool:
    return interaction.channel_id == SCORES_CHANNEL_ID


async def send_private(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


# ── Database helpers ──────────────────────────────────────────────────────────

def get_scores() -> list:
    return list(scores_col.find({}, {"_id": 0}).sort("points", -1))


def add_point(user_id: str, display_name: str, amount: int = 1) -> None:
    scores_col.update_one(
        {"user_id": user_id},
        {"$inc": {"points": amount}, "$set": {"name": display_name}},
        upsert=True
    )


def remove_points(user_id: str, amount: int) -> bool:
    doc = scores_col.find_one({"user_id": user_id})
    if not doc or doc.get("points", 0) == 0:
        return False
    new_total = max(0, doc["points"] - amount)
    if new_total == 0:
        scores_col.delete_one({"user_id": user_id})
    else:
        scores_col.update_one({"user_id": user_id}, {"$set": {"points": new_total}})
    return True


def add_trophy(user_id: str, display_name: str, amount: int = 1) -> None:
    scores_col.update_one(
        {"user_id": user_id},
        {"$inc": {"trophies": amount}, "$set": {"name": display_name}},
        upsert=True
    )


def remove_trophy(user_id: str, amount: int = 1) -> bool:
    doc = scores_col.find_one({"user_id": user_id})
    if not doc or doc.get("trophies", 0) == 0:
        return False
    new_total = max(0, doc["trophies"] - amount)
    scores_col.update_one({"user_id": user_id}, {"$set": {"trophies": new_total}})
    return True


# ── Embed builder ─────────────────────────────────────────────────────────────

def get_medal(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"**{rank}.**")


def build_embed(page: int = 0) -> tuple[discord.Embed, int]:
    scores = get_scores()
    total_pages = max(1, -(-len(scores) // PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_scores = scores[start:end]

    if not scores:
        description = "No scores yet — use /point in the score channel to get started!"
    else:
        lines = []
        for i, entry in enumerate(page_scores, start=start + 1):
            medal = get_medal(i)
            name = entry.get("name", "Unknown")
            pts = entry.get("points", 0)
            trophies = entry.get("trophies", 0)

            trophy_str = "  ".join(["🏆"] * trophies) if trophies > 0 else ""
            trophy_part = f"   |   {trophy_str}" if trophy_str else ""

            lines.append(f"{medal} **{name}** — {pts} pt{'s' if pts != 1 else ''}{trophy_part}")
        description = "\n".join(lines)

    embed = discord.Embed(
        title="🏆 Scoreboard",
        description=description,
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Page {page + 1} of {total_pages} • Use /point to add a point")
    embed.timestamp = discord.utils.utcnow()
    return embed, total_pages


# ── Pagination view ───────────────────────────────────────────────────────────

class ScoreboardView(discord.ui.View):
    def __init__(self, page: int = 0):
        super().__init__(timeout=180)
        self.page = page
        self._navigation_lock = asyncio.Lock()

    async def navigate(self, interaction: discord.Interaction, action: str) -> None:
        # Acknowledge before a database read or waiting for another button click.
        await interaction.response.defer()
        async with self._navigation_lock:
            if action == "first":
                self.page = 0
            elif action == "previous":
                self.page -= 1
            elif action == "next":
                self.page += 1
            elif action == "last":
                _, total_pages = build_embed(self.page)
                self.page = total_pages - 1

            embed, total_pages = build_embed(self.page)
            self.page = max(0, min(self.page, total_pages - 1))
            self.first_button.disabled = self.page == 0
            self.prev_button.disabled = self.page == 0
            self.next_button.disabled = self.page >= total_pages - 1
            self.last_button.disabled = self.page >= total_pages - 1
            await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def first_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.navigate(interaction, "first")

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.navigate(interaction, "previous")

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.navigate(interaction, "next")

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def last_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.navigate(interaction, "last")


# ── Core: post the scoreboard embed ───────────────────────────────────────────

async def update_scoreboard() -> None:
    channel = bot.get_channel(SCOREBOARD_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(SCOREBOARD_CHANNEL_ID)

    embed, total_pages = build_embed(0)
    view = ScoreboardView(page=0)

    view.first_button.disabled = True
    view.prev_button.disabled = True
    if total_pages <= 1:
        view.next_button.disabled = True
        view.last_button.disabled = True

    await channel.send(embed=embed, view=view)


# ── Guild-only application commands ───────────────────────────────────────────

@bot.tree.command(name="help", description="List the available bot commands.")
@app_commands.guild_only()
async def help_cmd(interaction: discord.Interaction) -> None:
    """Show the available commands and where they can be used."""
    await send_private(
        interaction,
        "**UNO Scoreboard commands**\n"
        "`/help` — show this command list.\n"
        "`/point` — add one point for yourself in the configured score channel.\n"
        "`/scores` — post the scoreboard in its configured channel.\n"
        "Admin commands (configured admin IDs or server administrators):\n"
        "`/addpoints` and `/removepoints` — change a member's points.\n"
        "`/addtrophy` and `/removetrophy` — change a member's trophies.\n"
        "Select a member from this server and enter a positive amount for admin commands."
    )


@bot.tree.command(name="point", description="Add one point to yourself in the score channel.")
@app_commands.guild_only()
async def point_cmd(interaction: discord.Interaction) -> None:
    """Add one point for the caller in the configured score channel."""
    if not is_score_channel(interaction):
        await interaction.response.send_message("Use /point in the configured score channel.")
        return

    await interaction.response.defer()
    add_point(str(interaction.user.id), interaction.user.display_name)
    await interaction.followup.send("✅ Added one point.")


@bot.tree.command(name="scores", description="Post the scoreboard in its configured channel.")
@app_commands.guild_only()
async def scores_cmd(interaction: discord.Interaction) -> None:
    """Show the scoreboard."""
    await interaction.response.defer(ephemeral=True)
    await update_scoreboard()
    await interaction.followup.send("Scoreboard posted.", ephemeral=True)


@bot.tree.command(name="addpoints", description="[Admin] Add points to a guild member.")
@app_commands.guild_only()
@app_commands.describe(member="Member to receive points", amount="Positive number of points")
async def addpoints_cmd(
    interaction: discord.Interaction, member: discord.Member, amount: int
) -> None:
    """Add points to a member selected from the current guild."""
    if not is_admin(interaction):
        await send_private(interaction, "❌ You don't have permission to use that command.")
        return
    if amount <= 0:
        await send_private(interaction, "❌ Amount must be a positive number.")
        return

    await interaction.response.defer(ephemeral=True)
    add_point(str(member.id), member.display_name, amount)
    await interaction.followup.send("✅ Points added.", ephemeral=True)


@bot.tree.command(name="removepoints", description="[Admin] Remove points from a guild member.")
@app_commands.guild_only()
@app_commands.describe(member="Member whose points should be removed", amount="Positive number of points")
async def removepoints_cmd(
    interaction: discord.Interaction, member: discord.Member, amount: int
) -> None:
    """Remove points from a member selected from the current guild."""
    if not is_admin(interaction):
        await send_private(interaction, "❌ You don't have permission to use that command.")
        return
    if amount <= 0:
        await send_private(interaction, "❌ Amount must be a positive number.")
        return

    await interaction.response.defer(ephemeral=True)
    if remove_points(str(member.id), amount):
        await interaction.followup.send("✅ Points removed.", ephemeral=True)
    else:
        await interaction.followup.send(
            f"**{member.display_name}** has no points to remove.", ephemeral=True
        )


@bot.tree.command(name="addtrophy", description="[Admin] Add trophies to a guild member.")
@app_commands.guild_only()
@app_commands.describe(member="Member to receive trophies", amount="Positive number of trophies")
async def addtrophy_cmd(
    interaction: discord.Interaction, member: discord.Member, amount: int = 1
) -> None:
    """Add trophies to a member selected from the current guild."""
    if not is_admin(interaction):
        await send_private(interaction, "❌ You don't have permission to use that command.")
        return
    if amount <= 0:
        await send_private(interaction, "❌ Amount must be a positive number.")
        return

    await interaction.response.defer(ephemeral=True)
    add_trophy(str(member.id), member.display_name, amount)
    await interaction.followup.send("✅ Trophies added.", ephemeral=True)


@bot.tree.command(name="removetrophy", description="[Admin] Remove trophies from a guild member.")
@app_commands.guild_only()
@app_commands.describe(member="Member whose trophies should be removed", amount="Positive number of trophies")
async def removetrophy_cmd(
    interaction: discord.Interaction, member: discord.Member, amount: int = 1
) -> None:
    """Remove trophies from a member selected from the current guild."""
    if not is_admin(interaction):
        await send_private(interaction, "❌ You don't have permission to use that command.")
        return
    if amount <= 0:
        await send_private(interaction, "❌ Amount must be a positive number.")
        return

    await interaction.response.defer(ephemeral=True)
    if remove_trophy(str(member.id), amount):
        await interaction.followup.send("✅ Trophies removed.", ephemeral=True)
    else:
        await interaction.followup.send(
            f"**{member.display_name}** has no trophies to remove.", ephemeral=True
        )


# ── Error handling ────────────────────────────────────────────────────────────

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    cause = error.original if isinstance(error, app_commands.CommandInvokeError) else error

    if isinstance(error, app_commands.CheckFailure):
        message = "❌ This command can only be used in a server."
    elif isinstance(cause, discord.Forbidden):
        message = "❌ The bot does not have permission to complete that action."
    elif isinstance(cause, discord.NotFound):
        message = "❌ The configured channel or message could not be found."
    elif isinstance(cause, discord.HTTPException):
        message = "❌ Discord could not complete that action. Please try again."
    else:
        # Log only the error class; exception strings can contain user data or
        # connection details. Never echo those details back to Discord.
        logger.error("Application command failed (%s).", type(cause).__name__)
        message = "❌ The command failed. Please try again later."

    await send_private(interaction, message)


@bot.event
async def on_ready():
    logger.info("Bot connected.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN must be configured.")
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
