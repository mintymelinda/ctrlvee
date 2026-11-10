

import asyncio
import discord
from discord.ext import commands, tasks
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import logging
import json
import os
import re
from urllib.parse import unquote
from ..config import Config
from ..utils.media_utils import MediaUtils

# Cross-platform timezone handling for PH time
def get_ph_timezone():
    try:
        return ZoneInfo("Asia/Manila")
    except ZoneInfoNotFoundError:
        try:
            # On Windows, try Singapore Standard Time (same UTC+8)
            return ZoneInfo("Singapore Standard Time")
        except Exception:
            from tzlocal import get_localzone
            # Fallback: use system local timezone (may be incorrect)
            return get_localzone()

PH_TZ = get_ph_timezone()
logger = logging.getLogger(__name__)


def _title_key(name: str):
    """Return a normalized movie title/year key for matching upgraded files."""
    title, year = MediaUtils.parse_movie_filename(unquote(name or ""))
    normalized = re.sub(r"\W+", " ", (title or "").lower()).strip()
    return (normalized, year) if normalized else None


def _resolve_schedule_index(entry: dict, items: list, exists=lambda uri: True):
    """Resolve a saved schedule to its current playlist index."""
    uri = entry.get("uri")
    if not uri:
        index = entry["number"] - 1
        return index if 0 <= index < len(items) else None

    exact = next((i for i, leaf in enumerate(items) if leaf.get("uri") == uri), None)
    if exact is not None and exists(uri):
        return exact

    wanted = _title_key(uri.rsplit("/", 1)[-1])
    matches = [
        i for i, leaf in enumerate(items)
        if wanted and i != exact
        and _title_key(leaf.get("name", "")) == wanted
        and exists(leaf.get("uri"))
    ]
    return matches[-1] if matches else exact


SCHEDULE_BACKUP_FILE = "schedule_backup.json"


from src.services.tmdb_service import TMDBService
from ..utils.command_utils import format_cmd_inline

