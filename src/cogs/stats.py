import logging
import re
from urllib.parse import unquote, urlparse

import discord
from discord.ext import commands, tasks

from ..config import Config
from ..utils.media_utils import MediaUtils

logger = logging.getLogger(__name__)
TICK_SECONDS = 30


def stats_title_key(item) -> str | None:
    """Stable title identity; upgraded movie encodes match, TV episodes remain distinct."""
    if item is None:
        return None
    source = item.get("uri") or item.get("name") or ""
    if not source:
        return None
    decoded = unquote(str(source))
    parsed = urlparse(decoded)
    filename = parsed.path if parsed.scheme else decoded
    title, season, episode, year = MediaUtils.parse_tv_filename(filename)
    # Strip common encode/release suffixes after the canonical title.
    title = re.split(
        r"\b(?:2160p|1080p|720p|uhd|blu[ .-]?ray|web[ .-]?dl|webrip|brrip|remux|x26[45]|h26[45])\b",
        title or "",
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    normalized = re.sub(r"\W+", " ", title.lower()).strip()
    if not normalized:
        return None
    if season is not None and episode is not None:
        return f"{normalized}|S{season:02d}E{episode:02d}"
    return f"{normalized}|{year or ''}"


def _format_ladder(rows: list, value) -> str:
    return "\n".join(
        f"{rank}. {discord.utils.escape_markdown(name)} — {value(amount)}"
        for rank, (name, amount) in enumerate(rows, 1)
    )


def build_stats_embed(stats) -> discord.Embed:
    hours, finished = stats.ladders()
    embed = discord.Embed(title="Watch Leaderboard", color=discord.Color.gold())
    if not hours:
        embed.description = "No watch time recorded yet."
        return embed
    embed.add_field(
        name="Hours watched",
        value=_format_ladder(hours, lambda seconds: f"{seconds / 3600:.1f} h"),
        inline=False,
    )
    embed.add_field(
        name="Titles finished",
        value=_format_ladder(finished, str) or "Nobody has finished a title yet.",
        inline=False,
    )
    return embed


class StatsCommands(commands.Cog):
    """Tracks viewers in the configured voice room and provides a leaderboard."""

    def __init__(self, bot: commands.Bot, vlc, stats):
        self.bot = bot
        self.vlc = vlc
        self.stats = stats

    async def cog_load(self):
        self._track.start()

    async def cog_unload(self):
        self._track.cancel()

    @tasks.loop(seconds=TICK_SECONDS)
    async def _track(self):
        try:
            channel = self.bot.get_channel(Config.VOICE_JOIN_CHANNEL_ID)
            if not isinstance(channel, discord.VoiceChannel):
                return
            viewers = {member.id: member.display_name for member in channel.members if not member.bot}
            if not viewers:
                return
            status, playlist = await self.vlc.run_async("get_playback_snapshot")
            if status is None or status.findtext("state") != "playing":
                return
            leaves = playlist.findall(".//leaf") if playlist is not None else []
            item = next((leaf for leaf in leaves if leaf.get("current")), None)
            self.stats.record(
                viewers,
                stats_title_key(item),
                int(status.findtext("time") or 0),
                int(status.findtext("length") or 0),
                TICK_SECONDS,
            )
            self.stats.save()
        except Exception:
            logger.exception("Watch stats tick failed")

    @_track.before_loop
    async def _before_track(self):
        await self.bot.wait_until_ready()
        if not isinstance(self.bot.get_channel(Config.VOICE_JOIN_CHANNEL_ID), discord.VoiceChannel):
            logger.warning(
                "Watch stats inactive: VOICE_JOIN_CHANNEL_ID is not a voice channel the bot can see"
            )

    @commands.command(name="stats", aliases=["leaderboard"])
    @commands.guild_only()
    async def stats_cmd(self, ctx: commands.Context):
        """Show the watch leaderboard."""
        await ctx.send(embed=build_stats_embed(self.stats))
