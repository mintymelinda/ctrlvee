

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
        due = [
            s for s in self.scheduled
            if s.get("status") != "announcing"
            and (s["dt"] - now).total_seconds() <= 600
        ]
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
        if not os.path.exists(SCHEDULE_BACKUP_FILE):
            return []

        try:
            with open(SCHEDULE_BACKUP_FILE, "r") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Error loading schedule backup: {e}")
            return []

        if not isinstance(data, list):
            logger.error("Schedule backup must contain a list; ignoring invalid backup")
            return []

        schedules = []
        for index, schedule in enumerate(data):
            if not isinstance(schedule, dict):
                logger.warning("Skipping invalid schedule backup entry at index %s", index)
                continue

            try:
                scheduled_at = schedule.get("dt")
                if isinstance(scheduled_at, str):
                    scheduled_at = datetime.fromisoformat(scheduled_at)
                if not isinstance(scheduled_at, datetime):
                    raise ValueError("missing or invalid scheduled datetime")
                if scheduled_at.tzinfo is None:
                    scheduled_at = scheduled_at.replace(tzinfo=PH_TZ)
                schedule["dt"] = scheduled_at
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "Skipping schedule backup entry at index %s with invalid date: %s",
                    index, exc,
                )
                continue

            schedule.setdefault("status", "pending")
            sent_channels = schedule.get("pre_announce_sent_channels", [])
            if not isinstance(sent_channels, list):
                sent_channels = []
            schedule["pre_announce_sent_channels"] = [
                channel_id for channel_id in sent_channels
                if isinstance(channel_id, int) and not isinstance(channel_id, bool)
            ]
            started_channels = schedule.get("started_announcement_sent_channels", [])
            if not isinstance(started_channels, list):
                started_channels = []
            schedule["started_announcement_sent_channels"] = [
                channel_id for channel_id in started_channels
                if isinstance(channel_id, int) and not isinstance(channel_id, bool)
            ]
            metadata_channels = schedule.get("started_metadata_sent_channels", [])
            if not isinstance(metadata_channels, list):
                metadata_channels = []
            schedule["started_metadata_sent_channels"] = [
                channel_id for channel_id in metadata_channels
                if isinstance(channel_id, int) and not isinstance(channel_id, bool)
            ]
            try:
                attempt_count = schedule.get("attempt_count", 0)
                if isinstance(attempt_count, bool):
                    raise ValueError("boolean attempt count")
                schedule["attempt_count"] = max(0, int(attempt_count))
            except (TypeError, ValueError, OverflowError):
                logger.warning(
                    "Resetting invalid attempt count for scheduled movie '%s'",
                    schedule.get("title", "Unknown"),
                )
                schedule["attempt_count"] = 0

            retry_at = schedule.get("next_attempt_at")
            if retry_at is not None:
                try:
                    if isinstance(retry_at, str):
                        retry_at = datetime.fromisoformat(retry_at)
                    elif isinstance(retry_at, bool) or not isinstance(retry_at, (int, float)):
                        raise ValueError("unsupported retry timestamp")
                    else:
                        retry_at = datetime.fromtimestamp(retry_at, tz=PH_TZ)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=PH_TZ)
                    schedule["next_attempt_at"] = retry_at
                except (OverflowError, OSError, TypeError, ValueError):
                    logger.warning(
                        "Ignoring invalid retry timestamp for scheduled movie '%s'",
                        schedule.get("title", "Unknown"),
                    )
                    schedule.pop("next_attempt_at", None)

            started_at = schedule.get("execution_started_at")
            if started_at is not None:
                try:
                    if isinstance(started_at, str):
                        started_at = datetime.fromisoformat(started_at)
                    elif isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
                        raise ValueError("unsupported execution timestamp")
                    else:
                        started_at = datetime.fromtimestamp(started_at, tz=PH_TZ)
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=PH_TZ)
                    schedule["execution_started_at"] = started_at.isoformat()
                except (OverflowError, OSError, TypeError, ValueError):
                    if schedule.get("status") == "executing":
                        # An invalid claim timestamp is ambiguous. Start a fresh
                        # confirmation window rather than immediately replaying it.
                        schedule["execution_started_at"] = datetime.now(PH_TZ).isoformat()
                        logger.warning(
                            "Resetting invalid execution timestamp for scheduled movie '%s'",
                            schedule.get("title", "Unknown"),
                        )
                    else:
                        schedule.pop("execution_started_at", None)

            schedules.append(schedule)

        return schedules

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
            duplicate = None
            save_failed = False
            async with self._schedule_lock:
                duplicate = next(
                    (
                        scheduled for scheduled in self.scheduled
                        if scheduled["number"] == number
                        and abs((scheduled["dt"] - dt).total_seconds()) < 60
                    ),
                    None,
                )
                if duplicate is None:
                    self.scheduled.append(entry)
                    if not self._save_schedule_backup():
                        self.scheduled.remove(entry)
                        save_failed = True

            if duplicate is not None:
                await ctx.send(
                    f"❌ Movie #{number} is already scheduled at "
                    f"{duplicate['dt'].strftime('%Y-%m-%d %H:%M %Z')}. "
                    "Double scheduling is not allowed."
                )
                return
            if save_failed:
                await ctx.send("❌ Could not save the schedule. No schedule was added.")
                return

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
        async with self._schedule_lock:
            schedules = list(self.scheduled)
        if not schedules:
            await ctx.send("No movies scheduled.")
            return
        embed = discord.Embed(title="Upcoming Scheduled Movies", color=discord.Color.purple())
        for s in sorted(schedules, key=lambda x: x["dt"]):
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
        save_failed = False
        async with self._schedule_lock:
            previous = self.scheduled
            updated = [s for s in previous if s["number"] != number]
            if len(updated) == len(previous):
                removed = False
            else:
                self.scheduled = updated
                removed = True
                if not self._save_schedule_backup():
                    self.scheduled = previous
                    save_failed = True

        if save_failed:
            await ctx.send("❌ Could not save the schedule changes. No schedules were removed.")
        elif not removed:
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
        if schedule.get("status") == "announcing":
            return "announcing"

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
                schedule["status"] = "announcing"
                schedule.setdefault("started_announcement_sent_channels", [])
                schedule.setdefault("started_metadata_sent_channels", [])
                if not self._save_schedule_backup():
                    schedule["status"] = "executing"
                    return "confirming"
                logger.info("Recovered completed scheduled playback for '%s'", schedule.get("title"))
                return "announcing"

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

            # Older persisted claims may not have an execution timestamp. They
            # cannot use the confirmation window, so retry them immediately.
            schedule["status"] = "pending"
            schedule.pop("execution_started_at", None)

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
            # A timeout or dropped HTTP connection is ambiguous: VLC may have
            # executed the command before the response was lost. Keep the
            # execution claim and confirm the current item before retrying.
            schedule["last_error"] = str(error)
            self._save_schedule_backup()
            logger.warning(
                "Scheduled movie '%s' has an unconfirmed VLC command; "
                "retaining its execution claim: %s",
                schedule.get("title", "Unknown"), error,
            )
            return "confirming"

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

        schedule["status"] = "announcing"
        schedule.setdefault("started_announcement_sent_channels", [])
        schedule.setdefault("started_metadata_sent_channels", [])
        if not self._save_schedule_backup():
            schedule["status"] = "executing"
            return "confirming"
        return "announcing"

    async def _pre_announce_schedule(self, schedule):
        """Deliver the ten-minute reminder once per channel, persisting successes."""
        from src.config import Config

        channel_ids = set(Config.get_announce_channel_ids() or [])
        if schedule.get("channel"):
            channel_ids.add(schedule["channel"])
        sent_channels = set(schedule.get("pre_announce_sent_channels", []))
        role_id = getattr(Config, "WATCH_ANNOUNCE_ROLE_ID", 0)
        mention = f'<@&{role_id}>' if role_id else ''
        message = (
            f"{mention} ⏰ Reminder: Scheduled movie #{schedule['number']} "
            f"({schedule.get('title', 'Unknown')}) will start in 10 minutes!"
        ).strip()

        for channel_id in sorted(channel_ids - sent_channels):
            channel = self.bot.get_channel(channel_id)
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception:
                    continue
            if not channel:
                continue
            try:
                await channel.send(message)
            except Exception:
                continue

            sent_channels.add(channel_id)
            schedule["pre_announce_sent_channels"] = sorted(sent_channels)
            if not self._save_schedule_backup():
                logger.error(
                    "Could not persist reminder delivery for scheduled movie '%s' and channel %s",
                    schedule.get("title", "Unknown"), channel_id,
                )

        if channel_ids and channel_ids.issubset(sent_channels):
            self.pre_announce_sent.add((schedule["number"], schedule["dt"]))

    async def _announce_schedule_started(self, schedule):
        """Persist successful per-channel start announcements and retry failures."""
        from src.config import Config

        channel_ids = set(Config.get_announce_channel_ids() or [])
        if schedule.get("channel"):
            channel_ids.add(schedule["channel"])
        sent_channels = set(schedule.get("started_announcement_sent_channels", []))
        metadata_channels = set(schedule.get("started_metadata_sent_channels", []))
        role_id = getattr(Config, "WATCH_ANNOUNCE_ROLE_ID", 0)
        mention = f'<@&{role_id}>' if role_id else ''
        message = (
            f"{mention} 🎬 Scheduled movie #{schedule['number']} "
            f"({schedule.get('title', 'Unknown')}) is now playing!"
        ).strip()

        title = schedule.get("title")
        year = None
        match = re.search(r"\((\d{4})\)$", title or "")
        if match:
            year = int(match.group(1))
            title = title[:match.start()].strip()
        embed = None
        if title:
            try:
                embed = self.tmdb.get_movie_metadata(
                    title, year, file_path=schedule.get("uri")
                )
            except Exception:
                logger.exception(
                    "Could not retrieve metadata for scheduled movie '%s'",
                    schedule.get("title", "Unknown"),
                )

        for channel_id in sorted(channel_ids):
            channel = self.bot.get_channel(channel_id)
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception:
                    continue
            if not channel:
                continue

            if channel_id not in sent_channels:
                try:
                    await channel.send(message)
                except Exception:
                    continue
                sent_channels.add(channel_id)
                schedule["started_announcement_sent_channels"] = sorted(sent_channels)
                if not self._save_schedule_backup():
                    logger.error(
                        "Could not persist start announcement for scheduled movie '%s' "
                        "and channel %s",
                        schedule.get("title", "Unknown"), channel_id,
                    )

            if embed is not None and channel_id not in metadata_channels:
                try:
                    await channel.send(embed=embed)
                except Exception:
                    continue
                metadata_channels.add(channel_id)
                schedule["started_metadata_sent_channels"] = sorted(metadata_channels)
                if not self._save_schedule_backup():
                    logger.error(
                        "Could not persist metadata announcement for scheduled movie '%s' "
                        "and channel %s",
                        schedule.get("title", "Unknown"), channel_id,
                    )

        if not channel_ids.issubset(sent_channels):
            return False
        if embed is not None and not channel_ids.issubset(metadata_channels):
            return False

        self.scheduled = [entry for entry in self.scheduled if entry is not schedule]
        if not self._save_schedule_backup():
            self.scheduled.append(schedule)
            return False
        return True

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
                await self._pre_announce_schedule(schedule)

            due = [schedule for schedule in self.scheduled if schedule["dt"] <= now]
            for schedule in due:
                outcome = self._execute_due_schedule(
                    schedule, resolved.get(id(schedule), (None, None))[1], now
                )
                if outcome == "announcing":
                    await self._announce_schedule_started(schedule)