class Scheduler(commands.Cog):
    def __init__(self, bot, vlc_controller):
        self.bot = bot
        self.vlc = vlc_controller
        self.tmdb = TMDBService()
        self.scheduled = self._load_schedule_backup()
        self.pre_announce_sent = set()
        self._schedule_lock = asyncio.Lock()
        self.check_schedules.start()

    def cog_unload(self):
        self.check_schedules.cancel()
        self._save_schedule_backup()

    def _save_schedule_backup(self):
        temporary_file = f"{SCHEDULE_BACKUP_FILE}.tmp"
        try:
            with open(temporary_file, "w") as f:
                json.dump(self.scheduled, f, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_file, SCHEDULE_BACKUP_FILE)
            return True
        except Exception as e:
            logger.error(f"Error saving schedule backup: {e}")
            try:
                if os.path.exists(temporary_file):
                    os.unlink(temporary_file)
            except OSError:
                pass
            return False

    def _uri_exists(self, uri) -> bool:
        path = self.vlc._uri_to_path(uri)
        return bool(path) and self.vlc._file_exists_robust(path)

    def _rebind_due_schedules(self, now) -> dict:
        """Resolve schedules due within ten minutes and persist updated indexes."""
        due = [s for s in self.scheduled if (s["dt"] - now).total_seconds() <= 600]
        if not due:
            return {}

        playlist = self.vlc.get_playlist()
        items = playlist.findall(".//leaf") if playlist is not None else []
        resolved = {}
        changed = False
        for schedule in due:
            index = _resolve_schedule_index(schedule, items, self._uri_exists)
            resolved[id(schedule)] = (index, items[index] if index is not None else None)
            if index is None:
                continue
            leaf = items[index]
            if schedule["number"] != index + 1 or (
                schedule.get("uri") and schedule["uri"] != leaf.get("uri")
            ):
                logger.info(
                    "Rebinding schedule '%s' #%s -> #%s (%s)",
                    schedule.get("title"), schedule["number"], index + 1, leaf.get("name"),
                )
                schedule["number"] = index + 1
                schedule["uri"] = leaf.get("uri") or schedule.get("uri")
                changed = True

        if changed:
            self._save_schedule_backup()
        return resolved

    def _load_schedule_backup(self):
        if os.path.exists(SCHEDULE_BACKUP_FILE):
            try:
                with open(SCHEDULE_BACKUP_FILE, "r") as f:
                    data = json.load(f)
                # Convert dt strings back to datetime
                for s in data:
                    if isinstance(s.get("dt"), str):
                        s["dt"] = datetime.fromisoformat(s["dt"])
                    if isinstance(s.get("next_attempt_at"), str):
                        s["next_attempt_at"] = datetime.fromisoformat(s["next_attempt_at"])
                    s.setdefault("status", "pending")
                    s.setdefault("attempt_count", 0)
                return data
            except Exception as e:
                logger.error(f"Error loading schedule backup: {e}")
        return []

    @commands.command(name="schedule")
    async def schedule_movie(self, ctx, number: int, date: str, time: str):
        """Schedule a movie by playlist number and PH time.

        Usage: shown with the configured command prefix.
        """
        try:
            dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
            dt = dt.replace(tzinfo=PH_TZ)
            now = datetime.now(PH_TZ)
            if dt <= now:
                await ctx.send("❌ Scheduled time must be in the future (PH time).")
                return
            # Check for double scheduling (same movie, same time within 1 minute)
            for s in self.scheduled:
                if s["number"] == number and abs((s["dt"] - dt).total_seconds()) < 60:
                    await ctx.send(f"❌ Movie #{number} is already scheduled at {s['dt'].strftime('%Y-%m-%d %H:%M %Z')}. Double scheduling is not allowed.")
                    return
            # Get movie title and duration from playlist (on demand)
            playlist = self.vlc.get_playlist()
            items = playlist.findall('.//leaf') if playlist is not None else []
            idx = number - 1
            if not (0 <= idx < len(items)):
                await ctx.send(f"❌ Movie number {number} is out of bounds. There are {len(items)} items in the playlist.")
                return
            filename = items[idx].get('name', 'Unknown')
            title = MediaUtils.clean_movie_title(filename)
            item_uri = items[idx].get('uri')
            duration = MediaUtils.get_media_duration(items[idx])
            entry = {
                "number": number,
                "title": title,
                "uri": item_uri,
                "dt": dt,
                "user": ctx.author.id,
                "channel": ctx.channel.id,
                "duration": duration,
                "status": "pending",
                "attempt_count": 0
            }
            self.scheduled.append(entry)
            self._save_schedule_backup()
            if duration == 'Loading...':
                dur_str = 'Loading...'
            elif duration:
                dur_str = MediaUtils.format_time(duration)
            else:
                dur_str = "Unknown"
            embed = discord.Embed(
                title="Movie Scheduled",
                color=discord.Color.green()
            )
            embed.add_field(name="Number", value=f"#{number}", inline=True)
            embed.add_field(name="Title", value=title, inline=True)
            embed.add_field(name="Scheduled For", value=dt.strftime('%Y-%m-%d %H:%M %Z'), inline=False)
            embed.add_field(name="Duration", value=dur_str, inline=True)
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"❌ Invalid date/time format. Use: {format_cmd_inline('schedule <number> <YYYY-MM-DD> <HH:MM>')}")

    @commands.command(name="schedules")
    async def list_schedules(self, ctx):
        """List all upcoming scheduled movies."""
        if not self.scheduled:
            await ctx.send("No movies scheduled.")
            return
        embed = discord.Embed(title="Upcoming Scheduled Movies", color=discord.Color.purple())
        for s in sorted(self.scheduled, key=lambda x: x["dt"]):
            dt_str = s["dt"].strftime('%Y-%m-%d %H:%M %Z') if isinstance(s["dt"], datetime) else str(s["dt"])
            duration = s.get("duration")
            if duration == 'Loading...' or duration == 0:
                dur_str = 'Loading...'
            elif duration:
                dur_str = MediaUtils.format_time(duration)
            else:
                dur_str = "Unknown"
            embed.add_field(
                name=f"#{s['number']} — {s.get('title', 'Unknown')}",
                value=f"Scheduled for {dt_str}\nDuration: {dur_str}",
                inline=False
            )
        await ctx.send(embed=embed)

    @commands.command(name="unschedule")
    async def unschedule(self, ctx, number: int):
        """Remove all schedules for a given movie number."""
        before = len(self.scheduled)
        self.scheduled = [s for s in self.scheduled if s["number"] != number]
        self._save_schedule_backup()
        after = len(self.scheduled)
        if before == after:
            await ctx.send(f"No schedules found for movie #{number}.")
        else:
            await ctx.send(f"Removed all schedules for movie #{number}.")

    def _retry_schedule(self, schedule, now, reason):
        """Persist a bounded exponential retry without discarding the due job."""
        attempts = int(schedule.get("attempt_count", 0)) + 1
        delay = min(30 * (2 ** min(attempts - 1, 4)), 480)
        schedule["attempt_count"] = attempts
        schedule["status"] = "pending"
        schedule.pop("execution_started_at", None)
        schedule["last_error"] = str(reason)
        schedule["next_attempt_at"] = now.timestamp() + delay
        logger.warning(
            "Scheduled movie '%s' attempt %s failed; retrying in %ss: %s",
            schedule.get("title", "Unknown"), attempts, delay, reason,
        )
        self._save_schedule_backup()

    def _execute_due_schedule(self, schedule, leaf, now):
        """Execute one due schedule with a durable claim and restart reconciliation."""
        item_id = leaf.get("id") if leaf is not None else None

        # If a previous process died after sending the command, reconcile against
        # VLC's current item before deciding whether another command is needed.
        if schedule.get("status") == "executing":
            claimed_item_id = schedule.get("target_item_id")
            if (
                item_id is not None
                and claimed_item_id is not None
                and str(item_id) == str(claimed_item_id)
                and leaf.get("current") is not None
            ):
                self.scheduled = [entry for entry in self.scheduled if entry is not schedule]
                if not self._save_schedule_backup():
                    self.scheduled.append(schedule)
                    return "confirming"
                logger.info("Recovered completed scheduled playback for '%s'", schedule.get("title"))
                return "recovered"

            # VLC's HTTP command response does not confirm that pl_play actually
            # changed the current item. Allow a short transition window before
            # retrying an in-flight command, including when playlist reads fail.
            started_at = schedule.get("execution_started_at")
            if started_at:
                if isinstance(started_at, str):
                    started_at = datetime.fromisoformat(started_at)
                elapsed = (now - started_at).total_seconds()
                if elapsed < 90:
                    return "confirming"

            attempts = max(1, int(schedule.get("attempt_count", 1)))
            delay = min(30 * (2 ** min(attempts - 1, 4)), 480)
            schedule["status"] = "pending"
            schedule.pop("execution_started_at", None)
            schedule["last_error"] = "VLC did not confirm the scheduled item became current"
            schedule["next_attempt_at"] = now.timestamp() + delay
            self._save_schedule_backup()
            return "retry"

        next_attempt = schedule.get("next_attempt_at")
        if next_attempt is not None:
            if isinstance(next_attempt, (int, float)):
                retry_at = datetime.fromtimestamp(next_attempt, tz=now.tzinfo)
            else:
                retry_at = next_attempt
            if retry_at > now:
                return "deferred"

        if item_id is None:
            self._retry_schedule(schedule, now, "scheduled media could not be resolved in the VLC playlist")
            return "retry"

        # Persist the claim before calling VLC so overlapping loop invocations
        # and a restarted process can identify an interrupted attempt.
        schedule["status"] = "executing"
        schedule["execution_started_at"] = now.isoformat()
        schedule["attempt_count"] = int(schedule.get("attempt_count", 0)) + 1
        schedule["target_item_id"] = str(item_id)
        if not self._save_schedule_backup():
            schedule["status"] = "pending"
            schedule.pop("execution_started_at", None)
            logger.error(
                "Not starting scheduled movie '%s' because its execution claim "
                "could not be persisted",
                schedule.get("title", "Unknown"),
            )
            return "retry"

        try:
            result = self.vlc.play_item(item_id)
            succeeded = result is not None and result is not False
        except Exception as exc:
            succeeded = False
            error = exc
        else:
            error = "VLC rejected the playback command"

        if not succeeded:
            # The attempt count was already persisted with the execution claim.
            attempts = int(schedule.get("attempt_count", 1))
            delay = min(30 * (2 ** min(attempts - 1, 4)), 480)
            schedule["status"] = "pending"
            schedule.pop("execution_started_at", None)
            schedule["last_error"] = str(error)
            schedule["next_attempt_at"] = now.timestamp() + delay
            self._save_schedule_backup()
            logger.warning(
                "Scheduled movie '%s' failed to start; retrying in %ss: %s",
                schedule.get("title", "Unknown"), delay, error,
            )
            return "retry"

        # A successful HTTP/XML response only means VLC answered the request.
        # Confirm that the requested playlist item is now current before
        # consuming the durable schedule.
        try:
            playlist = self.vlc.get_playlist()
            current_item = next(
                (
                    entry for entry in playlist.findall(".//leaf")
                    if entry.get("current") is not None
                ),
                None,
            ) if playlist is not None else None
            confirmed = (
                current_item is not None
                and str(current_item.get("id")) == str(item_id)
            )
        except Exception as exc:
            logger.warning("Could not confirm scheduled VLC playback: %s", exc)
            confirmed = False

        if not confirmed:
            logger.info(
                "VLC accepted the play request for '%s', awaiting current-item confirmation",
                schedule.get("title", "Unknown"),
            )
            return "confirming"

        self.scheduled = [entry for entry in self.scheduled if entry is not schedule]
        if not self._save_schedule_backup():
            self.scheduled.append(schedule)
            return "confirming"
        return "started"

    async def _announce_schedule_started(self, schedule):
        """Send playback and metadata announcements after VLC accepts the command."""
        from src.config import Config

        announce_ids = Config.get_announce_channel_ids()
        channel_ids = set(announce_ids or [])
        if schedule.get("channel"):
            channel_ids.add(schedule["channel"])
        role_id = getattr(Config, "WATCH_ANNOUNCE_ROLE_ID", 0)
        mention = f'<@&{role_id}>' if role_id else ''
        msg = f"{mention} 🎬 Scheduled movie #{schedule['number']} ({schedule.get('title', 'Unknown')}) is now playing!".strip()

        for cid in channel_ids:
            channel = self.bot.get_channel(cid)
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(cid)
                except Exception:
                    channel = None
            if channel:
                try:
                    await channel.send(msg)
                except Exception:
                    pass

        title = schedule.get("title")
        year = None
        match = re.search(r"\((\d{4})\)$", title or "")
        if match:
            year = int(match.group(1))
            title = title[:match.start()].strip()
        if title:
            embed = self.tmdb.get_movie_metadata(title, year, file_path=schedule.get("uri"))
            if embed:
                for cid in channel_ids:
                    channel = self.bot.get_channel(cid)
                    if not channel:
                        try:
                            channel = await self.bot.fetch_channel(cid)
                        except Exception:
                            channel = None
                    if channel:
                        try:
                            await channel.send(embed=embed)
                        except Exception:
                            pass

    @tasks.loop(seconds=30)
    async def check_schedules(self):
        from src.config import Config

        async with self._schedule_lock:
            now = datetime.now(PH_TZ)
            try:
                resolved = self._rebind_due_schedules(now)
            except Exception as exc:
                logger.error("Error resolving scheduled items: %s", exc)
                resolved = {}

            # Pre-announce schedules within ten minutes. This is intentionally
            # separate from execution retry state.
            for schedule in list(self.scheduled):
                pre_announce_key = (schedule["number"], schedule["dt"])
                if pre_announce_key in self.pre_announce_sent:
                    continue
                delta = (schedule["dt"] - now).total_seconds()
                if not 0 < delta <= 600:
                    continue
                channel_ids = set(Config.get_announce_channel_ids() or [])
                if schedule.get("channel"):
                    channel_ids.add(schedule["channel"])
                role_id = getattr(Config, "WATCH_ANNOUNCE_ROLE_ID", 0)
                mention = f'<@&{role_id}>' if role_id else ''
                for channel_id in channel_ids:
                    channel = self.bot.get_channel(channel_id)
                    if not channel:
                        try:
                            channel = await self.bot.fetch_channel(channel_id)
                        except Exception:
                            continue
                    try:
                        await channel.send(
                            f"{mention} ⏰ Reminder: Scheduled movie #{schedule['number']} "
                            f"({schedule.get('title', 'Unknown')}) will start in 10 minutes!".strip()
                        )
                    except Exception:
                        pass
                self.pre_announce_sent.add(pre_announce_key)

            due = [schedule for schedule in self.scheduled if schedule["dt"] <= now]
            for schedule in due:
                outcome = self._execute_due_schedule(
                    schedule, resolved.get(id(schedule), (None, None))[1], now
                )
                if outcome in {"started", "recovered"}:
                    await self._announce_schedule_started(schedule)
