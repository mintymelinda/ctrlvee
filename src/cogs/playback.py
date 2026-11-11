import discord
from discord.ext import commands
import asyncio
import logging
import json
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse
from ..utils.media_utils import MediaUtils
from ..config import Config
from ..utils.command_utils import format_cmd_inline

# Set up logger for this module
logger = logging.getLogger(__name__)


def _decide_playback_allow(
    room_resolved: bool,
    requester_in_room: bool,
    requester_id: int,
    owner_id: int | None,
    owner_holds_current: bool,
    owner_in_room: bool,
) -> tuple[bool, str]:
    """Pure decision rule for voice-room presence and per-track ownership."""
    if not room_resolved:
        return True, ""
    if not requester_in_room:
        return False, "not_in_room"
    if owner_id is not None and owner_id != requester_id and owner_holds_current and owner_in_room:
        return False, "hijack"
    return True, ""

def _member_has_allowed_role(member, allowed_roles) -> bool:
    """Match configured role names and IDs for interaction-based controls."""
    member_roles = getattr(member, "roles", ())
    for role in member_roles:
        role_id = str(getattr(role, "id", ""))
        role_name = getattr(role, "name", "")
        for allowed in allowed_roles:
            if isinstance(allowed, int) and role_id == str(allowed):
                return True
            if isinstance(allowed, str) and allowed.strip() in (role_name, role_id):
                return True
    return False




class PlaybackCommands(commands.Cog):
    def __init__(self, bot, vlc_controller, tmdb_service, watch_service):
        self.bot = bot
        self.vlc = vlc_controller
        self.tmdb = tmdb_service
        self.watch_service = watch_service
        self.last_state_change = {}
        self.logger = logging.getLogger(__name__)
        self.last_known_state = None
        self.last_known_position = None
        self.last_known_playing_item = None  # Track the last item that was playing
        self.monitoring_task = None
        self._presence_progress_task = None
        self._playback_panel_refresh_task = None
        self._playback_panel_messages = {}
        self._playback_panel_registry_restored = False
        self._last_command_announce_ts = 0.0
        self._suppress_auto_announce_until = 0.0
        self._last_announced_item_id = None
        self._last_announced_item_name = None
        self._command_initiated_item_id = None  # Suppress only the matching command-selected item
        # Unified announcer cooldown
        self._last_now_playing_key = None
        self._last_now_playing_ts = 0.0
        try:
            from ..config import Config
            self._np_cooldown = float(getattr(Config, 'NOW_PLAYING_COOLDOWN_SECONDS', 10.0))
            self._auto_suppress_seconds = float(getattr(Config, 'AUTO_ANNOUNCE_SUPPRESS_SECONDS', 6.0))
        except Exception:
            self._np_cooldown = 10.0
            self._auto_suppress_seconds = 6.0
        self.periodic_announce_task = None
        self.playback_started_event = asyncio.Event()
        self.last_queue_auto_play = 0  # Timestamp of last queue auto-play to prevent rapid triggers
        self._playback_owner_id = None
        self._playback_owner_item_id = None
        # Presence/update throttling for bot activity updates
        self._presence_last_set = 0.0
        self._presence_last_name = None  # Track last presence activity name to avoid throttling new titles
        # Allow a configurable throttle via Config.PRESENCE_UPDATE_THROTTLE (seconds); default to 5s
        try:
            self._presence_throttle_seconds = int(getattr(Config, 'PRESENCE_UPDATE_THROTTLE', 5))
        except Exception:
            self._presence_throttle_seconds = 5
        # Voice-channel name update throttling/state
        self._voice_status_last_set = 0.0
        self._voice_status_last_text = None
        try:
            self._voice_status_throttle_seconds = int(getattr(Config, 'VOICE_STATUS_UPDATE_THROTTLE', 15))
        except Exception:
            self._voice_status_throttle_seconds = 15
        
        # Track selected subtitle since VLC API doesn't expose it
        self.selected_subtitle_stream_index = None
        # Track selected audio stream for consistent UI when VLC selection flags are missing
        self.selected_audio_stream_index = None
        self._initial_scan_pending = True # Guard for startup presence

    def _resolve_room_channel(self, guild) -> discord.VoiceChannel | None:
        """Resolve the configured voice room for playback presence checks."""
        try:
            channel_id = int(getattr(Config, "VOICE_JOIN_CHANNEL_ID", 0) or 0)
            if channel_id <= 0:
                return None
            if guild is not None:
                channel = guild.get_channel(channel_id)
                if isinstance(channel, discord.VoiceChannel):
                    return channel
            channel = self.bot.get_channel(channel_id)
            return channel if isinstance(channel, discord.VoiceChannel) else None
        except Exception:
            return None

    async def _current_stream_item_id(self) -> str | None:
        """Return the VLC playlist ID currently playing, if any."""
        try:
            status, playlist = await self.vlc.run_async("get_playback_snapshot")
            if status is None:
                return None
            state_element = status.find("state")
            state = state_element.text if state_element is not None else None
            if state in (None, "stopped", "end"):
                return None
            if playlist is None:
                return None
            for leaf in playlist.findall(".//leaf"):
                if leaf.get("current"):
                    return leaf.get("id")
        except Exception:
            return None
        return None

    async def _playback_guard(self, member: discord.Member, presence_only: bool = False) -> tuple[bool, str]:
        """Enforce voice-room presence and prevent hijacking an active user's track."""
        auto_join_on_command = (
            bool(getattr(Config, "ENABLE_VOICE_JOIN", False))
            and bool(getattr(Config, "VOICE_AUTO_JOIN_ON_COMMAND", False))
        )
        if auto_join_on_command:
            voice = getattr(member, "voice", None)
            requester_channel = getattr(voice, "channel", None)
            if requester_channel is None:
                return False, "Join a voice channel first, then try the playback command."

            join_member_channel = getattr(self.bot, "join_member_voice_channel", None)
            if not callable(join_member_channel):
                return False, "Voice auto-join is unavailable right now. Please try again later."

            joined, join_result = await join_member_channel(member)
            if not joined:
                return False, join_result
            if not bool(getattr(Config, "ENABLE_VOICE_ROOM_RULES", True)):
                return True, ""
            room = requester_channel
            requester_in_room = True
        else:
            if not bool(getattr(Config, "ENABLE_VOICE_ROOM_RULES", True)):
                return True, ""
            room = self._resolve_room_channel(member.guild)
            if room is None and int(getattr(Config, "VOICE_JOIN_CHANNEL_ID", 0) or 0) > 0:
                return False, (
                    "Playback control is unavailable: the configured voice room can't be found. "
                    "Ask the bot owner to check `VOICE_JOIN_CHANNEL_ID`."
                )

            voice = getattr(member, "voice", None)
            requester_in_room = bool(
                voice and voice.channel and room is not None and voice.channel.id == room.id
            )
        current_id = None if presence_only else await self._current_stream_item_id()
        owner_id = None if presence_only else self._playback_owner_id
        owner = None
        owner_in_room = False
        if owner_id is not None and owner_id != member.id:
            owner = member.guild.get_member(owner_id)
            owner_voice = getattr(owner, "voice", None) if owner is not None else None
            owner_in_room = bool(
                owner_voice and owner_voice.channel and room is not None
                and owner_voice.channel.id == room.id
            )

        allowed, reason = _decide_playback_allow(
            room_resolved=room is not None,
            requester_in_room=requester_in_room,
            requester_id=member.id,
            owner_id=owner_id,
            owner_holds_current=bool(current_id and current_id == self._playback_owner_item_id),
            owner_in_room=owner_in_room,
        )
        if allowed:
            return True, ""
        if reason == "not_in_room":
            return False, f"You must be in **{room.name}** to control playback."
        owner_name = getattr(owner, "display_name", "This user") if owner else "This user"
        return False, f"⏳ **{owner_name}** is watching this one — ask before switching the playback."

    async def _check_room(self, ctx) -> bool:
        """Presence-only guard for controls that do not start a different item."""
        allowed, reason = await self._playback_guard(ctx.author, presence_only=True)
        if not allowed:
            await ctx.send(reason)
        return allowed

    def _seat_playback_owner(self, member: discord.Member, item_id: str | None) -> None:
        """Associate the currently selected VLC item with the user who started it."""
        if bool(getattr(Config, "ENABLE_VOICE_ROOM_RULES", True)):
            self._playback_owner_id = member.id
            self._playback_owner_item_id = item_id

    def _filename_from_uri(self, uri: str | None) -> str | None:
        """Extract a filename from VLC item URI/path for metadata fallback."""
        try:
            if not uri:
                return None
            raw = unquote(str(uri))
            parsed = urlparse(raw)
            path = parsed.path if parsed.scheme else raw
            if not path:
                return None
            base = os.path.basename(path)
            return base or None
        except Exception:
            return None

    def _looks_like_release_group_name(self, name: str | None) -> bool:
        """Heuristic: detect low-quality VLC names like scene group tags (e.g., IKA)."""
        try:
            if not name:
                return True
            n = str(name).strip()
            if not n:
                return True
            # If it already looks like a media filename/path, it's not a group-only token.
            if any(sep in n for sep in ['.', '/', '\\']) or n.lower().endswith(('.mkv', '.mp4', '.avi', '.mov', '.m4v', '.webm')):
                return False
            token = re.sub(r'[^A-Za-z0-9]', '', n)
            if not token:
                return True
            # Typical scene-group style short alnum token.
            if re.fullmatch(r'[A-Za-z0-9]{2,6}', token):
                return True
            # Parsed title is too short to be a meaningful media title.
            parsed_title, _ = MediaUtils.parse_movie_filename(n)
            if parsed_title and len(parsed_title.strip()) <= 4:
                return True
            return False
        except Exception:
            return False

    def _choose_metadata_source_name(self, item_name: str | None, item_uri: str | None) -> str:
        """Choose best source name for metadata parsing, preferring URI filename when needed."""
        name = (item_name or '').strip()
        uri_name = self._filename_from_uri(item_uri)

        if not name and uri_name:
            return uri_name
        if self._looks_like_release_group_name(name) and uri_name:
            return uri_name
        return name
        
    def signal_initial_scan_complete(self):
        """Signal that the initial watch folder scan is complete."""
        self.logger.info("Initial scan complete; presence clearing is now enabled.")
        self._initial_scan_pending = False

    async def cog_load(self):
        """Called when the cog is loaded"""
        # Re-register stable component IDs so existing panel messages survive restarts.
        self.bot.add_view(PlaybackControlView(self))
        self.monitoring_task = self.bot.loop.create_task(self._monitor_vlc_state())
        self.logger.info("VLC state monitoring started")
        self._playback_panel_refresh_task = self.bot.loop.create_task(
            self._playback_panel_refresh_loop()
        )
        # Schedule a one-time startup presence sync (runs after the bot is ready)
        try:
            self.bot.loop.create_task(self._startup_presence_sync())
        except Exception as e:
            self.logger.debug(f"Could not schedule startup presence sync: {e}")
        # Start periodic presence progress updater if enabled
        try:
            self._presence_progress_task = self.bot.loop.create_task(self._presence_progress_loop())
            self.logger.info("Presence progress updater started")
        except Exception as e:
            self.logger.debug(f"Could not start presence progress updater: {e}")
        # Start periodic announcement task
        try:
            self.periodic_announce_task = self.bot.loop.create_task(self._periodic_announce_loop())
            self.logger.info("Periodic announcement task started")
        except Exception as e:
            self.logger.debug(f"Could not start periodic announcement task: {e}")

    async def _startup_presence_sync(self):
        """Sync bot presence on startup if VLC is already playing/paused.

        Runs after the bot is ready to ensure presence updates are applied.
        Respects Config.ENABLE_PRESENCE and uses a brief delay to let VLC status stabilize.
        """
        try:
            if not getattr(Config, 'ENABLE_PRESENCE', True):
                return
            # Ensure the bot is fully ready before attempting presence updates
            await self.bot.wait_until_ready()
            # Small delay to allow VLC HTTP status to stabilize after connect
            await asyncio.sleep(0.4)

            status = await self.vlc.run_async("get_status")
            if status is None:
                return

            state_elem = status.find('state')
            current_state = state_elem.text if state_elem is not None else None
            if current_state not in ['playing', 'paused']:
                return

            # Try to resolve the current item's display name from playlist first
            name = None
            try:
                playlist = await self.vlc.run_async("get_playlist")
                if playlist is not None:
                    _, current_item = self._find_current_position(playlist)
                    if current_item is not None:
                        name = current_item.get('name')
            except Exception:
                name = None

            # Fallback: pull filename from status information
            if not name:
                try:
                    info_root = status.find('information')
                    if info_root is not None:
                        for category in info_root.findall('category'):
                            for info in category.findall('info'):
                                if info.get('name') == 'filename':
                                    name = info.text
                                    break
                            if name:
                                break
                except Exception:
                    name = None

            if name:
                try:
                    await self._set_presence(name, reason="startup sync")
                    await self._set_voice_channel_status(name, state=current_state, reason="startup sync")
                    self.logger.info(f"Startup presence set: {name}")
                except Exception as e:
                    self.logger.debug(f"Failed to set startup presence: {e}")
        except Exception as e:
            self.logger.debug(f"Startup presence sync skipped: {e}")

    def _format_voice_status_text(self, title: str | None, state: str | None = None) -> str | None:
        """Build a voice channel status text from current media title and state."""
        try:
            if title:
                clean = MediaUtils.clean_filename_for_display(title).strip()
                if not clean:
                    clean = str(title).strip()
                if not clean:
                    return None
                prefix = str(getattr(Config, 'VOICE_STATUS_PREFIX', 'Now Playing: '))
                if prefix and not prefix.endswith((' ', '-', '•', '|')):
                    prefix = f"{prefix} "
                label = f"{prefix}{clean}"
                if state == 'paused' and bool(getattr(Config, 'VOICE_STATUS_SHOW_PAUSED', True)):
                    label = f"{label} [Paused]"
                return label[:120]

            idle_name = str(getattr(Config, 'VOICE_STATUS_IDLE_NAME', '')).strip()
            if idle_name:
                return idle_name[:120]
            return None
        except Exception:
            return None

    async def _resolve_voice_status_channel(self) -> discord.VoiceChannel | None:
        """Resolve the voice channel to rename for status updates."""
        try:
            if not bool(getattr(Config, 'ENABLE_VOICE_CHANNEL_STATUS', False)):
                return None

            channel_id = int(getattr(Config, 'VOICE_STATUS_CHANNEL_ID', 0) or 0)
            if channel_id <= 0:
                channel_id = int(getattr(Config, 'VOICE_JOIN_CHANNEL_ID', 0) or 0)
            if channel_id <= 0:
                return None

            ch = self.bot.get_channel(channel_id)
            if not ch:
                try:
                    ch = await self.bot.fetch_channel(channel_id)
                except Exception:
                    return None
            if not isinstance(ch, discord.VoiceChannel):
                return None
            return ch
        except Exception:
            return None

    async def _set_voice_channel_status(self, title: str | None, state: str | None = None, reason: str | None = None):
        """Update voice channel status text to reflect currently playing media."""
        try:
            if not bool(getattr(Config, 'ENABLE_VOICE_CHANNEL_STATUS', False)):
                return

            channel = await self._resolve_voice_status_channel()
            if not channel:
                return

            target_text = self._format_voice_status_text(title, state=state)
            if target_text is not None and getattr(channel, 'status', None) == target_text:
                self._voice_status_last_text = target_text
                return
            if target_text is None and getattr(channel, 'status', None) in (None, ''):
                self._voice_status_last_text = None
                return

            now = asyncio.get_event_loop().time()
            if (now - self._voice_status_last_set) < self._voice_status_throttle_seconds:
                if self._voice_status_last_text == target_text:
                    return

            me = channel.guild.me
            if me is None or not channel.permissions_for(me).manage_channels:
                self.logger.debug("Skipping voice status update: missing Manage Channels permission")
                return

            await channel.edit(status=target_text, reason=(reason or "Update voice status"))
            self._voice_status_last_set = now
            self._voice_status_last_text = target_text
            self.logger.info(f"Voice channel status updated to: {target_text if target_text else '<cleared>'}")
        except TypeError as e:
            self.logger.warning(f"Voice status update is not supported by this discord.py version: {e}")
        except discord.Forbidden:
            self.logger.warning("Cannot update voice channel status: missing permissions")
        except Exception as e:
            self.logger.debug(f"Voice channel status update failed: {e}")

    async def _announce_now_playing(
        self,
        origin: str,
        item: ET.Element | None,
        position: int | None,
        expected_state: str | None = None,
    ):
        """Unified Now Playing announcer with cooldown and de-duplication.

        origin: 'command' | 'monitor' | 'periodic'
        item: current VLC playlist leaf element
        position: 1-based playlist position if known
        """
        try:
            if item is None:
                return
            name = item.get('name') or ''
            uri = item.get('uri')
            metadata_name = self._choose_metadata_source_name(name, uri)
            display_name = metadata_name or name
            if not display_name:
                return
            # Build a de-duplication key from cleaned name and position
            key = f"{MediaUtils.clean_filename_for_display(display_name)}|{position or ''}"
            now_ts = asyncio.get_event_loop().time()
            # Cooldown: avoid re-announcing the same item too frequently
            if self._last_now_playing_key == key and (now_ts - self._last_now_playing_ts) < self._np_cooldown:
                return
            # Prepare TMDB embed if possible
            tmdb_embed = None
            edition_tag = None
            is_movie_embed = False
            has_explicit_episode = False
            episode_label = None
            try:
                title, year = MediaUtils.parse_movie_filename(metadata_name)
                tv_title, tv_season, tv_episode, tv_year = MediaUtils.parse_tv_filename(metadata_name)
                edition_tag = MediaUtils.extract_edition_tag(metadata_name)
                if tv_season and tv_episode:
                    episode_label = f"S{int(tv_season):02d}E{int(tv_episode):02d}"
                
                # Detect if there's an explicit episode marker
                has_explicit_episode = bool(tv_episode) or bool(re.search(r"(?i)(s\d{1,2}e\d{1,2}|\d{1,2}x\d{1,2})", metadata_name))
                
                if has_explicit_episode and tv_title:
                    # Has explicit episode: prefer TV
                    tmdb_embed = self.tmdb.get_tv_metadata(tv_title, tv_season, tv_year)
                else:
                    # No explicit episode: try both and compare scores
                    movie_embed = None
                    tv_embed = None
                    movie_score = 0.0
                    tv_score = 0.0
                    
                    if title:
                        movie_embed = self.tmdb.get_movie_metadata(title, year, file_path=uri)
                        movie_score = getattr(self.tmdb, '_last_match_score', 0.0)
                    
                    if tv_title:
                        tv_embed = self.tmdb.get_tv_metadata(tv_title, tv_season, tv_year)
                        tv_score = getattr(self.tmdb, '_last_match_score', 0.0)
                    
                    # Choose the better match
                    if tv_embed and movie_embed:
                        tmdb_embed = tv_embed if tv_score > movie_score else movie_embed
                        is_movie_embed = (tmdb_embed == movie_embed)
                    elif tv_embed:
                        tmdb_embed = tv_embed
                    elif movie_embed:
                        tmdb_embed = movie_embed
                        is_movie_embed = True
                    else:
                        # Fallback: try generic TV lookup
                        if title:
                            tmdb_embed = self.tmdb.get_tv_metadata(title)
            except Exception:
                tmdb_embed = None
            if tmdb_embed:
                final = tmdb_embed
                final.title = f"Now Playing: {final.title}"
                if edition_tag and is_movie_embed:
                    try:
                        final.add_field(name="Edition", value=edition_tag, inline=True)
                    except Exception:
                        pass
                if episode_label and not is_movie_embed:
                    try:
                        final.add_field(name="Episode", value=episode_label, inline=True)
                    except Exception:
                        pass
            else:
                final = discord.Embed(title=f"Now Playing: {display_name}", color=discord.Color.blue())
                if edition_tag and not has_explicit_episode:
                    try:
                        final.add_field(name="Edition", value=edition_tag, inline=True)
                    except Exception:
                        pass
                if episode_label:
                    try:
                        final.add_field(name="Episode", value=episode_label, inline=True)
                    except Exception:
                        pass
            # Add position if available
            if position:
                try:
                    final.add_field(name="Playlist", value=f"#{position}", inline=True)
                except Exception:
                    pass
            # Update presence. Monitor-origin announcements must revalidate after
            # each Discord await so an older snapshot cannot publish over a newer
            # manual selection.
            try:
                await self._set_presence(display_name, reason=f"now playing ({origin})")
                if expected_state is not None and not await self._monitor_snapshot_is_current(
                    item.get("id"), expected_state
                ):
                    return
                await self._set_voice_channel_status(
                    display_name, state=expected_state or 'playing', reason=f"now playing ({origin})"
                )
            except Exception:
                pass
            if expected_state is not None and not await self._monitor_snapshot_is_current(
                item.get("id"), expected_state
            ):
                return
            # Send to announce channels
            channel_ids = Config.get_announce_channel_ids()
            for channel_id in channel_ids or []:
                try:
                    channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                    if channel:
                        await channel.send(embed=final)
                        self.logger.info(f"Sent unified Now Playing ({origin}) to channel {channel_id}")
                except Exception as e:
                    self.logger.error(f"Failed to send unified Now Playing to channel {channel_id}: {e}")
            # Record last
            self._last_now_playing_key = key
            self._last_now_playing_ts = now_ts
            # Mark command suppression states
            try:
                self._last_command_announce_ts = now_ts if origin == 'command' else self._last_command_announce_ts
                if origin == 'command':
                    self._suppress_auto_announce_until = now_ts + self._auto_suppress_seconds
                    self._last_announced_item_id = item.get('id')
                    self._last_announced_item_name = name
            except Exception:
                pass
        except Exception as e:
            self.logger.error(f"_announce_now_playing error: {e}")
        
    async def cog_unload(self):
        """Called when the cog is unloaded"""
        if self.monitoring_task:
            self.monitoring_task.cancel()
            self.logger.info("VLC state monitoring stopped")
        if self._presence_progress_task:
            self._presence_progress_task.cancel()
            self.logger.info("Presence progress updater stopped")
        if self._playback_panel_refresh_task:
            self._playback_panel_refresh_task.cancel()
            self.logger.info("Playback panel refresh updater stopped")
        if self.periodic_announce_task:
            self.periodic_announce_task.cancel()
            self.logger.info("Periodic announcement task stopped")

    @commands.command(name='cleanup', aliases=['plcleanup','cleanup_missing'])
    async def cleanup_missing(self, ctx: commands.Context):
        """Remove missing/unavailable files from the VLC playlist.

        Scans the current VLC playlist for items whose underlying files no longer exist
        (e.g., replaced/upgraded files) and removes them. Shows a summary of removed items.
        
        Works with both local and network share files on all platforms (Windows, Linux, macOS).
        """
        try:
            await ctx.trigger_typing()
        except Exception:
            pass
        try:
            result = await self.vlc.run_async("remove_missing_playlist_items")
            removed = int(result.get('removed', 0))
            items = result.get('items', []) or []
            if removed == 0:
                await ctx.send("✅ Playlist cleanup: no missing files detected.")
                return
            max_list = 10
            listed = items[:max_list]
            more = removed - len(listed)
            lines = []
            for it in listed:
                nm = it.get('name') or '<unknown>'
                lines.append(f"• {MediaUtils.clean_filename_for_display(nm)}")
            if more > 0:
                lines.append(f"… and {more} more")
            embed = discord.Embed(
                title="🧹 Playlist Cleanup",
                description=f"Removed {removed} missing file(s) from the playlist:\n\n" + "\n".join(lines),
                color=discord.Color.orange()
            )
            try:
                embed.set_footer(text="Cleanup tool")
            except Exception:
                pass
            await ctx.send(embed=embed)
        except Exception as e:
            self.logger.error(f"cleanup_missing error: {e}")
            await ctx.send(f"❌ Cleanup failed: {e}")

    async def _periodic_announce_loop(self):
        """Periodically announce the currently playing media, triggered by playback start."""
        try:
            await self.bot.wait_until_ready()
        except Exception:
            return

        while not self.bot.is_closed():
            try:
                # Wait until playback starts
                self.logger.info("Periodic announcer is waiting for playback to start...")
                await self.playback_started_event.wait()
                self.logger.info("Playback started, periodic announcer is active.")

                interval = int(getattr(Config, 'PERIODIC_ANNOUNCE_INTERVAL', 300))

                # Wait for the initial interval before the first announcement
                await asyncio.sleep(interval)

                # Keep announcing as long as playback is active
                while self.playback_started_event.is_set():
                    if not getattr(Config, 'PERIODIC_ANNOUNCE_ENABLED', False):
                        self.logger.debug("Periodic announcement disabled in config, pausing until re-enabled.")
                        # If disabled, wait until it might be re-enabled without spamming checks
                        await asyncio.sleep(interval)
                        continue

                    channel_ids = Config.get_announce_channel_ids()
                    if not channel_ids:
                        self.logger.debug("Periodic announcement skipped: No announcement channels configured.")
                        await asyncio.sleep(interval)
                        continue
                    
                    status = await self.vlc.run_async("get_status")
                    if status and status.find('state').text == 'playing':
                        self.logger.info("VLC is playing, preparing periodic announcement...")
                        embed = await self.get_status_embed()
                        if embed:
                            for channel_id in channel_ids:
                                try:
                                    channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                                    if channel:
                                        await channel.send(embed=embed)
                                        self.logger.info(f"Sent periodic 'Now Playing' announcement to channel {channel_id}")
                                except Exception as e:
                                    self.logger.error(f"Failed to send periodic announcement to channel {channel_id}: {e}")
                        else:
                            self.logger.debug("Periodic announcement skipped: could not generate status embed.")
                    else:
                        self.logger.debug("Periodic announcement skipped: VLC not in 'playing' state.")

                    # Wait for the next interval
                    await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break # Exit loop cleanly on cancellation
            except Exception as e:
                self.logger.error(f"Error in periodic announcement loop: {e}")
                # If a major error occurs, wait a bit before restarting the wait
                await asyncio.sleep(30)

    async def _presence_progress_loop(self):
        """Periodically update presence with playback progress (mm:ss/MM:SS)."""
        try:
            await self.bot.wait_until_ready()
        except Exception:
            return
        interval = 0
        try:
            interval = max(5, int(getattr(Config, 'PRESENCE_PROGRESS_UPDATE_INTERVAL', 30)))
        except Exception:
            interval = 30
        while not self.bot.is_closed():
            try:
                # Respect config toggles
                if not getattr(Config, 'ENABLE_PRESENCE', True) or not getattr(Config, 'ENABLE_PRESENCE_PROGRESS', True):
                    await asyncio.sleep(interval)
                    continue

                status = None
                try:
                    status = await self.vlc.run_async("get_status")
                except Exception:
                    status = None

                if status is None:
                    await asyncio.sleep(interval)
                    continue

                state_elem = status.find('state')
                current_state = state_elem.text if state_elem is not None else None
                # Only update progress while playing or paused
                if current_state not in ['playing', 'paused']:
                    await asyncio.sleep(interval)
                    continue

                # Resolve current title
                title = None
                try:
                    playlist = await self.vlc.run_async("get_playlist")
                    if playlist is not None:
                        _, current_item = self._find_current_position(playlist)
                        if current_item is not None:
                            title = current_item.get('name')
                except Exception:
                    title = None
                if not title:
                    try:
                        info_root = status.find('information')
                        if info_root is not None:
                            for category in info_root.findall('category'):
                                for info in category.findall('info'):
                                    if info.get('name') == 'filename':
                                        title = info.text
                                        break
                                if title:
                                    break
                    except Exception:
                        title = None

                if not title:
                    await asyncio.sleep(interval)
                    continue

                # Compute progress string
                progress_suffix = None
                try:
                    time_elem = status.find('time')
                    length_elem = status.find('length')
                    if time_elem is not None and length_elem is not None and time_elem.text and length_elem.text:
                        cur = int(time_elem.text)
                        total = int(length_elem.text)
                        if total > 0 and cur >= 0:
                            def fmt(n: int) -> str:
                                return f"{n//60}:{n%60:02d}"
                            progress_suffix = f"{fmt(cur)}/{fmt(total)}"
                except Exception:
                    progress_suffix = None

                name_for_presence = title
                if progress_suffix:
                    # Trim to keep final presence text within safe limits (~120 chars)
                    base = title
                    try:
                        suffix = f" — {progress_suffix}"
                        max_total = 120
                        max_base = max_total - len(suffix)
                        if len(base) > max_base:
                            base = base[: max(0, max_base - 3)] + "..."
                        name_for_presence = base + suffix
                    except Exception:
                        name_for_presence = f"{title} — {progress_suffix}"

                # Mark paused state in reason for clarity (no change to visible text)
                reason = "progress tick (paused)" if current_state == 'paused' else "progress tick"
                try:
                    await self._set_presence(name_for_presence, reason=reason)
                except Exception:
                    pass

            except Exception as e:
                logger.debug(f"Presence progress loop error: {e}")
            finally:
                try:
                    await asyncio.sleep(interval)
                except Exception:
                    pass
        
    def _find_current_position(self, playlist):
        """Find the position of the current item in the playlist
        
        Args:
            playlist: The XML playlist from VLC
            
        Returns:
            tuple: (position, current_item) where position is 1-based index or None if not found
                  and current_item is the XML element or None if not found
        """
        if playlist is None:
            return None, None
            
        current_item = None
        position = None
        
        # Find current item and its position
        for i, item in enumerate(playlist.findall('.//leaf')):
            if item.get('current'):
                current_item = item
                position = i + 1  # Convert to 1-based index
                break
                
        return position, current_item

    async def _check_cooldown(self, ctx):
        """Check if enough time has passed since last state change"""
        guild_id = str(ctx.guild.id) if ctx.guild else 'dm'
        current_time = asyncio.get_event_loop().time()
        
        if guild_id in self.last_state_change:
            time_since_last = current_time - self.last_state_change[guild_id]
            if time_since_last < 1:
                logger.debug(f"Command ignored - too soon after last state change ({time_since_last:.2f}s)")
                return False
        return True
    
    def _check_queue_auto_play_cooldown(self):
        """Check if enough time has passed since last queue auto-play to prevent rapid triggers"""
        current_time = asyncio.get_event_loop().time()
        time_since_last = current_time - self.last_queue_auto_play
        
        if time_since_last < 2.0:  # 2 second cooldown between queue auto-plays
            return False
        
        self.last_queue_auto_play = current_time
        return True
        
    @commands.command(name='speed', aliases=['spd', 'speed15', 'speednorm'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def speed(self, ctx, target: str = None):
        """Set playback speed. Usage examples: set a numeric rate (e.g. 1.5) or use a preset like 'normal'.

        Short aliases are available (e.g. `speed15`, `speednorm`).
        The playback rate will also be reset automatically when a file finishes playing.
        """
        try:
            # No target provided -> show usage embed
            if target is None:
                embed = discord.Embed(
                    title="Playback Speed — Usage",
                    description=(
                        f"Set the playback rate using a numeric value, or use a preset like 'normal' to reset.\n\n"
                        f"Examples: {format_cmd_inline('speed 1.5')} or {format_cmd_inline('speed normal')}"
                    ),
                    color=discord.Color.blue()
                )
                embed.add_field(name="Aliases", value="spd, speed15, speednorm", inline=True)
                await ctx.send(embed=embed)
                return

            if not await self._check_room(ctx):
                return

            t = target.strip().lower()
            if t in ('1.5', '1.5x', '15', 'fast', 'up'):
                rate = 1.5
            elif t in ('1', '1.0', 'normal', 'default', 'reset', 'norm'):
                rate = 1.0
            else:
                # Try to parse a float
                try:
                    rate = float(t.rstrip('x'))
                except Exception:
                    embed = discord.Embed(
                        title="Invalid speed",
                        description="Please provide a numeric rate like `1.5` or use `normal` to reset.",
                        color=discord.Color.red()
                    )
                    embed.set_footer(text=f"Usage: {format_cmd_inline('speed 1.5')}")
                    await ctx.send(embed=embed)
                    return

            ok = False
            try:
                ok = await self.vlc.run_async("set_rate", rate)
            except Exception as e:
                logger.error(f"Error setting playback rate: {e}")

            if ok:
                if rate == 1.0:
                    embed = discord.Embed(
                        title="Playback Speed Reset",
                        description="✅ Playback speed reset to normal (1.0x)",
                        color=discord.Color.green()
                    )
                else:
                    embed = discord.Embed(
                        title="Playback Speed Updated",
                        description=f"✅ Playback speed set to {rate}x",
                        color=discord.Color.green()
                    )
                # Add Ko-fi support field when configured
                try:
                    if Config.KOFI_URL:
                        embed.add_field(name="Support kutsaratinidor by supporting CtrlVee", value=f"☕ {f'<{Config.KOFI_URL}>'}", inline=False)
                except Exception:
                    pass
                await ctx.send(embed=embed)
            else:
                embed = discord.Embed(
                    title="Playback Speed Failed",
                    description=f"⚠️ Failed to set playback speed to {rate}x",
                    color=discord.Color.orange()
                )
                await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"Speed command error: {e}")
            embed = discord.Embed(
                title="Error",
                description=f"An error occurred while setting playback speed: {e}",
                color=discord.Color.dark_red()
            )
            await ctx.send(embed=embed)
        
    async def _monitor_snapshot_is_current(
        self, expected_item_id: str | None, expected_state: str
    ) -> bool:
        """Confirm a monitor snapshot still owns playback before publishing it."""
        if expected_item_id is None:
            return False
        try:
            confirmation = await self.vlc.run_async(
                "get_current_item_if_unchanged", expected_item_id
            )
            return bool(
                confirmation.get("success")
                and confirmation.get("state") == expected_state
            )
        except Exception:
            return False

    async def _monitor_vlc_state(self):
        """Background task to monitor VLC state changes"""
        await self.bot.wait_until_ready()
        try:
            restored_queue = await self.vlc.run_async("reconcile_queue_with_playlist")
            if restored_queue.get("success"):
                removed_ids = restored_queue.get("removed_item_ids", [])
                if removed_ids:
                    self.logger.info(
                        "Removed %s stale soft-queue item(s) during startup: %s",
                        len(removed_ids),
                        ", ".join(removed_ids),
                    )
                else:
                    self.logger.info(
                        "Restored %s soft-queue item(s) from persistent backup",
                        restored_queue.get("remaining", 0),
                    )
            else:
                self.logger.warning(
                    "Soft queue startup reconciliation deferred: %s",
                    restored_queue.get("reason", "unknown error"),
                )
        except Exception:
            self.logger.exception("Soft queue startup reconciliation failed; saved queue was retained")

        while not self.bot.is_closed():
            try:
                status, playlist = await self.vlc.run_async("get_playback_snapshot")
                if status:
                    current_state = status.find('state').text
                    # If VLC is stopped, clear the bot's presence (throttled)
                    # BUT: do not clear it if we are still waiting for the initial scan to complete
                    try:
                        if current_state == 'stopped':
                            if not self._initial_scan_pending:
                                await self._set_presence(None, reason="stopped")
                                await self._set_voice_channel_status(None, state='stopped', reason="stopped")
                            # Signal that playback has stopped
                            if self.playback_started_event.is_set():
                                self.logger.info("Playback stopped, deactivating periodic announcer.")
                                self.playback_started_event.clear()
                        elif current_state == 'playing':
                            # Signal that playback has started
                            if not self.playback_started_event.is_set():
                                self.playback_started_event.set()
                    except Exception:
                        # Non-fatal: presence/event update failures should not stop monitoring
                        pass
                    
                    # Get current position and item from playlist
                    current_position = None
                    current_item = None
                    if playlist is not None:
                        current_position, current_item = self._find_current_position(playlist)
                    
                    # Check for state changes
                    if self.last_known_state is not None:
                        state_changed = current_state != self.last_known_state
                        previous_item_id = (
                            self.last_known_playing_item.get("id")
                            if self.last_known_playing_item is not None
                            else None
                        )
                        current_item_id = current_item.get("id") if current_item is not None else None
                        position_changed = current_item_id != previous_item_id
                        
                        # Handle queue transitions when track changes OR when state changes to stopped/paused
                        if (position_changed and current_item is not None) or (state_changed and current_state in ['stopped', 'paused']):
                            current_item_id = current_item.get('id') if current_item else None
                            
                            # Priority 1: Handle position changes (track transitions)
                            if position_changed and current_item_id:
                                # Check if there's a queue and this is a natural track progression
                                next_queued = await self.vlc.run_async("get_next_queued_item")
                                if next_queued:
                                    # There's a queued item - check if the current track is NOT the queued item
                                    if current_item_id != next_queued['item_id']:
                                        if self._check_queue_auto_play_cooldown():
                                            logger.info(f"Track changed to {current_item_id} but we have queued item {next_queued['item_id']} - interrupting to play queued item")
                                            try:
                                                play_result = await self.vlc.run_async("play_queued_item_if_monitor_state", current_state, expected_current_item_id=current_item_id)
                                                logger.info(f"Auto-play result: {play_result}")
                                                
                                                if play_result.get("success"):
                                                    logger.info(f"Auto-played next queued item: {play_result.get('item_name', 'Unknown')}")
                                                    
                                                    # Optionally notify in Discord if notification channel is set
                                                    channel_id = getattr(Config, 'WATCH_ANNOUNCE_CHANNEL_ID', 0)
                                                    if channel_id:
                                                        try:
                                                            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                                                            if channel:
                                                                await channel.send(f"Auto-playing next queued item: **{play_result.get('item_name', 'Unknown')}**")
                                                        except Exception as e:
                                                            logger.error(f"Failed to send auto-play notification: {e}")
                                                        
                                                else:
                                                    logger.warning(f"Auto-play failed: {play_result.get('error', 'Unknown error')}")
                                            except Exception as e:
                                                logger.error(f"Error auto-playing next queued item: {e}")
                            
                            # Priority 2: For state changes to stopped OR paused, check if we should auto-play next queued item
                            # (movies often go to paused state when they end, not stopped)
                            elif state_changed and current_state in ['stopped', 'paused'] and await self.vlc.run_async("get_next_queued_item"):
                                if self._check_queue_auto_play_cooldown():
                                    try:
                                        logger.info(f"Track {current_state} - checking for next queued item to auto-play")
                                        next_queued = await self.vlc.run_async("get_next_queued_item")
                                        logger.info(f"Next queued item found: {next_queued}")
                                        
                                        # Additional check: if paused, make sure we're actually at the end
                                        should_auto_play = True
                                        if current_state == 'paused':
                                            try:
                                                status = await self.vlc.run_async("get_status")
                                                if status is not None:
                                                    time_elem = status.find('time')
                                                    length_elem = status.find('length')
                                                    if time_elem is not None and length_elem is not None:
                                                        current_time = int(time_elem.text)
                                                        total_length = int(length_elem.text)
                                                        # Only auto-play if we're within 3 seconds of the end
                                                        if total_length > 0 and (total_length - current_time) > 3:
                                                            should_auto_play = False
                                                            logger.debug(f"Paused but not at end: {current_time}/{total_length}s - not auto-playing")
                                            except Exception as e:
                                                logger.debug(f"Could not check time position: {e}")
                                        
                                        if should_auto_play:
                                            play_result = await self.vlc.run_async("play_queued_item_if_monitor_state", current_state, expected_current_item_id=current_item_id, require_end=(current_state == "paused"))
                                            logger.info(f"Auto-play result: {play_result}")
                                            
                                            if play_result.get("success"):
                                                logger.info(f"Auto-played next queued item: {play_result.get('item_name', 'Unknown')}")
                                                
                                                # Optionally notify in Discord if notification channel is set
                                                channel_id = getattr(Config, 'WATCH_ANNOUNCE_CHANNEL_ID', 0)
                                                if channel_id:
                                                    try:
                                                        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                                                        if channel:
                                                            await channel.send(f"Auto-playing next queued item: **{play_result.get('item_name', 'Unknown')}**")
                                                    except Exception as e:
                                                        logger.error(f"Failed to send auto-play notification: {e}")
                                                # Update presence to show the newly playing queued item
                                                try:
                                                    await self._set_presence(play_result.get('item_name'), reason="auto-queue (end detection)")
                                                    await self._set_voice_channel_status(play_result.get('item_name'), state='playing', reason="auto-queue (end detection)")
                                                except Exception:
                                                    pass
                                            else:
                                                logger.warning(f"Auto-play failed: {play_result.get('error', 'Unknown error')}")
                                            
                                    except Exception as e:
                                        logger.error(f"Error auto-playing next queued item: {e}")
                            
                            # Apply queue bookkeeping only if this monitor snapshot is
                            # still current. This also completes the previous queued item
                            # under the same controller lock as the identity check.
                            if position_changed and current_item_id:
                                try:
                                    last_item_id = (
                                        self.last_known_playing_item.get("id")
                                        if self.last_known_playing_item is not None
                                        else None
                                    )
                                    queue_result = await self.vlc.run_async(
                                        "check_and_handle_queue_transition",
                                        current_item_id,
                                        expected_state=current_state,
                                        finished_item_id=last_item_id,
                                    )
                                    if queue_result.get("stale"):
                                        logger.debug(
                                            "Discarding stale monitor transition (%s); "
                                            "VLC now reports item %s",
                                            queue_result.get("reason"),
                                            queue_result.get("current_item_id"),
                                        )
                                        await asyncio.sleep(0.5)
                                        continue
                                except Exception as e:
                                    logger.error(f"Error handling queue transition: {e}")

                        if state_changed or position_changed:
                            # The queue transition check may have been followed by a
                            # manual command. Reconfirm immediately before publishing
                            # presence or announcements from this monitor snapshot.
                            if current_item_id and not await self._monitor_snapshot_is_current(
                                current_item_id, current_state
                            ):
                                await asyncio.sleep(0.5)
                                continue

                            # Get item name if available
                            item_name = None
                            if current_item is not None:
                                item_name = current_item.get('name')
                                
                            # Log the change regardless of notification channel
                            if state_changed:
                                logger.info(f"VLC state changed to: {current_state}")
                            elif position_changed:
                                logger.info(f"Track changed to: {item_name or 'Unknown'} #{current_position if current_position else 'N/A'}")

                            # Update presence on normal track transitions (no queue intervention)
                            try:
                                if position_changed and item_name:
                                    await self._set_presence(item_name, reason="track change")
                                    if current_item_id and not await self._monitor_snapshot_is_current(
                                        current_item_id, current_state
                                    ):
                                        await asyncio.sleep(0.5)
                                        continue
                                    await self._set_voice_channel_status(
                                        item_name, state=current_state, reason="track change"
                                    )
                                    if current_item_id and not await self._monitor_snapshot_is_current(
                                        current_item_id, current_state
                                    ):
                                        await asyncio.sleep(0.5)
                                        continue
                            except Exception:
                                pass
                            
                            # Only send Discord message if a notification channel is configured
                            channel_ids = Config.get_announce_channel_ids()
                            now_ts = asyncio.get_event_loop().time()
                            # Suppress only the item selected by the command. If a
                            # different item won the race, let that real transition announce.
                            if position_changed and self._command_initiated_item_id is not None:
                                expected_item_id = self._command_initiated_item_id
                                self._command_initiated_item_id = None
                                if str(current_item_id) == str(expected_item_id):
                                    self.logger.debug(
                                        "Auto announce suppressed for command-selected item %s",
                                        expected_item_id,
                                    )
                                    self.last_known_state = current_state
                                    self.last_known_position = current_position
                                    self.last_known_playing_item = current_item
                                    continue
                            # Hard suppression: if we just sent a command-driven Now Playing, skip auto announce entirely (short window)
                            if position_changed and (now_ts - self._last_command_announce_ts) < self._auto_suppress_seconds:
                                self.logger.debug("Auto announce suppressed: recent command-driven Now Playing")
                            # Note: do not suppress by ID/name to allow manual selection announcements
                            elif channel_ids and (now_ts - self._last_command_announce_ts) > 1 and now_ts >= self._suppress_auto_announce_until:
                                # Use unified announcer
                                await self._announce_now_playing(
                                    'monitor', current_item, current_position,
                                    expected_state=current_state,
                                )
                            elif not channel_ids:
                                self.logger.debug("Track change announcement skipped: No announcement channels configured.")
                            else:
                                self.logger.debug("Track change announcement skipped: Debounced.")
                    
                    # Update last known state
                    self.last_known_state = current_state
                    self.last_known_position = current_position
                    self.last_known_playing_item = current_item  # Track the current playing item
                    
                    # Priority 3: End-of-track detection - check if current track is about to end
                    if current_state in ['playing', 'paused'] and await self.vlc.run_async("get_next_queued_item"):
                        # This check is to see if we are near the end of the media.
                        # If we are, we can be more aggressive about checking for the next item.
                        # This helps in cases where the state change to 'stopped' is delayed.
                        try:
                            status = await self.vlc.run_async("get_status")
                            if status is not None:
                                time_elem = status.find('time')
                                length_elem = status.find('length')
                                if time_elem is not None and length_elem is not None:
                                    current_time = int(time_elem.text)
                                    total_time = int(length_elem.text)
                                    # If within 3 seconds of the end, we might want to act.
                                    if total_time > 0 and (total_time - current_time) < 3:
                                        if self._check_queue_auto_play_cooldown():
                                            logger.info("Track is near the end, preparing to auto-play next queued item.")
                                            # This path is tricky because we might preemptively switch.
                                            # For now, we just log. The main 'stopped'/'paused' handler will do the work.
                        except Exception as e:
                            logger.debug(f"Error in end-of-track detection: {e}")

                    # If VLC pauses at the very end of a track (common behavior) and there's no queued item,
                    # clear presence to avoid showing a stale title
                    try:
                        if current_state == 'paused' and not await self.vlc.run_async("get_next_queued_item"):
                            status = await self.vlc.run_async("get_status")
                            if status is not None:
                                time_elem = status.find('time')
                                length_elem = status.find('length')
                                if time_elem is not None and length_elem is not None:
                                    current_time = int(time_elem.text)
                                    total_length = int(length_elem.text)
                                    if total_length > 0 and (total_length - current_time) <= 3:
                                        # Near end while paused and nothing queued -> clear presence
                                        await self._set_presence(None, reason="paused at end")
                                        logger.info("Cleared presence: VLC paused at track end and no queued items")
                    except Exception as e:
                        logger.debug(f"Paused-end presence clear check failed: {e}")
                    
                    # Enhanced periodic check: If we have queued items, ensure they get played
                    next_queued = await self.vlc.run_async("get_next_queued_item")
                    if next_queued:
                        # Case 1: VLC is stopped and we have queued items
                        if current_state == 'stopped':
                            # Reset playback rate when VLC has stopped (file finished)
                            try:
                                await self.vlc.run_async("set_rate", 1.0)
                            except Exception:
                                pass
                            if self._check_queue_auto_play_cooldown():
                                try:
                                    play_result = await self.vlc.run_async("play_queued_item_if_monitor_state", "stopped")
                                    logger.info(f"Auto-play result from stopped state: {play_result}")
                                    if play_result.get("success"):
                                        logger.info(f"Auto-played next queued item: {play_result.get('item_name', 'Unknown')}")
                                        # Optionally notify
                                        channel_id = getattr(Config, 'WATCH_ANNOUNCE_CHANNEL_ID', 0)
                                        if channel_id:
                                            try:
                                                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                                                if channel:
                                                    await channel.send(f"Auto-playing next queued item: **{play_result.get('item_name', 'Unknown')}**")
                                            except Exception as e:
                                                logger.error(f"Failed to send auto-play notification: {e}")
                                        # Update presence
                                        try:
                                            await self._set_presence(play_result.get('item_name'), reason="auto-queue (stopped)")
                                            await self._set_voice_channel_status(play_result.get('item_name'), state='playing', reason="auto-queue (stopped)")
                                        except Exception:
                                            pass
                                    else:
                                        logger.warning(f"Auto-play from stopped state failed: {play_result.get('error', 'Unknown error')}")
                                except Exception as e:
                                    logger.error(f"Error auto-playing from stopped state: {e}")
                        
                        # Case 2: VLC is playing but wrong item (queue was bypassed)
                        elif current_state == 'playing' and current_item:
                            current_item_id = current_item.get('id')
                            if current_item_id != next_queued['item_id']:
                                if self._check_queue_auto_play_cooldown():
                                    try:
                                        logger.info(f"Periodic check: Wrong item playing ({current_item_id}), should be queued item ({next_queued['item_id']}) - correcting")
                                        play_result = await self.vlc.run_async("play_queued_item_if_monitor_state", "playing", expected_current_item_id=current_item_id)
                                        
                                        if play_result.get("success"):
                                            logger.info(f"Periodic correction successful: {play_result.get('item_name', 'Unknown')}")
                                            try:
                                                await self._set_presence(play_result.get('item_name'), reason="periodic correction (wrong item)")
                                                await self._set_voice_channel_status(play_result.get('item_name'), state='playing', reason="periodic correction (wrong item)")
                                            except Exception:
                                                pass
                                    except Exception as e:
                                        logger.error(f"Error in periodic queue correction: {e}")
                    
            except Exception as e:
                logger.error(f"Error in VLC monitoring task: {e}")
            
            # Wait before next check
            await asyncio.sleep(0.5)  # Check every half second for more responsive queue handling

    @commands.command(name='speedstatus', aliases=['spdstatus', 'sr'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def speed_status(self, ctx):
        """Report current VLC playback rate/speed.

        Usage examples are shown with the configured prefix in `!!controls`.
        """
        try:
            status = await self.vlc.run_async("get_status")
            if not status:
                await ctx.send('Error: Could not access VLC status')
                return

            rate_elem = status.find('rate')
            rate_val = None
            if rate_elem is not None and rate_elem.text:
                try:
                    rate_val = float(rate_elem.text)
                except Exception:
                    # Try to clean whitespace/formatting
                    try:
                        rate_val = float(rate_elem.text.strip())
                    except Exception:
                        rate_val = None

            if rate_val is not None:
                embed = discord.Embed(
                    title="Playback Speed",
                    description=f"Current playback rate: {rate_val:.2f}x",
                    color=discord.Color.blue()
                )
            else:
                embed = discord.Embed(
                    title="Playback Speed",
                    description="Current playback rate: unknown (VLC did not expose rate in status)",
                    color=discord.Color.orange()
                )

            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to get playback speed: {e}")
            await ctx.send(f"Error reading playback speed: {e}")

    @commands.command(name='sub_next', aliases=['subn','sub+','subnext'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def subtitle_next(self, ctx):
        """Cycle to the next subtitle track in VLC (if supported)."""
        try:
            if not await self._check_room(ctx):
                return
            ok = await self.vlc.run_async("subtitle_next")
            if ok:
                embed = discord.Embed(
                    title="💬 Subtitles",
                    description="Switched to the next subtitle track.",
                    color=discord.Color.green()
                )
                await ctx.send(embed=embed)
            else:
                embed = discord.Embed(
                    title="💬 Subtitles",
                    description=(
                        "Could not cycle subtitle track. Ensure VLC's HTTP interface supports "
                        "relative subtitle changes (subtitle_track +1)."
                    ),
                    color=discord.Color.orange()
                )
                await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"subtitle_next error: {e}")
            await ctx.send(f"Error cycling subtitles: {e}")

    @commands.command(name='sub_prev', aliases=['subp','sub-','subprev'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def subtitle_prev(self, ctx):
        """Cycle to the previous subtitle track in VLC (if supported)."""
        try:
            if not await self._check_room(ctx):
                return
            ok = await self.vlc.run_async("subtitle_prev")
            if ok:
                embed = discord.Embed(
                    title="💬 Subtitles",
                    description="Switched to the previous subtitle track.",
                    color=discord.Color.green()
                )
                await ctx.send(embed=embed)
            else:
                embed = discord.Embed(
                    title="💬 Subtitles",
                    description=(
                        "Could not cycle subtitle track. Ensure VLC's HTTP interface supports "
                        "relative subtitle changes (subtitle_track -1)."
                    ),
                    color=discord.Color.orange()
                )
                await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"subtitle_prev error: {e}")
            await ctx.send(f"Error cycling subtitles: {e}")

    @commands.command(name='sub_list', aliases=['subs','slist'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def subtitle_list(self, ctx):
        """List available subtitle tracks and indicate which is selected."""
        try:
            try:
                await ctx.trigger_typing()
            except Exception:
                pass
            
            tracks = await self.vlc.run_async("get_subtitle_tracks")
            if tracks is None:
                await ctx.send("Couldn't retrieve subtitle tracks from VLC.")
                return
            if len(tracks) == 0:
                embed = discord.Embed(
                    title="💬 Subtitles",
                    description="No subtitle tracks reported by VLC for the current media.",
                    color=discord.Color.orange()
                )
                await ctx.send(embed=embed)
                return
            
            # Mark selected track based on our tracking (VLC API doesn't expose this)
            if self.selected_subtitle_stream_index is not None:
                for tr in tracks:
                    if tr.get('stream_index') == self.selected_subtitle_stream_index:
                        tr['selected'] = True
                        logger.info(f"Marked track as selected: stream_index={self.selected_subtitle_stream_index}")
                        break
            
            # Build a neatly aligned list
            lines = []
            # Determine width for index alignment
            max_index = max((tr.get('index') or i) for i, tr in enumerate(tracks, start=1))
            for i, tr in enumerate(tracks, start=1):
                # Use checkmark/empty circle markers for alignment
                mark = "✅" if tr.get('selected') else "⚪"
                ui_idx = tr.get('index') or i
                name = tr.get('name') or f"Track {ui_idx}"
                lines.append(f"{mark} **{ui_idx}.** {name}")
            list_text = "\n".join(lines[:20]) + ("\n..." if len(lines) > 20 else "")
            embed = discord.Embed(
                title="💬 Subtitle Tracks",
                description=list_text,
                color=discord.Color.blue()
            )
            # Show current selection explicitly
            try:
                selected_track = next((tr for tr in tracks if tr.get('selected')), None)
            except Exception:
                selected_track = None
            if selected_track:
                cur_idx = selected_track.get('index') or selected_track.get('id')
                cur_name = selected_track.get('name') or (f"Track {cur_idx}" if cur_idx is not None else "Track")
                embed.add_field(name="Current", value=f"✅ {cur_name} ({cur_idx})", inline=True)
            else:
                embed.add_field(name="Current", value="⚪ Off", inline=True)
            embed.add_field(
                name="Usage",
                value=(
                    f"Use {format_cmd_inline('sub_set <number>')} to select by position (as shown), or "
                    f"{format_cmd_inline('sub_set off')} to disable."
                ),
                inline=False
            )
            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"subtitle_list error: {e}")
            await ctx.send(f"Error listing subtitles: {e}")

    @commands.command(name='sub_set', aliases=['subset','subid'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def subtitle_set(self, ctx, track_id: str):
        """Select a specific subtitle track by position (from sub_list), or 'off' to disable.

        Examples:
        - sub_set 2          # Select 2nd subtitle in the list
        - sub_set 1          # Select 1st subtitle in the list
        - sub_set off        # Disable subtitles
        """
        try:
            if not await self._check_room(ctx):
                return
            if not track_id:
                await ctx.send(f"Usage: {format_cmd_inline('sub_set <number|off>')}")
                return
            # Fetch tracks to support index-based addressing
            tracks = await self.vlc.run_async("get_subtitle_tracks") or []
            logger.info(f"sub_set: User requested '{track_id}', found {len(tracks)} tracks")
            
            # Log all tracks for debugging
            for i, tr in enumerate(tracks, start=1):
                logger.debug(f"  Track {i}: id={tr.get('id')}, index={tr.get('index')}, stream_index={tr.get('stream_index')}, name={tr.get('name')}, selected={tr.get('selected')}")

            # Handle disable synonyms
            tokens_off = {"off", "none", "disable", "disabled"}
            if track_id.lower() in tokens_off:
                logger.info(f"sub_set: Disabling subtitles")
                # Try -1 first, fallback to 0 for older VLC versions
                ok = await self.vlc.run_async("set_subtitle_track", -1)
                if not ok:
                    ok = await self.vlc.run_async("set_subtitle_track", 0)
                if not ok:
                    await ctx.send("Failed to disable subtitles (tried -1 and 0).")
                    return
                # Track that subtitles are disabled
                self.selected_subtitle_stream_index = None
                embed = discord.Embed(
                    title="💬 Subtitles",
                    description="Subtitles disabled.",
                    color=discord.Color.green()
                )
                await ctx.send(embed=embed)
                return

            # Parse position as 1-based GUI order
            try:
                pos_index = int(track_id)
            except Exception:
                await ctx.send(f"Please provide a numeric position or 'off'. Use {format_cmd_inline('sub_list')} to see available tracks.")
                return
            
            if pos_index < 1 or pos_index > len(tracks):
                await ctx.send(f"Position out of range. There are {len(tracks)} subtitle tracks. Use {format_cmd_inline('sub_list')} to see them.")
                return
            
            logger.info(f"sub_set: Looking for track at position {pos_index}")
            
            # Find track by GUI order (index field)
            tid = None
            stream_idx = None
            selected_track = None
            for tr in tracks:
                if tr.get('index') == pos_index:
                    tid = tr.get('id')
                    stream_idx = tr.get('stream_index')
                    selected_track = tr
                    logger.info(f"sub_set: Found track by index: id={tid}, stream_index={stream_idx}, name={tr.get('name')}")
                    break
            
            # Fallback to list order if no index mapping available
            if tid is None:
                selected_track = tracks[pos_index - 1]
                tid = selected_track.get('id')
                stream_idx = selected_track.get('stream_index')
                logger.info(f"sub_set: Using list order fallback: id={tid}, stream_index={stream_idx}, name={selected_track.get('name')}")
            
            # Try setting subtitle track - prefer stream_index over track ID
            # VLC HTTP API typically uses stream index (0, 1, 2...) not track IDs
            ok = False
            if stream_idx is not None:
                logger.info(f"sub_set: Attempting to set subtitle by stream_index={stream_idx}")
                ok = await self.vlc.run_async("set_subtitle_track", stream_idx)
                if ok:
                    logger.info(f"sub_set: Successfully set by stream_index={stream_idx}")
                    # Track the selected subtitle
                    self.selected_subtitle_stream_index = stream_idx
            
            if not ok and tid is not None:
                logger.info(f"sub_set: Attempting to set subtitle by track id={tid}")
                ok = await self.vlc.run_async("set_subtitle_track", tid)
                if ok:
                    logger.info(f"sub_set: Successfully set by track id={tid}")
                    self.selected_subtitle_stream_index = stream_idx if stream_idx else tid
            
            if not ok:
                # Try direct position fallback for VLC versions that support it
                logger.warning(f"sub_set: Failed to set by stream_index and id, trying position-based fallback")
                ok = await self.vlc.run_async("set_subtitle_track", pos_index - 1)
                if ok:
                    logger.info(f"sub_set: Successfully set by position {pos_index - 1}")
                    self.selected_subtitle_stream_index = stream_idx if stream_idx else (pos_index - 1)
                else:
                    ok = await self.vlc.run_async("set_subtitle_track", pos_index)
                    if ok:
                        logger.info(f"sub_set: Successfully set by position {pos_index}")
                        self.selected_subtitle_stream_index = stream_idx if stream_idx else pos_index
                
            if not ok:
                embed = discord.Embed(
                    title="💬 Subtitles",
                    description=f"Failed to set subtitle track {pos_index}. Use {format_cmd_inline('sub_list')} to verify available tracks.",
                    color=discord.Color.orange()
                )
                await ctx.send(embed=embed)
                return

            # Confirm new selection by re-reading tracks
            tracks2 = await self.vlc.run_async("get_subtitle_tracks") or []
            selected_name = None
            selected_pos = None
            # Use the track we attempted to set as a fallback if VLC doesn't mark selection
            fallback_name = selected_track.get('name') if selected_track else None
            fallback_pos = selected_track.get('index') if selected_track else pos_index
            for tr in tracks2:
                if tr.get('selected'):
                    selected_name = tr.get('name')
                    selected_pos = tr.get('index')
                    logger.info(f"sub_set: Confirmed selection - pos={selected_pos}, name={selected_name}")
                    break

            if selected_pos is None:
                # Show the intended track as a helpful hint even if VLC didn't mark it selected yet
                desc = f"Set subtitle to {fallback_pos}: {fallback_name or 'Unknown'}"
            else:
                desc = f"Selected subtitle {selected_pos}: {selected_name or 'Unknown'}"

            embed = discord.Embed(
                title="💬 Subtitles",
                description=desc,
                color=discord.Color.green()
            )
            if tracks2:
                embed.add_field(
                    name="Tip",
                    value=f"Use {format_cmd_inline('sub_set off')} to disable subtitles.",
                    inline=False
                )
            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"subtitle_set error: {e}")
            await ctx.send(f"Error setting subtitles: {e}")

    @commands.command(name='audio_list', aliases=['audios','alist'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def audio_list(self, ctx):
        """List available audio tracks and indicate which is selected."""
        try:
            try:
                await ctx.trigger_typing()
            except Exception:
                pass

            tracks = await self.vlc.run_async("get_audio_tracks")
            if tracks is None:
                await ctx.send("Couldn't retrieve audio tracks from VLC.")
                return
            if len(tracks) == 0:
                embed = discord.Embed(
                    title="🔊 Audio",
                    description="No audio tracks reported by VLC for the current media.",
                    color=discord.Color.orange()
                )
                await ctx.send(embed=embed)
                return

            selected_track = next((tr for tr in tracks if tr.get('selected')), None)
            if not selected_track and self.selected_audio_stream_index is not None:
                for tr in tracks:
                    if tr.get('stream_index') == self.selected_audio_stream_index:
                        tr['selected'] = True
                        selected_track = tr
                        break

            lines = []
            for i, tr in enumerate(tracks, start=1):
                mark = "✅" if tr.get('selected') else "⚪"
                ui_idx = tr.get('index') or i
                name = tr.get('name') or f"Track {ui_idx}"
                lines.append(f"{mark} **{ui_idx}.** {name}")
            list_text = "\n".join(lines[:20]) + ("\n..." if len(lines) > 20 else "")
            embed = discord.Embed(
                title="🔊 Audio Tracks",
                description=list_text,
                color=discord.Color.blue()
            )

            if selected_track:
                cur_idx = selected_track.get('index') or selected_track.get('id')
                cur_name = selected_track.get('name') or (f"Track {cur_idx}" if cur_idx is not None else "Track")
                embed.add_field(name="Current", value=f"✅ {cur_name} ({cur_idx})", inline=True)
            else:
                embed.add_field(name="Current", value="⚪ Unknown", inline=True)

            embed.add_field(
                name="Usage",
                value=(
                    f"Use {format_cmd_inline('audio_set <number>')} to select by position."
                ),
                inline=False
            )
            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"audio_list error: {e}")
            await ctx.send(f"Error listing audio tracks: {e}")

    @commands.command(name='audio_set', aliases=['audioset','audioid'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def audio_set(self, ctx, track_id: str):
        """Select a specific audio track by position (from audio_list)."""
        try:
            if not await self._check_room(ctx):
                return
            if not track_id:
                await ctx.send(f"Usage: {format_cmd_inline('audio_set <number>')}")
                return

            tracks = await self.vlc.run_async("get_audio_tracks") or []
            if not tracks:
                await ctx.send(f"No audio tracks found. Use {format_cmd_inline('audio_list')} to check availability.")
                return

            try:
                pos_index = int(track_id)
            except Exception:
                await ctx.send(f"Please provide a numeric position. Use {format_cmd_inline('audio_list')} to see available tracks.")
                return

            if pos_index < 1 or pos_index > len(tracks):
                await ctx.send(f"Position out of range. There are {len(tracks)} audio tracks. Use {format_cmd_inline('audio_list')} to see them.")
                return

            tid = None
            stream_idx = None
            selected_track = None
            for tr in tracks:
                if tr.get('index') == pos_index:
                    tid = tr.get('id')
                    stream_idx = tr.get('stream_index')
                    selected_track = tr
                    break

            if tid is None:
                selected_track = tracks[pos_index - 1]
                tid = selected_track.get('id')
                stream_idx = selected_track.get('stream_index')

            ok = False
            if stream_idx is not None:
                ok = await self.vlc.run_async("set_audio_track", stream_idx)
                if ok:
                    self.selected_audio_stream_index = stream_idx

            if not ok and tid is not None:
                ok = await self.vlc.run_async("set_audio_track", tid)
                if ok:
                    self.selected_audio_stream_index = stream_idx if stream_idx is not None else tid

            if not ok:
                ok = await self.vlc.run_async("set_audio_track", pos_index - 1)
                if ok:
                    self.selected_audio_stream_index = stream_idx if stream_idx is not None else (pos_index - 1)
                else:
                    ok = await self.vlc.run_async("set_audio_track", pos_index)
                    if ok:
                        self.selected_audio_stream_index = stream_idx if stream_idx is not None else pos_index

            if not ok:
                embed = discord.Embed(
                    title="🔊 Audio",
                    description=f"Failed to set audio track {pos_index}. Use {format_cmd_inline('audio_list')} to verify available tracks.",
                    color=discord.Color.orange()
                )
                await ctx.send(embed=embed)
                return

            fallback_name = selected_track.get('name') if selected_track else None
            fallback_pos = selected_track.get('index') if selected_track else pos_index
            embed = discord.Embed(
                title="🔊 Audio",
                description=f"Selected audio {fallback_pos}: {fallback_name or 'Unknown'}",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"audio_set error: {e}")
            await ctx.send(f"Error setting audio track: {e}")

    @commands.command(name='status', aliases=['np', 'nowplaying'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def status(self, ctx):
        """Show current VLC status and what's playing"""
        embed = await self.get_status_embed()
        if embed:
            await ctx.send(embed=embed)
        else:
            await ctx.send("Could not retrieve VLC status.")

    async def get_status_embed(self):
        """Generate a rich embed for the current VLC status."""
        try:
            status = await self.vlc.run_async("get_status")
            if not status:
                return None

            state = status.find('state').text
            
            playlist = await self.vlc.run_async("get_playlist")
            position, current_item = self._find_current_position(playlist)
            
            item_name = None
            playlist_name = None
            item_uri = None
            if current_item is not None:
                item_name = current_item.get('name')
                playlist_name = current_item.get('name')
                item_uri = current_item.get('uri')

            metadata_name = self._choose_metadata_source_name(item_name, item_uri)

            # Fallback: pull filename from VLC status information when playlist current item is missing.
            if not item_name:
                try:
                    info_root = status.find('information')
                    if info_root is not None:
                        for category in info_root.findall('category'):
                            for info in category.findall('info'):
                                if info.get('name') == 'filename' and info.text:
                                    item_name = info.text
                                    break
                            if item_name:
                                break
                except Exception:
                    item_name = None

            # Secondary fallback: scan playlist leaf nodes for current item.
            if not item_name and playlist is not None:
                try:
                    for leaf in playlist.findall('.//leaf'):
                        if leaf.get('current'):
                            item_name = leaf.get('name')
                            if not playlist_name:
                                playlist_name = item_name
                            break
                except Exception:
                    pass

            # Prefer playlist name for parsing when it contains explicit TV episode markers.
            parse_name = metadata_name or item_name
            try:
                if playlist_name and re.search(r"(?i)(s\d{1,2}e\d{1,2}|\d{1,2}x\d{1,2})", playlist_name):
                    parse_name = playlist_name
            except Exception:
                parse_name = metadata_name or item_name or playlist_name

            # Try to get TMDB metadata
            tmdb_embed = None
            if item_name or metadata_name:
                edition_tag = MediaUtils.extract_edition_tag(parse_name or metadata_name or item_name)
                is_movie_embed = False
                episode_label = None
                clean_title, year = MediaUtils.parse_movie_filename(parse_name or metadata_name or item_name)
                tv_title, tv_season, tv_episode, tv_year = MediaUtils.parse_tv_filename(parse_name or metadata_name or item_name)
                if tv_season and tv_episode:
                    episode_label = f"S{int(tv_season):02d}E{int(tv_episode):02d}"
                
                # Detect if there's an explicit episode marker
                has_explicit_episode = bool(tv_episode) or bool(re.search(r"(?i)(s\d{1,2}e\d{1,2}|\d{1,2}x\d{1,2})", parse_name or metadata_name or item_name or ''))
                
                if has_explicit_episode and tv_title:
                    # Has explicit episode: prefer TV
                    tmdb_embed = self.tmdb.get_tv_metadata(tv_title, tv_season, tv_year)
                else:
                    # No explicit episode: try both and compare scores
                    movie_embed = None
                    tv_embed = None
                    movie_score = 0.0
                    tv_score = 0.0
                    
                    if clean_title:
                        movie_embed = self.tmdb.get_movie_metadata(clean_title, year, file_path=item_uri)
                        movie_score = getattr(self.tmdb, '_last_match_score', 0.0)
                    
                    if tv_title:
                        tv_embed = self.tmdb.get_tv_metadata(tv_title, tv_season, tv_year)
                        tv_score = getattr(self.tmdb, '_last_match_score', 0.0)
                    
                    # Choose the better match
                    if tv_embed and movie_embed:
                        tmdb_embed = tv_embed if tv_score > movie_score else movie_embed
                        is_movie_embed = (tmdb_embed == movie_embed)
                    elif tv_embed:
                        tmdb_embed = tv_embed
                    elif movie_embed:
                        tmdb_embed = movie_embed
                        is_movie_embed = True
                    else:
                        # Fallback: try generic TV lookup
                        if clean_title:
                            tmdb_embed = self.tmdb.get_tv_metadata(clean_title)

            if tmdb_embed:
                # Use the rich embed from TMDB
                final_embed = tmdb_embed
                final_embed.title = f"Now Playing: {final_embed.title}"
                if edition_tag and is_movie_embed:
                    try:
                        final_embed.add_field(name="Edition", value=edition_tag, inline=True)
                    except Exception:
                        pass
                if episode_label and not is_movie_embed:
                    try:
                        final_embed.add_field(name="Episode", value=episode_label, inline=True)
                    except Exception:
                        pass
            else:
                # Create a basic embed
                title = "VLC Status"
                if metadata_name or item_name:
                    title = f"Now Playing: {metadata_name or item_name}"
                
                final_embed = discord.Embed(title=title, color=discord.Color.blue())
                if item_name and edition_tag and not has_explicit_episode:
                    try:
                        final_embed.add_field(name="Edition", value=edition_tag, inline=True)
                    except Exception:
                        pass
                if episode_label:
                    try:
                        final_embed.add_field(name="Episode", value=episode_label, inline=True)
                    except Exception:
                        pass

            # Add playback state and position
            state_emoji_map = {
                'playing': '▶️',
                'paused': '⏸️',
                'stopped': '⏹️'
            }
            state_text = f"{state_emoji_map.get(state, '')} {state.capitalize()}".strip()
            
            if position and item_name:
                final_embed.add_field(name="Playlist", value=f"#{position}", inline=True)

            final_embed.add_field(name="State", value=state_text, inline=True)

            # Add time/duration
            time_elem = status.find('time')
            length_elem = status.find('length')
            if time_elem is not None and length_elem is not None:
                try:
                    current_time = int(time_elem.text)
                    total_time = int(length_elem.text)
                    if total_time > 0:
                        progress = f"{MediaUtils.format_time(current_time)} / {MediaUtils.format_time(total_time)}"
                        final_embed.add_field(name="Progress", value=progress, inline=False)
                except (ValueError, TypeError):
                    pass # Ignore if time/length are not valid numbers

            # Add footer
            try:
                if Config.KOFI_URL:
                    final_embed.add_field(name="Support kutsaratinidor by supporting CtrlVee", value=f"☕ <{Config.KOFI_URL}>", inline=False)
            except Exception:
                pass

            return final_embed

        except Exception as e:
            self.logger.error(f"Error getting status embed: {e}")
            return None
            

    def _playback_panel_registry_path(self) -> str:
        """Return the local file used to persist panel channel/message IDs."""
        return os.path.abspath(
            os.path.expanduser(
                str(getattr(Config, "PLAYBACK_PANEL_REGISTRY_PATH", "data/playback_panels.json"))
            )
        )

    def _save_playback_panel_registry(self, extra_entries=None) -> None:
        """Persist registered panel references atomically for the next restart."""
        path = self._playback_panel_registry_path()
        temporary_path = f"{path}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            entries = [
                {"channel_id": int(message.channel.id), "message_id": int(message.id)}
                for message in self._playback_panel_messages.values()
                if getattr(message, "channel", None) is not None
                and getattr(message.channel, "id", None) is not None
                and getattr(message, "id", None) is not None
            ]
            entries.extend(extra_entries or [])
            with open(temporary_path, "w", encoding="utf-8") as registry_file:
                json.dump(entries, registry_file, indent=2)
                registry_file.write("\n")
            os.replace(temporary_path, path)
        except OSError:
            try:
                if os.path.exists(temporary_path):
                    os.remove(temporary_path)
            except OSError:
                pass
            self.logger.exception("Could not persist playback panel registry")

    async def _restore_playback_panel_registry(self) -> None:
        """Resolve previously registered panel messages after Discord is ready."""
        if self._playback_panel_registry_restored:
            return
        self._playback_panel_registry_restored = True
        path = self._playback_panel_registry_path()
        try:
            with open(path, "r", encoding="utf-8") as registry_file:
                entries = json.load(registry_file)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError):
            self.logger.exception("Could not read playback panel registry")
            return

        if not isinstance(entries, list):
            self.logger.warning("Ignoring invalid playback panel registry format")
            return

        restored = {}
        unresolved = []
        for entry in entries:
            try:
                channel_id = int(entry["channel_id"])
                message_id = int(entry["message_id"])
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(channel_id)
                message = await channel.fetch_message(message_id)
                if getattr(getattr(message, "author", None), "id", None) != getattr(self.bot.user, "id", None):
                    continue
                embeds = getattr(message, "embeds", ())
                footer = getattr(embeds[0], "footer", None) if embeds else None
                footer_text = getattr(footer, "text", "") or ""
                if not footer_text.startswith("Video playback controls and queue overview."):
                    continue
                restored[message_id] = message
            except (KeyError, TypeError, ValueError, discord.NotFound, discord.Forbidden):
                continue
            except discord.HTTPException:
                self.logger.debug("Could not restore playback panel message %s", entry)
                unresolved.append(entry)
            except Exception:
                self.logger.exception("Unexpected error restoring playback panel %s", entry)

        self._playback_panel_messages.update(restored)
        self._save_playback_panel_registry(extra_entries=unresolved)

    async def _refresh_playback_panels(self) -> None:
        """Refresh registered panel messages without issuing playback commands."""
        if not self._playback_panel_messages:
            return

        embed = await self._playback_panel_embed()
        registry_changed = False
        for message_id, message in list(self._playback_panel_messages.items()):
            try:
                await message.edit(embed=embed, view=PlaybackControlView(self))
            except (discord.NotFound, discord.Forbidden):
                self._playback_panel_messages.pop(message_id, None)
                registry_changed = True
            except discord.HTTPException:
                self.logger.debug(
                    "Could not refresh playback panel message %s", message_id
                )
        if registry_changed:
            self._save_playback_panel_registry()

    async def _playback_panel_refresh_loop(self) -> None:
        """Keep active playback panel embeds current at a conservative interval."""
        try:
            await self.bot.wait_until_ready()
        except Exception:
            return

        try:
            interval = max(
                15,
                int(getattr(Config, "PLAYBACK_PANEL_REFRESH_INTERVAL", 30)),
            )
        except (TypeError, ValueError):
            interval = 30

        await self._restore_playback_panel_registry()
        try:
            await self._refresh_playback_panels()
        except Exception:
            self.logger.exception("Initial playback panel refresh failed")

        while not self.bot.is_closed():
            await asyncio.sleep(interval)
            try:
                await self._refresh_playback_panels()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Playback panel refresh failed")

    async def _playback_panel_embed(self) -> discord.Embed:
        """Build a video-first Now Playing embed for the interactive playback panel."""
        embed = await self.get_status_embed()
        if embed is None:
            embed = discord.Embed(
                title="Video Playback",
                description="VLC status is currently unavailable.",
                color=discord.Color.blue(),
            )
        elif embed.title == "VLC Status":
            embed.title = "Video Playback"

        try:
            queue_status, queue_playlist = await self.vlc.run_async("get_queue_snapshot")
            queued_items = queue_status.get("queued_items", {})
            embed.add_field(
                name="Shuffle",
                value="Enabled" if queue_status.get("shuffle_currently_on") else "Disabled",
                inline=True,
            )
            playlist_names = {
                item.get("id"): item.get("name", "Unknown")
                for item in (queue_playlist.findall(".//leaf") if queue_playlist is not None else [])
            }
            ordered_queue = sorted(
                queued_items.items(),
                key=lambda entry: entry[1].get("queue_order", 0),
            )
            queue_lines = []
            for item_id, info in ordered_queue[:3]:
                item_name = info.get("item_name", "Unknown")
                queue_lines.append(f"**#{info.get('queue_order', '?')}** {item_name}")
            if len(ordered_queue) > 3:
                queue_lines.append(f"…and {len(ordered_queue) - 3} more")
            embed.add_field(
                name=f"Soft Queue ({len(ordered_queue)})",
                value="\n".join(queue_lines) if queue_lines else "No queued items",
                inline=False,
            )
        except Exception:
            embed.add_field(name="Queue / Shuffle", value="Currently unavailable", inline=False)

        embed.set_footer(
            text="Video playback controls and queue overview. Playback actions are restricted to permitted roles."
        )
        return embed

    @commands.command(name="panel", aliases=["playback_panel"])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def playback_panel(self, ctx):
        """Post an interactive playback control panel."""
        message = await ctx.send(
            embed=await self._playback_panel_embed(),
            view=PlaybackControlView(self),
        )
        if message is not None and getattr(message, "id", None) is not None:
            self._playback_panel_messages[message.id] = message
            self._save_playback_panel_registry()

    @commands.command(name='play',aliases=['start','resume'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def play(self, ctx):
        """Start or resume playback"""

        if not await self._check_room(ctx):
            return
        if not await self._check_cooldown(ctx):
            return

        play_result = await self.vlc.run_async("play_if_not_playing")
        if not play_result["available"]:
            await ctx.send('Error: Could not access VLC')
            return
        if not play_result["command_sent"]:
            return

        if play_result["command_sent"]:
            await asyncio.sleep(0.5)
            new_status = await self.vlc.run_async("get_status")
            if new_status and new_status.find('state').text == 'playing':
                guild_id = str(ctx.guild.id) if ctx.guild else 'dm'
                self.last_state_change[guild_id] = asyncio.get_event_loop().time()
                logger.info("Playback started/resumed")
                embed = discord.Embed(
                    title="▶️ Playback started",
                    description="Playback started/resumed",
                    color=discord.Color.green()
                )
                await ctx.send(embed=embed)

    @commands.command(name='pause')
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def pause(self, ctx):
        """Pause playback"""
        if (
            bool(getattr(Config, "ENABLE_VOICE_JOIN", False))
            and bool(getattr(Config, "VOICE_AUTO_JOIN_ON_COMMAND", False))
        ):
            if not await self._check_room(ctx):
                return
        if not await self._check_cooldown(ctx):
            return

        pause_result = await self.vlc.run_async("pause_if_playing")
        if not pause_result["available"]:
            await ctx.send('Error: Could not access VLC')
            return
        if not pause_result["command_sent"]:
            return

        if pause_result["command_sent"]:
            await asyncio.sleep(0.5)
            new_status = await self.vlc.run_async("get_status")
            if new_status and new_status.find('state').text == 'paused':
                guild_id = str(ctx.guild.id) if ctx.guild else 'dm'
                self.last_state_change[guild_id] = asyncio.get_event_loop().time()
                logger.info("Playback paused")
                await ctx.send('Playback paused')

    @commands.command(name='stop')
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def stop(self, ctx):
        """Stop playback"""

        if not await self._check_room(ctx):
            return
        if await self.vlc.run_async("stop"):
            logger.info("Playback stopped")
            embed = discord.Embed(
                title="⏹️ Playback stopped",
                description="Playback has been stopped",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
        else:
            logger.error("Failed to stop playback")
            embed = discord.Embed(
                title="⏹️ Playback stop failed",
                description="Error: Could not stop playback",
                color=discord.Color.dark_red()
            )
            await ctx.send(embed=embed)

    @commands.command(name='restart')
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def restart(self, ctx):
        """Restart current file from the beginning"""

        if not await self._check_room(ctx):
            return
        if await self.vlc.run_async("seek", "0"):
            logger.info("Restarted current file from beginning")
            await ctx.send('Restarted current file from the beginning')
        else:
            logger.error("Failed to restart file")
            await ctx.send('Error: Could not restart file')

    @commands.command(name='rewind', aliases=['rw'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def rewind(self, ctx, seconds: int = 10):
        """Rewind playback by specified number of seconds"""

        if not await self._check_room(ctx):
            return
        if seconds <= 0:
            embed = discord.Embed(
                title="⏪ Rewind failed",
                description="Please specify a positive number of seconds",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return

        if await self.vlc.run_async("seek", f"-{seconds}"):
            embed = discord.Embed(
                title="⏪ Rewound",
                description=f"Rewound {seconds} seconds",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
        else:
            embed = discord.Embed(
                title="⏪ Rewind failed",
                description="Error: Could not rewind",
                color=discord.Color.dark_red()
            )
            await ctx.send(embed=embed)
 
    @commands.command(name='forward', aliases=['ff','skip'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def forward(self, ctx, seconds: int = 10):
        """Fast forward playback by specified number of seconds"""

        if not await self._check_room(ctx):
            return
        if seconds <= 0:
            embed = discord.Embed(
                title="⏩ Fast-forward failed",
                description="Please specify a positive number of seconds",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return

        if await self.vlc.run_async("seek", f"+{seconds}"):
            embed = discord.Embed(
                title="⏩ Fast-forwarded",
                description=f"Fast forwarded {seconds} seconds",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
        else:
            embed = discord.Embed(
                title="⏩ Fast-forward failed",
                description="Error: Could not fast forward",
                color=discord.Color.dark_red()
            )
            await ctx.send(embed=embed)
    
    @commands.command(name='play_num')
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def play_number(self, ctx, number: int):
        """Play a specific item from the playlist by its number"""
        try:
            if number < 1:
                await ctx.send('Please provide a number greater than 0')
                return

            ok, reason = await self._playback_guard(ctx.author)
            if not ok:
                await ctx.send(reason)
                return

            selection = await self.vlc.run_async("play_playlist_item_at_position", number)
            if not selection["success"]:
                error = selection.get("error")
                if error == "playlist_unavailable":
                    await ctx.send('Could not access VLC playlist')
                elif error == "playlist_empty":
                    await ctx.send('Playlist is empty')
                elif error == "position_out_of_range":
                    await ctx.send(f'Number too high. Playlist has {selection["playlist_count"]} items')
                else:
                    await ctx.send('Error: Could not start playback')
                return
            item = selection["item"]
            item_id = selection["item_id"]

            if selection["success"]:
                self._command_initiated_item_id = str(item_id)
                logger.info(f"Loading playlist item #{number}")
                await ctx.send(f'Loading item #{number}...')
                await asyncio.sleep(3)  # Give VLC time to load and start playing the file
                
                recovery = await self.vlc.run_async(
                    "resume_selected_item_if_current", item_id
                )
                if recovery["reason"] in {"playlist_unavailable", "status_unavailable"}:
                    self._clear_command_initiated_item(item_id)
                    await ctx.send(
                        "VLC accepted the selection, but playback could not be confirmed "
                        "because its current state is unavailable."
                    )
                    return
                if recovery["reason"] == "selection_changed":
                    self._clear_command_initiated_item(item_id)
                    logger.info(
                        "Skipping delayed play recovery for item %s; VLC selection changed to %s",
                        item_id,
                        recovery.get("current_item_id"),
                    )
                    return

                if recovery.get("state") != "playing":
                    # Retry only if this item is still current; never issue a generic pl_play.
                    await asyncio.sleep(2)
                    recovery = await self.vlc.run_async(
                        "resume_selected_item_if_current", item_id
                    )
                    if recovery["reason"] == "selection_changed":
                        self._clear_command_initiated_item(item_id)
                        logger.info(
                            "Skipping playback retry for item %s; VLC selection changed",
                            item_id,
                        )
                        return
                    await asyncio.sleep(1)
                    recovery = await self.vlc.run_async(
                        "resume_selected_item_if_current", item_id, resume=False
                    )
                    if recovery["reason"] == "selection_changed":
                        self._clear_command_initiated_item(item_id)
                        logger.info(
                            "Skipping playback announcement for item %s; VLC selection changed",
                            item_id,
                        )
                        return

                if recovery.get("state") != "playing":
                    self._clear_command_initiated_item(item_id)
                    await ctx.send(
                        "VLC accepted the selection, but playback could not be confirmed."
                    )
                    return

                self._seat_playback_owner(ctx.author, item_id)
                # Announce only after the selected item has been confirmed playing.
                await self._announce_now_playing('command', item, number)
                
                # Verify it's actually playing
                status = await self.vlc.run_async("get_status")
                if status and status.find('state').text != 'playing':
                    await ctx.send(f"Warning: VLC might not be playing. Try using {format_cmd_inline('play')} if playback doesn't start.")
            else:
                await ctx.send('Error: Could not start playback')
        except ValueError:
            await ctx.send('Please provide a valid number')
            
    def _clear_command_initiated_item(self, expected_item_id: str | None) -> None:
        """Clear only this command's monitor-suppression marker."""
        if (
            expected_item_id is not None
            and self._command_initiated_item_id == str(expected_item_id)
        ):
            self._command_initiated_item_id = None

    async def _confirm_navigation_target(
        self,
        ctx,
        expected_item_id: str | None,
        fallback_message: str,
        queued_result: dict | None = None,
    ) -> bool:
        """Announce a navigation result only if its selected item is still current."""
        if expected_item_id is None:
            logger.warning("Navigation command completed without a captured current item")
            await ctx.send(fallback_message)
            return False

        await asyncio.sleep(3)
        confirmation = await self.vlc.run_async(
            "get_current_item_if_unchanged", expected_item_id
        )
        if confirmation.get("reason") == "selection_changed":
            self._clear_command_initiated_item(expected_item_id)
            logger.info(
                "Skipping stale navigation announcement for item %s; current item is %s",
                expected_item_id,
                confirmation.get("current_item_id"),
            )
            return False
        if not confirmation.get("success"):
            self._clear_command_initiated_item(expected_item_id)
            await ctx.send(fallback_message)
            return False

        if confirmation.get("state") != "playing":
            await asyncio.sleep(2)
            confirmation = await self.vlc.run_async(
                "get_current_item_if_unchanged", expected_item_id
            )
            if not confirmation.get("success"):
                if confirmation.get("reason") == "selection_changed":
                    self._clear_command_initiated_item(expected_item_id)
                    logger.info(
                        "Skipping stale navigation retry for item %s; current item is %s",
                        expected_item_id,
                        confirmation.get("current_item_id"),
                    )
                    return False
                await ctx.send(fallback_message)
                return False

        if confirmation.get("state") != "playing":
            self._clear_command_initiated_item(expected_item_id)
            await ctx.send(fallback_message)
            return False

        item = confirmation.get("item")
        position = confirmation.get("position")
        if item is None:
            self._clear_command_initiated_item(expected_item_id)
            await ctx.send(fallback_message)
            return False

        self._seat_playback_owner(ctx.author, expected_item_id)
        if queued_result is not None:
            embed = discord.Embed(
                title="🎵 Playing Queued Item",
                description=f"Now playing: **{queued_result['item_name']}**",
                color=discord.Color.green(),
            )
            embed.add_field(
                name="Queue Info",
                value=f"This was queued item #{queued_result.get('queue_order', 'unknown')}",
                inline=True,
            )
            await ctx.send(embed=embed)
            try:
                await self._set_presence(queued_result.get("item_name"), reason="next (queued)")
            except Exception:
                pass
        else:
            await self._announce_now_playing("command", item, position)

        try:
            self._suppress_auto_announce_until = asyncio.get_event_loop().time() + 5.0
        except Exception:
            pass
        return True

    @commands.command(name='next')
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def next_track(self, ctx):
        """Play next track in playlist (prioritizes queued items)"""
        if not await self._check_cooldown(ctx):
            return
        
        ok, reason = await self._playback_guard(ctx.author)
        if not ok:
            await ctx.send(reason)
            return

        # Resolve the queue and issue the selected playback command atomically.
        decision = await self.vlc.run_async("play_next_or_advance")
        expected_item_id = decision.get("expected_item_id")
        self._command_initiated_item_id = (
            str(expected_item_id) if expected_item_id is not None else None
        )
        queued_result = decision.get("queued_result")
        if queued_result and not queued_result.get("success"):
            await ctx.send(f"Error playing queued item: {queued_result.get('error', 'Unknown error')}")
        if queued_result and queued_result.get("success"):
            result = queued_result
            await self._confirm_navigation_target(
                ctx,
                decision.get("expected_item_id"),
                "The queued item did not remain selected; no now-playing update was sent.",
                queued_result=result,
            )
            return

        if decision.get("next_success"):
            logger.info("Loading next track")
            await ctx.send('Loading next track...')
            await self._confirm_navigation_target(
                ctx,
                decision.get("expected_item_id"),
                "Skipped to next track",
            )
        else:
            await ctx.send('Error: Could not skip to next track')
            
    @commands.command(name='previous')
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def previous_track(self, ctx):
        """Play previous track in playlist"""
        if not await self._check_cooldown(ctx):
            return
            
        ok, reason = await self._playback_guard(ctx.author)
        if not ok:
            await ctx.send(reason)
            return

        decision = await self.vlc.run_async("previous_and_capture")
        expected_item_id = decision.get("expected_item_id")
        self._command_initiated_item_id = (
            str(expected_item_id) if expected_item_id is not None else None
        )
        if decision.get("success"):
            logger.info("Loading previous track")
            await ctx.send('Loading previous track...')
            await self._confirm_navigation_target(
                ctx,
                decision.get("expected_item_id"),
                "Jumped to previous track",
            )
        else:
            await ctx.send('Error: Could not jump to previous track')

            
    async def _check_vlc_connection(self, ctx):
        """Check if VLC is accessible and send error message if not
        
        Returns:
            bool: True if VLC is accessible, False if not
        """
        try:
            status = await self.vlc.run_async("get_status")
            if not status:
                logger.error("Could not access VLC - HTTP interface may not be enabled")
                await ctx.send('Error: Could not access VLC. Make sure VLC is running with HTTP interface enabled.')
                return False
            logger.debug("VLC connection check successful")
            return True
        except Exception as e:
            logger.error(f"Error connecting to VLC: {str(e)}")
            await ctx.send(f'Error connecting to VLC: {str(e)}')
            return False
        
    async def _set_presence(self, name: str | None, reason: str | None = None):
        """Set the bot's Discord presence (throttled).

        Args:
            name: The activity name to show (e.g., movie title). If None, clears the activity.
        """
        # Respect global config toggle
        try:
            if not getattr(Config, 'ENABLE_PRESENCE', True):
                logger.debug("Presence updates disabled by config; skipping change")
                return
            now = asyncio.get_event_loop().time()
            # Always allow clearing presence. For setting a title, only throttle if
            # it's the same as the last title within the throttle window.
            if name is not None and (now - self._presence_last_set) < self._presence_throttle_seconds:
                if self._presence_last_name == name:
                    logger.debug(
                        f"Presence skipped due to throttle (same title within {self._presence_throttle_seconds}s): {name}"
                    )
                    return

            # Build an activity. Use 'Watching' to avoid Streaming URL requirements.
            if name:
                display_name = f"🎬 {name}"
                try:
                    activity = discord.Activity(type=discord.ActivityType.watching, name=display_name)
                except Exception:
                    # Fallback: still attempt to set as watching if streaming creation fails
                    activity = discord.Activity(type=discord.ActivityType.watching, name=display_name)
            else:
                activity = None

            # Attempt to change presence; non-fatal
            try:
                await self.bot.change_presence(activity=activity)
                self._presence_last_set = now
                prev = self._presence_last_name
                self._presence_last_name = name
                # Log updates sparingly: only log a clear once per stopped state
                if name:
                    logger.info(
                        f"Presence updated to: {display_name}" + (f" (reason: {reason})" if reason else "")
                    )
                else:
                    # Avoid spamming 'Presence cleared' when repeatedly stopped
                    try:
                        if getattr(self, '_presence_cleared_once', False):
                            # Already logged this state; keep quiet
                            pass
                        else:
                            logger.info("Presence cleared" + (f" (reason: {reason})" if reason else ""))
                            self._presence_cleared_once = True
                    except Exception:
                        logger.info("Presence cleared" + (f" (reason: {reason})" if reason else ""))
            except Exception as e:
                logger.debug(f"Failed to set presence: {e}")
        except Exception as e:
            logger.debug(f"Presence update error: {e}")
            
    async def _status_legacy(self, ctx):
        """Legacy status implementation (kept for reference, not registered as a command)."""
        if not await self._check_vlc_connection(ctx):
            return
        # Use formatting helper for commands
            
        status = await self.vlc.run_async("get_status")
            
        state = status.find('state').text
        current = status.find('information')
        
        logger.info(f"Current VLC state: {state}")
        logger.debug(f"Status - Current Info: {ET.tostring(current).decode() if current is not None else 'None'}")
        
        # Get position by finding current item in playlist
        playlist = await self.vlc.run_async("get_playlist")
        current_position = None
        if playlist is not None:
            # Find current item and its position
            for i, item in enumerate(playlist.findall('.//leaf')):
                if item.get('current'):
                    current_position = i + 1  # Convert to 1-based index
                    break
            logger.info(f"Current position in playlist: {current_position if current_position else 'not found'}")
        
        # Compute media library size
        size_bytes = self.watch_service.get_total_media_size() if self.watch_service else 0
        def human_size(num):
            for unit in ['B','KB','MB','GB','TB']:
                if num < 1024.0:
                    return f"{num:.2f} {unit}"
                num /= 1024.0
            return f"{num:.2f} PB"

        embed = discord.Embed(
            title="VLC Status",
            color=discord.Color.blue()
        )
        embed.add_field(name="State", value=state.capitalize(), inline=True)
        embed.add_field(name="Media Library Size", value=human_size(size_bytes), inline=True)
        
        if state != 'stopped':
            # Get the name from information/category/info[@name='filename']
            name = None
            if current is not None:
                for category in current.findall('category'):
                    for info in category.findall('info'):
                        if info.get('name') == 'filename':
                            name = info.text
                            break
                    if name:
                        break
            
            # If we couldn't get name from status, try playlist
            if not name:
                playlist = await self.vlc.run_async("get_playlist")
                if playlist is not None:
                    for item in playlist.findall('.//leaf'):
                        if item.get('current'):
                            name = item.get('name')
                            break

            # Prefer playlist name for episode parsing when it contains SxxEyy/1x02
            playlist_name = None
            current_item_uri = None
            try:
                playlist = await self.vlc.run_async("get_playlist")
                if playlist is not None:
                    for item in playlist.findall('.//leaf'):
                        if item.get('current'):
                            playlist_name = item.get('name')
                            current_item_uri = item.get('uri')
                            break
            except Exception:
                playlist_name = None

            parse_name = name
            try:
                if playlist_name and re.search(r"(?i)(s\d{1,2}e\d{1,2}|\d{1,2}x\d{1,2})", playlist_name):
                    parse_name = playlist_name
            except Exception:
                parse_name = name or playlist_name
            
            if name:
                logger.debug(f"Status - Found name: {name}")
                edition_tag = MediaUtils.extract_edition_tag(parse_name or name)
                is_movie_embed = False
                episode_label = None
                # Get metadata (try both movie and TV)
                search_title, search_year = MediaUtils.parse_movie_filename(parse_name or name)
                tv_title, tv_season, tv_episode, tv_year = MediaUtils.parse_tv_filename(parse_name or name)
                if tv_season and tv_episode:
                    episode_label = f"S{int(tv_season):02d}E{int(tv_episode):02d}"
                logger.debug(f"Status - Cleaned title: {search_title}, Year: {search_year}")
                
                # Detect if there's an explicit episode marker
                has_explicit_episode = bool(tv_episode) or bool(re.search(r"(?i)(s\d{1,2}e\d{1,2}|\d{1,2}x\d{1,2})", name))
                
                movie_data = None
                if has_explicit_episode and tv_title:
                    # Has explicit episode: prefer TV
                    movie_data = self.tmdb.get_tv_metadata(tv_title, tv_season, tv_year)
                else:
                    # No explicit episode: try both and compare scores
                    movie_embed = None
                    tv_embed = None
                    movie_score = 0.0
                    tv_score = 0.0
                    
                    if search_title:
                        movie_embed = self.tmdb.get_movie_metadata(search_title, search_year, file_path=current_item_uri)
                        movie_score = getattr(self.tmdb, '_last_match_score', 0.0)
                    
                    if tv_title:
                        tv_embed = self.tmdb.get_tv_metadata(tv_title, tv_season, tv_year)
                        tv_score = getattr(self.tmdb, '_last_match_score', 0.0)
                    
                    # Choose the better match
                    if tv_embed and movie_embed:
                        movie_data = tv_embed if tv_score > movie_score else movie_embed
                        is_movie_embed = (movie_data == movie_embed)
                    elif tv_embed:
                        movie_data = tv_embed
                    elif movie_embed:
                        movie_data = movie_embed
                        is_movie_embed = True
                    else:
                        # Fallback: try generic TV lookup
                        if search_title:
                            movie_data = self.tmdb.get_tv_metadata(search_title)
                
                logger.debug(f"Status - Movie data: {movie_data}")
                
                # Log movie data attributes
                if movie_data:
                    logger.debug("Status - Movie data attributes:")
                    logger.debug(f"  - Has title: {hasattr(movie_data, 'title')}")
                    logger.debug(f"  - Has overview: {hasattr(movie_data, 'overview')}")
                    logger.debug(f"  - Has tmdb_url: {hasattr(movie_data, 'tmdb_url')}")
                    logger.debug(f"  - Has fields: {hasattr(movie_data, 'fields')}")
                    if hasattr(movie_data, 'fields'):
                        logger.debug("  - Fields:")
                        for field in movie_data.fields:
                            logger.debug(f"    - {field.name}: {field.value}")
                
                # Add position info if available
                position_text = f" (Item {current_position})" if current_position is not None else ""
                
                if movie_data:
                    logger.debug("Status - Using TMDB movie data")
                    # Use the movie_data embed directly and update the state field
                    embed = movie_data
                    if edition_tag and is_movie_embed:
                        try:
                            embed.add_field(name="Edition", value=edition_tag, inline=True)
                        except Exception:
                            pass
                    if episode_label and not is_movie_embed:
                        try:
                            embed.add_field(name="Episode", value=episode_label, inline=True)
                        except Exception:
                            pass
                    embed.insert_field_at(0, name="State", value=state.capitalize(), inline=True)
                else:
                    logger.debug("Status - No movie data, using filename")
                    # Add the filename to our existing embed
                    embed.add_field(
                        name="Now Playing",
                        value=name,
                        inline=False
                    )
                    if edition_tag and not has_explicit_episode:
                        try:
                            embed.add_field(name="Edition", value=edition_tag, inline=True)
                        except Exception:
                            pass
                    if episode_label:
                        try:
                            embed.add_field(name="Episode", value=episode_label, inline=True)
                        except Exception:
                            pass
                    
                # Add progress information
                time_elem = status.find('time')
                length_elem = status.find('length')
                if time_elem is not None and length_elem is not None:
                    time = int(time_elem.text)
                    length = int(length_elem.text)
                    progress = f"{time//60}:{time%60:02d}/{length//60}:{length%60:02d}"
                    embed.add_field(name="Progress", value=progress, inline=True)
                    
                # Add position note as a field with bold formatting after progress
                if current_position is not None:
                    embed.add_field(name="Quick Replay", value=f"💡 Use {format_cmd_inline(f'play_num {current_position}')} to play this item again", inline=False)
                
                # Ensure Support field (Ko-fi) is present and a thumbnail is set for visibility
                try:
                    if Config.KOFI_URL:
                        try:
                            embed.add_field(name="Support kutsaratinidor by supporting CtrlVee", value=f"☕ {f'<{Config.KOFI_URL}>'}", inline=False)
                        except Exception:
                            pass
                except Exception:
                    pass

                # If embed has no thumbnail, attempt to set bot avatar as thumbnail to increase visibility
                try:
                    if not embed.thumbnail or not getattr(embed.thumbnail, 'url', None):
                        bot_user = getattr(self.bot, 'user', None)
                        if bot_user and getattr(bot_user, 'display_avatar', None):
                            embed.set_thumbnail(url=bot_user.display_avatar.url)
                except Exception:
                    pass
            else:
                # Playing but no name found
                embed.add_field(name="Now Playing", value="Unknown item", inline=False)
        else:
            # VLC is stopped - add helpful information
            embed.add_field(name="Now Playing", value="Nothing currently playing", inline=False)
            
            # Add playlist information if available
            if playlist is not None:
                items = playlist.findall('.//leaf')
                playlist_count = len(items)
                if playlist_count > 0:
                    embed.add_field(
                        name="Playlist Info", 
                        value=f"{playlist_count} items in playlist\nUse {format_cmd_inline('play')} to resume or {format_cmd_inline('play_num <number>')} to play a specific item",
                        inline=False
                    )
                else:
                    embed.add_field(name="Playlist Info", value="Playlist is empty", inline=False)
        
        await ctx.send(embed=embed)

    @commands.command(name='queue_next', aliases=['qnext'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def queue_next(self, ctx, number: int):
        """Queue a specific playlist item to play next using soft queue system (handles shuffle intelligently)"""
        try:
            if number < 1:
                await ctx.send('Please provide a number greater than 0')
                return

            ok, reason = await self._playback_guard(ctx.author)
            if not ok:
                await ctx.send(reason)
                return

            result = await self.vlc.run_async("queue_playlist_item_at_position", number)
            if not result.get("success"):
                error = result.get("error")
                if error == "playlist_unavailable":
                    await ctx.send('Could not access VLC playlist')
                elif error == "playlist_empty":
                    await ctx.send('Playlist is empty')
                elif error == "position_out_of_range":
                    await ctx.send(f'Number too high. Playlist has {result["playlist_count"]} items')
                else:
                    await ctx.send(f'Error queuing item: {result.get("error", "Unknown error")}')
                return
            item_id = result["item_id"]
            item_name = result["item_name"]
            
            if result.get("success"):
                embed = discord.Embed(
                    title="🎵 Item Queued",
                    color=discord.Color.green()
                )
                embed.add_field(
                    name="Queued Item",
                    value=f"**{item_name}**\n📋 Playlist position: #{number}",
                    inline=False
                )
                embed.add_field(
                    name="Queue Position",
                    value=f"#{result['queue_order']} of {result.get('total_queued', 1)} in queue",
                    inline=True
                )
                
                await ctx.send(embed=embed)
                logger.info(f"Queued item #{number} ({item_name}) to play next")
            else:
                await ctx.send(f'Error queuing item: {result.get("error", "Unknown error")}')
                
        except ValueError:
            await ctx.send('Please provide a valid number')
        except Exception as e:
            logger.error(f"Error in queue_next command: {e}")
            await ctx.send(f'Error queuing item: {str(e)}')

    def _scheduled_media_protection(self):
        """Snapshot scheduled playlist identities that queue cleanup must preserve."""
        get_cog = getattr(self.bot, "get_cog", None)
        scheduler = get_cog("Scheduler") if callable(get_cog) else None
        schedules = getattr(scheduler, "scheduled", ()) if scheduler is not None else ()
        item_ids = set()
        uris = set()
        for schedule in schedules:
            if not isinstance(schedule, dict):
                continue
            target_item_id = schedule.get("target_item_id")
            if target_item_id is not None:
                item_ids.add(str(target_item_id))
            uri = schedule.get("uri")
            if uri:
                uris.add(str(uri))
        return {"protected_item_ids": item_ids, "protected_uris": uris}

    async def queue_playlist_item_by_id(self, ctx, item_id: str):
        """Queue the exact playlist item selected by the interactive picker."""
        allowed, reason = await self._playback_guard(ctx.author)
        if not allowed:
            await ctx.send(reason)
            return

        result = await self.vlc.run_async("queue_playlist_item_by_id", item_id)
        if not result.get("success"):
            if result.get("error") == "item_not_found":
                message = "That playlist item is no longer available. Reopen Add to Queue and choose it again."
            elif result.get("error") == "playlist_unavailable":
                message = "Could not access the VLC playlist. Please try again."
            elif result.get("error") == "playlist_empty":
                message = "The VLC playlist is empty."
            else:
                message = f"Could not queue that item: {result.get('error', 'Unknown error')}"
            await ctx.send(message)
            return

        await ctx.send(
            f"Queued **{result.get('item_name', 'Unknown')}** as queue "
            f"item **#{result.get('queue_order', '?')}**."
        )

    @commands.command(name='queue_status', aliases=['qstatus'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def queue_status(self, ctx):
        """Show current soft queue status and shuffle state"""
        try:
            queue_status, playlist = await self.vlc.run_async("get_queue_snapshot")
            shuffle_on = queue_status.get("shuffle_currently_on", False)
            
            embed = discord.Embed(
                title="📋 Queue Status",
                color=discord.Color.blue()
            )
            
            # Get queue count for title
            queued_items = queue_status.get("queued_items", {})
            queue_count = len(queued_items)
            if queue_count > 0:
                embed.title = f"📋 Queue Status ({queue_count} item{'s' if queue_count != 1 else ''})"
            
            # Active queued items
            if queued_items:
                # Get playlist to map item IDs to titles and positions
                playlist_map = {}
                if playlist:
                    for idx, item in enumerate(playlist.findall('.//leaf'), 1):
                        item_id = item.get('id')
                        item_name = item.get('name', 'Unknown')
                        if item_id:
                            playlist_map[item_id] = {
                                'name': item_name,
                                'position': idx
                            }
                
                queue_list = []
                for queue_id, info in sorted(
                    queued_items.items(),
                    key=lambda entry: entry[1].get("queue_order", 0),
                ):
                    playlist_item_id = str(info.get("playlist_item_id", info.get("item_id", "")))
                    if playlist_item_id in playlist_map:
                        playlist_pos = playlist_map[playlist_item_id]['position']
                        queue_list.append(
                            f"• **{info.get('item_name', 'Unknown')}** "
                            f"(playlist #{playlist_pos}, queue #{info['queue_order']})"
                        )
                    else:
                        queue_list.append(
                            f"• **{info.get('item_name', 'Unknown')}** (queue #{info['queue_order']})"
                        )
                
                embed.add_field(
                    name="Active Queue Items",
                    value="\n".join(queue_list[:5]) + ("\n..." if len(queue_list) > 5 else ""),
                    inline=False
                )
            else:
                embed.add_field(
                    name="Active Queue Items",
                    value="No items currently queued",
                    inline=False
                )
            
            
            # Usage hint
            embed.add_field(
                name="Usage",
                value=f"Use {format_cmd_inline('queue_next <number>')} to queue a playlist item to play next",
                inline=False
            )
            
            await ctx.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error in queue_status command: {e}")
            await ctx.send(f'Error getting queue status: {str(e)}')

    @commands.command(name='clear_queue', aliases=['qclear'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def clear_queue(self, ctx):
        """Clear all queue tracking (useful for reset)"""

        if not await self._check_room(ctx):
            return
        try:
            result = await self.vlc.run_async(
                "clear_queue",
                **self._scheduled_media_protection(),
            )
            if not result.get("success"):
                raise RuntimeError(
                    f"Queue cleanup incomplete: {result.get('failed_item_ids', [])}"
                )
            preserved_count = len(result.get("preserved_item_ids", []))
            embed = discord.Embed(
                title="🗑️ Queue Cleared",
                description=(
                    "Queued media and its queue tracking have been reconciled. "
                    f"Preserved {preserved_count} current or scheduled item(s)."
                    if preserved_count else
                    "Queued media and its queue tracking have been reconciled."
                ),
                color=discord.Color.orange()
            )
            embed.add_field(
                name="Note",
                value="The current item and scheduled media are preserved; unrelated playlist entries are untouched.",
                inline=False
            )
            await ctx.send(embed=embed)
            logger.info("Queue tracking cleared by user command")
            
        except Exception as e:
            logger.error(f"Error in clear_queue command: {e}")
            await ctx.send(f'Error clearing queue: {str(e)}')

    @commands.command(name='remove_queue', aliases=['qremove','unqueue'])
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def remove_queue(self, ctx, ref: str):
        """Remove a queued entry by queue order (e.g., 1) or playlist number (#10).

        Usage:
        - remove_queue 1           # remove by queue order
        - remove_queue #10         # remove by playlist number

        (Commands are shown with the configured prefix in the help output.)
        """

        if not await self._check_room(ctx):
            return
        try:
            if ref.startswith('#'):
                num = int(ref[1:])
                result = await self.vlc.run_async("remove_from_queue_by_playlist_number", num, **self._scheduled_media_protection())
            else:
                num = int(ref)
                result = await self.vlc.run_async("remove_from_queue_by_order", num, **self._scheduled_media_protection())

            if not result.get('success'):
                await ctx.send(f"❌ {result.get('error', 'Failed to remove from queue')}")
                return

            name = result.get('item_name', 'Unknown')
            embed = discord.Embed(
                title="✅ Removed from Queue",
                description=f"{name} has been removed from the queue.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
        except ValueError:
            await ctx.send("❌ Invalid reference. Use a number (queue order) or #<playlist number>.")
        except Exception as e:
            logger.error(f"Error in remove_queue command: {e}")
            await ctx.send(f"Error removing from queue: {str(e)}")

    @commands.has_any_role(*Config.ALLOWED_ROLES)
    @commands.command(name='shuffle_on', aliases=['shuffle_enable'])
    async def shuffle_on(self, ctx):
        """Enable shuffle mode"""

        if not await self._check_room(ctx):
            return
        try:
            shuffle_result = await self.vlc.run_async("set_shuffle_state", True)
            current_shuffle = shuffle_result["previous"]
            if not shuffle_result["success"]:
                await ctx.send("❌ Could not update VLC shuffle mode.")
                return

            if current_shuffle:
                embed = discord.Embed(
                    title="🔀 Shuffle Already On",
                    description="Shuffle mode is already enabled",
                    color=discord.Color.blue()
                )
            else:
                # Enable shuffle
                embed = discord.Embed(
                    title="🔀 Shuffle Enabled",
                    description="Shuffle mode has been turned on",
                    color=discord.Color.green()
                )
            
            await ctx.send(embed=embed)
            logger.info(f"Shuffle enable command used by {ctx.author} (was already {'on' if current_shuffle else 'off'})")
            
        except Exception as e:
            logger.error(f"Error in shuffle_on command: {e}")
            await ctx.send(f'Error enabling shuffle: {str(e)}')

    @commands.has_any_role(*Config.ALLOWED_ROLES)
    @commands.command(name='shuffle_off', aliases=['shuffle_disable'])
    async def shuffle_off(self, ctx):
        """Disable shuffle mode"""

        if not await self._check_room(ctx):
            return
        try:
            shuffle_result = await self.vlc.run_async("set_shuffle_state", False)
            current_shuffle = shuffle_result["previous"]
            if not shuffle_result["success"]:
                await ctx.send("❌ Could not update VLC shuffle mode.")
                return

            if not current_shuffle:
                embed = discord.Embed(
                    title="▶️ Shuffle Already Off",
                    description="Shuffle mode is already disabled",
                    color=discord.Color.blue()
                )
            else:
                # Disable shuffle
                embed = discord.Embed(
                    title="▶️ Shuffle Disabled",
                    description="Shuffle mode has been turned off",
                    color=discord.Color.green()
                )
            
            await ctx.send(embed=embed)
            logger.info(f"Shuffle disable command used by {ctx.author} (was already {'on' if current_shuffle else 'off'})")
            
        except Exception as e:
            logger.error(f"Error in shuffle_off command: {e}")
            await ctx.send(f'Error disabling shuffle: {str(e)}')

    @commands.has_any_role(*Config.ALLOWED_ROLES)
    @commands.command(name='shuffle_toggle', aliases=['shuffle'])
    async def shuffle_toggle(self, ctx):
        """Toggle shuffle mode on/off"""

        if not await self._check_room(ctx):
            return
        try:
            shuffle_result = await self.vlc.run_async("toggle_shuffle_state")
            if not shuffle_result["success"]:
                await ctx.send("❌ Could not update VLC shuffle mode.")
                return
            current_shuffle = shuffle_result["previous"]
            new_shuffle = shuffle_result["enabled"]
            
            if new_shuffle:
                embed = discord.Embed(
                    title="🔀 Shuffle Enabled",
                    description="Shuffle mode has been turned on",
                    color=discord.Color.green()
                )
            else:
                embed = discord.Embed(
                    title="▶️ Shuffle Disabled",
                    description="Shuffle mode has been turned off",
                    color=discord.Color.green()
                )
            
            await ctx.send(embed=embed)
            logger.info(f"Shuffle toggle command used by {ctx.author} (changed from {'on' if current_shuffle else 'off'} to {'on' if new_shuffle else 'off'})")
            
        except Exception as e:
            logger.error(f"Error in shuffle_toggle command: {e}")
            await ctx.send(f'Error toggling shuffle: {str(e)}')

class PlaybackAudioTrackSelect(discord.ui.Select):
    """Choose an audio track reported by the current VLC media."""

    def __init__(self, playback_view, tracks):
        self.playback_view = playback_view
        options = []
        for position, track in enumerate(tracks, 1):
            index = track.get("index") or position
            name = track.get("name") or f"Track {index}"
            options.append(discord.SelectOption(
                label=f"{index}. {name}"[:100],
                value=str(index),
                description=(str(track.get("language") or "Audio track")[:100]),
                default=bool(track.get("selected")),
            ))
        super().__init__(
            placeholder="Audio track",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.playback_view._dispatch(
            interaction, "audio_set", self.values[0], refresh_panel=False
        )


class PlaybackSubtitleTrackSelect(discord.ui.Select):
    """Choose a subtitle track, disable subtitles, or cycle available tracks."""

    def __init__(self, playback_view, tracks):
        self.playback_view = playback_view
        options = []
        track_limit = 22 if len(tracks) > 1 else 24
        for position, track in enumerate(tracks[:track_limit], 1):
            index = track.get("index") or position
            name = track.get("name") or f"Track {index}"
            options.append(discord.SelectOption(
                label=f"{index}. {name}"[:100],
                value=str(index),
                description=(str(track.get("language") or "Subtitle track")[:100]),
                default=bool(track.get("selected")),
            ))
        if len(tracks) > 1:
            options.extend([
                discord.SelectOption(label="Next subtitle track", value="__next__"),
                discord.SelectOption(label="Previous subtitle track", value="__previous__"),
            ])
        options.append(discord.SelectOption(label="Turn subtitles off", value="off"))
        super().__init__(
            placeholder="Subtitle track",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if selected == "__next__":
            command, args = "subtitle_next", ()
        elif selected == "__previous__":
            command, args = "subtitle_prev", ()
        else:
            command, args = "subtitle_set", (selected,)
        await self.playback_view._dispatch(
            interaction, command, *args, refresh_panel=False
        )


class PlaybackSpeedSelect(discord.ui.Select):
    """Choose a playback rate using the existing speed command."""

    SPEED_OPTIONS = (
        ("0.5x", "0.5"),
        ("0.75x", "0.75"),
        ("Normal (1.0x)", "1.0"),
        ("1.25x", "1.25"),
        ("1.5x", "1.5"),
        ("1.75x", "1.75"),
        ("2.0x", "2.0"),
    )

    def __init__(self, playback_view):
        self.playback_view = playback_view
        super().__init__(
            placeholder="Playback speed",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=value, default=value == "1.0")
                for label, value in self.SPEED_OPTIONS
            ],
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.playback_view._dispatch(
            interaction, "speed", self.values[0], refresh_panel=False
        )


class PlaybackAdvancedView(discord.ui.View):
    """Ephemeral advanced controls populated from the currently loaded VLC media."""

    def __init__(self, playback_view, audio_tracks, subtitle_tracks):
        super().__init__(timeout=120)
        self.playback_view = playback_view
        if audio_tracks:
            self.add_item(PlaybackAudioTrackSelect(playback_view, audio_tracks))
        if subtitle_tracks:
            self.add_item(PlaybackSubtitleTrackSelect(playback_view, subtitle_tracks))
        self.add_item(PlaybackSpeedSelect(playback_view))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _member_has_allowed_role(interaction.user, Config.ALLOWED_ROLES):
            await interaction.response.send_message(
                "You don't have a role permitted to control playback.",
                ephemeral=True,
            )
            return False
        return True


class QueuePlaylistSelect(discord.ui.Select):
    """Select a playlist entry from the current page to add to the soft queue."""

    def __init__(self, picker_view):
        self.picker_view = picker_view
        start = picker_view.page * picker_view.page_size
        page_items = picker_view.playlist_items[start:start + picker_view.page_size]
        options = [
            discord.SelectOption(
                label=(item.get("name") or "Unknown")[:100],
                value=str(item.get("id")),
                description=f"Playlist item #{position}",
            )
            for position, item in page_items
        ]
        super().__init__(
            placeholder=f"Choose a playlist item (page {picker_view.page + 1}/{picker_view.page_count})",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if not _member_has_allowed_role(interaction.user, Config.ALLOWED_ROLES):
            await interaction.response.send_message(
                "You don't have a role permitted to control playback.",
                ephemeral=True,
            )
            return
        await self.picker_view.playback_view._dispatch(
            interaction,
            "queue_playlist_item_by_id",
            self.values[0],
        )


def _filter_queue_playlist_items(playlist_items, query):
    """Filter indexed VLC playlist entries while preserving their original positions."""
    query_tokens = re.findall(r"\w+", str(query or "").casefold())
    if not query_tokens:
        return list(playlist_items)

    query_text = " ".join(query_tokens)
    matches = []
    for position, item in playlist_items:
        searchable = " ".join(
            str(item.get(field) or "")
            for field in ("name", "uri")
        ).casefold()
        searchable_tokens = re.findall(r"\w+", searchable)
        searchable_text = " ".join(searchable_tokens)
        if query_text in searchable_text or all(
            token in searchable_tokens for token in query_tokens
        ):
            matches.append((position, item))
    return matches


class QueuePlaylistSearchModal(discord.ui.Modal, title="Search VLC playlist"):
    """Collect a private playlist search without adding permanent panel buttons."""

    def __init__(self, picker_view):
        super().__init__(timeout=120)
        self.picker_view = picker_view
        self.query = discord.ui.TextInput(
            label="Title or filename",
            placeholder="Search the VLC playlist",
            required=False,
            max_length=100,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.picker_view._authorized(interaction):
            return
        await self.picker_view.apply_search(
            interaction,
            str(self.query.value or "").strip(),
        )


class QueuePlaylistView(discord.ui.View):
    """Private, bounded playlist picker with search across the loaded playlist."""

    page_size = 25

    def __init__(self, playback_view, playlist_items, page=0, requester_id=None):
        super().__init__(timeout=120)
        self.playback_view = playback_view
        self.requester_id = requester_id
        self.all_playlist_items = [
            (position, item)
            for position, item in enumerate(playlist_items, 1)
            if item.get("id")
        ]
        self.search_query = ""
        self.playlist_items = list(self.all_playlist_items)
        self.page = min(max(0, page), self.page_count - 1)
        self._refresh_select()
        self._update_navigation()

    @property
    def page_count(self):
        return max(1, (len(self.playlist_items) + self.page_size - 1) // self.page_size)

    def _page_content(self):
        if not self.playlist_items:
            return f'No playlist items matched "{self.search_query}". Use Search to try another query.'
        if self.search_query:
            return (
                f'Search results for **{self.search_query}** '
                f'({len(self.playlist_items)} matches). Choose an item to add to the soft queue.'
            )
        return f"Choose a playlist item to add to the soft queue ({len(self.playlist_items)} items)."

    async def _authorized(self, interaction):
        if self.requester_id is not None and interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "That playlist picker belongs to someone else.",
                ephemeral=True,
            )
            return False
        if not _member_has_allowed_role(interaction.user, Config.ALLOWED_ROLES):
            await interaction.response.send_message(
                "You don't have a role permitted to control playback.",
                ephemeral=True,
            )
            return False
        return True

    def _refresh_select(self):
        current_select = next(
            (child for child in self.children if isinstance(child, QueuePlaylistSelect)),
            None,
        )
        if current_select is not None:
            self.remove_item(current_select)
        if self.playlist_items:
            self.add_item(QueuePlaylistSelect(self))

    def _update_navigation(self):
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.page_count - 1
        self.page_button.label = f"Page {self.page + 1}/{self.page_count}"

    async def apply_search(self, interaction, query):
        self.search_query = query
        self.playlist_items = _filter_queue_playlist_items(
            self.all_playlist_items,
            query,
        )
        self.page = 0
        self._refresh_select()
        self._update_navigation()
        await interaction.response.edit_message(
            content=self._page_content(),
            view=self,
        )

    async def _change_page(self, interaction, offset):
        self.page = min(max(0, self.page + offset), self.page_count - 1)
        self._refresh_select()
        self._update_navigation()
        await interaction.response.edit_message(
            content=self._page_content(),
            view=self,
        )

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def previous_button(self, interaction, button):
        await self._change_page(interaction, -1)

    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.secondary, row=1, disabled=True)
    async def page_button(self, interaction, button):
        await interaction.response.defer()

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction, button):
        await self._change_page(interaction, 1)

    @discord.ui.button(label="Search", emoji="🔎", style=discord.ButtonStyle.primary, row=1)
    async def search_button(self, interaction, button):
        await interaction.response.send_modal(QueuePlaylistSearchModal(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self._authorized(interaction)


class QueueManageSelect(discord.ui.Select):
    """Select a soft-queue entry from the current page for removal or reordering."""

    def __init__(self, queue_view):
        self.queue_view = queue_view
        start = queue_view.page * queue_view.page_size
        page_items = queue_view.ordered_items[start:start + queue_view.page_size]
        options = [
            discord.SelectOption(
                label=f"#{info.get('queue_order', '?')} - "
                f"{(queue_view.playlist_names.get(item_id) or info.get('item_name') or 'Unknown')[:85]}",
                value=str(item_id),
                description=f"Queue position {info.get('queue_order', position)}",
            )
            for position, (item_id, info) in enumerate(page_items, start + 1)
        ]
        super().__init__(
            placeholder=f"Choose a queued track (page {queue_view.page + 1}/{queue_view.page_count})",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.queue_view.selected_queue_item_id = self.values[0]
        selected_order = self.queue_view.queue_orders[self.values[0]]
        self.queue_view.remove_selected_button.disabled = False
        self.queue_view.move_earlier_button.disabled = selected_order <= 1
        self.queue_view.move_later_button.disabled = (
            selected_order >= self.queue_view.total_items
        )
        await interaction.response.edit_message(
            content=(
                f"Selected queue item **#{selected_order}**. "
                "Remove it or move it one position earlier or later."
            ),
            view=self.queue_view,
        )


class QueueManageView(discord.ui.View):
    """Private queue manager using the controller's serialized queue operations."""

    page_size = 25

    def __init__(self, cog: PlaybackCommands, queue_status, playlist, page=0):
        super().__init__(timeout=120)
        self.cog = cog
        self.selected_queue_item_id = None
        queued_items = queue_status.get("queued_items", {})
        self.ordered_items = sorted(
            queued_items.items(),
            key=lambda entry: entry[1].get("queue_order", 0),
        )
        self.total_items = len(self.ordered_items)
        self.page_count = max(1, (self.total_items + self.page_size - 1) // self.page_size)
        self.page = min(max(0, page), self.page_count - 1)
        live_names = {
            str(item.get("id")): item.get("name", "Unknown")
            for item in (playlist.findall(".//leaf") if playlist is not None else [])
            if item.get("id") is not None
        }
        self.playlist_names = {
            queue_id: live_names.get(
                str(info.get("playlist_item_id", info.get("item_id", queue_id))),
                info.get("item_name", "Unknown"),
            )
            for queue_id, info in queued_items.items()
        }
        self.queue_orders = {
            item_id: info.get("queue_order", position)
            for position, (item_id, info) in enumerate(self.ordered_items, 1)
        }
        self.add_item(QueueManageSelect(self))
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.page_count - 1
        self.page_button.label = f"Page {self.page + 1}/{self.page_count}"
        self.remove_selected_button.disabled = True
        self.move_earlier_button.disabled = True
        self.move_later_button.disabled = True

    async def _change_page(self, interaction, offset):
        self.page = min(max(0, self.page + offset), self.page_count - 1)
        self.selected_queue_item_id = None
        current_select = next(
            child for child in self.children
            if isinstance(child, QueueManageSelect)
        )
        self.remove_item(current_select)
        self.add_item(QueueManageSelect(self))
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.page_count - 1
        self.page_button.label = f"Page {self.page + 1}/{self.page_count}"
        self.remove_selected_button.disabled = True
        self.move_earlier_button.disabled = True
        self.move_later_button.disabled = True
        await interaction.response.edit_message(
            content="Select a queued track to remove or move earlier/later.",
            view=self,
        )

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def previous_button(self, interaction, button):
        await self._change_page(interaction, -1)

    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.secondary, row=1, disabled=True)
    async def page_button(self, interaction, button):
        await interaction.response.defer()

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction, button):
        await self._change_page(interaction, 1)

    @discord.ui.button(
        label="Clear Queue",
        emoji="🧹",
        style=discord.ButtonStyle.danger,
        row=3,
    )
    async def clear_queue_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_message(
            "Clear all soft-queue tracking and shuffle-restoration entries? "
            "Items already in the VLC playlist will remain in place.",
            view=QueueClearConfirmView(self.cog),
            ephemeral=True,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _member_has_allowed_role(interaction.user, Config.ALLOWED_ROLES):
            await interaction.response.send_message(
                "You don't have a role permitted to control playback.",
                ephemeral=True,
            )
            return False
        return True

    async def _apply_queue_change(self, interaction: discord.Interaction, operation: str):
        if self.selected_queue_item_id is None:
            await interaction.response.send_message(
                "Choose a queued track first.",
                ephemeral=True,
            )
            return

        allowed, reason = await self.cog._playback_guard(
            interaction.user,
            presence_only=True,
        )
        if not allowed:
            await interaction.response.send_message(reason, ephemeral=True)
            return

        await interaction.response.defer()
        try:
            if operation == "remove":
                result = await self.cog.vlc.run_async(
                    "remove_from_queue_by_id",
                    self.selected_queue_item_id,
                    **self.cog._scheduled_media_protection(),
                )
            else:
                result = await self.cog.vlc.run_async(
                    "move_queued_item_by_id",
                    self.selected_queue_item_id,
                    operation,
                )

            if not result.get("success"):
                message = result.get("error", "The queue could not be updated.")
            elif operation == "remove":
                message = f"Removed **{result.get('item_name', 'Unknown')}** from the queue."
            else:
                message = (
                    f"Moved **{result.get('item_name', 'Unknown')}** "
                    f"to queue position #{result.get('queue_order')}."
                )

            queue_status, playlist = await self.cog.vlc.run_async("get_queue_snapshot")
            if not queue_status.get("queued_items"):
                await interaction.message.edit(
                    content=f"{message}\nThe soft queue is now empty.",
                    view=None,
                )
                return

            await interaction.message.edit(
                content=f"{message}\nSelect another queued track to manage.",
                view=QueueManageView(self.cog, queue_status, playlist, page=self.page),
            )
        except Exception:
            self.cog.logger.exception("Could not update the soft queue from the playback panel")
            await interaction.message.edit(
                content="The queue could not be updated. Please try again.",
                view=self,
            )

    @discord.ui.button(
        label="Remove",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        row=2,
        disabled=True,
    )
    async def remove_selected_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self._apply_queue_change(interaction, "remove")

    @discord.ui.button(
        label="Move Earlier",
        emoji="⬆️",
        style=discord.ButtonStyle.secondary,
        row=2,
        disabled=True,
    )
    async def move_earlier_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self._apply_queue_change(interaction, "up")

    @discord.ui.button(
        label="Move Later",
        emoji="⬇️",
        style=discord.ButtonStyle.secondary,
        row=2,
        disabled=True,
    )
    async def move_later_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self._apply_queue_change(interaction, "down")


class QueueClearConfirmView(discord.ui.View):
    """Explicit confirmation for clearing soft-queue tracking."""

    def __init__(self, cog: PlaybackCommands):
        super().__init__(timeout=30)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _member_has_allowed_role(interaction.user, Config.ALLOWED_ROLES):
            await interaction.response.send_message(
                "You don't have a role permitted to control playback.",
                ephemeral=True,
            )
            return False
        return True

    async def confirm_clear(self, interaction: discord.Interaction):
        allowed, reason = await self.cog._playback_guard(
            interaction.user,
            presence_only=True,
        )
        if not allowed:
            await interaction.response.send_message(reason, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.cog.vlc.run_async(
                "clear_queue",
                **self.cog._scheduled_media_protection(),
            )
            if not result.get("success"):
                raise RuntimeError(
                    f"Queue cleanup incomplete: {result.get('failed_item_ids', [])}"
                )
            preserved_count = len(result.get("preserved_item_ids", []))
            preserved_note = (
                f" Preserved {preserved_count} current or scheduled playlist item(s)."
                if preserved_count else ""
            )
            await interaction.edit_original_response(
                content=f"The soft queue has been cleared and safe VLC entries removed.{preserved_note}",
                view=None,
            )
        except Exception:
            self.cog.logger.exception("Could not clear the soft queue from the playback panel")
            await interaction.edit_original_response(
                content="The soft queue could not be cleared. Please try again.",
                view=None,
            )

    @discord.ui.button(
        label="Confirm Clear",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self.confirm_clear(interaction)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.edit_message(
            content="Queue clear cancelled.",
            view=None,
        )


class PlaybackManagementSelect(discord.ui.Select):
    """Compact persistent menu for less-frequent playback management actions."""

    def __init__(self, view: "PlaybackControlView"):
        self.playback_view = view
        super().__init__(
            placeholder="Management and playback options",
            min_values=1,
            max_values=1,
            row=2,
            custom_id="ctrlvee:panel:management",
            options=[
                discord.SelectOption(label="View Queue", value="view_queue", emoji="📋"),
                discord.SelectOption(label="Manage Queue", value="manage_queue", emoji="🛠️"),
                discord.SelectOption(label="Add Media to Queue", value="add_to_queue", emoji="➕"),
                discord.SelectOption(label="Toggle Shuffle", value="shuffle", emoji="🔀"),
                discord.SelectOption(label="Advanced Options", value="advanced", emoji="⚙️"),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.playback_view._handle_management_action(interaction, self.values[0])


class PlaybackControlView(discord.ui.View):
    """Persistent interactive panel that delegates to existing guarded commands."""

    def __init__(self, cog: PlaybackCommands):
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(PlaybackManagementSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not _member_has_allowed_role(interaction.user, Config.ALLOWED_ROLES):
            await interaction.response.send_message(
                "You don't have a role permitted to control playback.",
                ephemeral=True,
            )
            return False
        return True

    async def _handle_management_action(
        self,
        interaction: discord.Interaction,
        action: str,
    ) -> None:
        handlers = {
            "view_queue": lambda: self._dispatch(interaction, "queue_status"),
            "manage_queue": lambda: self.manage_queue_button(interaction, None),
            "add_to_queue": lambda: self.add_to_queue_button(interaction, None),
            "shuffle": lambda: self._dispatch(interaction, "shuffle_toggle"),
            "advanced": lambda: self.advanced_options_button(interaction, None),
        }
        handler = handlers.get(action)
        if handler is None:
            await interaction.response.send_message(
                "That management option is not available.",
                ephemeral=True,
            )
            return
        await handler()

    async def _dispatch(
        self,
        interaction: discord.Interaction,
        command_name: str,
        *args,
        refresh_panel: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        class InteractionContext:
            def __init__(self, current_interaction):
                self.author = current_interaction.user
                self.guild = current_interaction.guild
                self.channel = current_interaction.channel
                self.message = current_interaction.message
                self._interaction = current_interaction

            async def send(self, content=None, *, embed=None, **kwargs):
                kwargs.pop("ephemeral", None)
                return await self._interaction.followup.send(
                    content=content,
                    embed=embed,
                    ephemeral=True,
                    **kwargs,
                )

        command = getattr(self.cog, command_name)
        try:
            callback = getattr(command, "callback", None)
            if callback is not None:
                await callback(self.cog, InteractionContext(interaction), *args)
            else:
                await command(InteractionContext(interaction), *args)
        except Exception:
            self.cog.logger.exception("Playback panel action failed: %s", command_name)
            await interaction.followup.send(
                "That playback action failed. Check the bot logs for details.",
                ephemeral=True,
            )
            return

        if refresh_panel and interaction.message is not None:
            try:
                await interaction.message.edit(
                    embed=await self.cog._playback_panel_embed(),
                    view=self,
                )
            except discord.HTTPException:
                self.cog.logger.debug("Could not refresh playback panel message")

    @discord.ui.button(label="Previous", emoji="⏮️", style=discord.ButtonStyle.secondary, row=0, custom_id="ctrlvee:panel:previous")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._dispatch(interaction, "previous_track")

    @discord.ui.button(label="Play", emoji="▶️", style=discord.ButtonStyle.success, row=0, custom_id="ctrlvee:panel:play")
    async def play_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._dispatch(interaction, "play")

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary, row=0, custom_id="ctrlvee:panel:pause")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._dispatch(interaction, "pause")

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0, custom_id="ctrlvee:panel:stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._dispatch(interaction, "stop")

    @discord.ui.button(label="Next", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0, custom_id="ctrlvee:panel:next")
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._dispatch(interaction, "next_track")

    @discord.ui.button(label="Rewind 10s", emoji="⏪", style=discord.ButtonStyle.secondary, row=1, custom_id="ctrlvee:panel:rewind-10s")
    async def rewind_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._dispatch(interaction, "rewind", 10)

    @discord.ui.button(label="Forward 10s", emoji="⏩", style=discord.ButtonStyle.secondary, row=1, custom_id="ctrlvee:panel:forward-10s")
    async def forward_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._dispatch(interaction, "forward", 10)

    @discord.ui.button(label="Rewind 30s", emoji="⏪", style=discord.ButtonStyle.secondary, row=1, custom_id="ctrlvee:panel:rewind-30s")
    async def rewind_thirty_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._dispatch(interaction, "rewind", 30)

    @discord.ui.button(label="Forward 30s", emoji="⏩", style=discord.ButtonStyle.secondary, row=1, custom_id="ctrlvee:panel:forward-30s")
    async def forward_thirty_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._dispatch(interaction, "forward", 30)

    async def advanced_options_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            audio_tracks = await self.cog.vlc.run_async("get_audio_tracks") or []
            subtitle_tracks = await self.cog.vlc.run_async("get_subtitle_tracks") or []
            view = PlaybackAdvancedView(self, audio_tracks, subtitle_tracks)
            sections = []
            if audio_tracks:
                sections.append("Audio tracks are available below.")
            else:
                sections.append("VLC did not report any audio tracks for this media.")
            if subtitle_tracks:
                sections.append("Subtitle tracks and navigation are available below.")
            else:
                sections.append("VLC did not report any subtitle tracks for this media.")
            sections.append("Playback speed is always available.")
            await interaction.followup.send(
                "\n".join(sections),
                view=view,
                ephemeral=True,
            )
        except Exception:
            self.cog.logger.exception("Could not open advanced playback options")
            await interaction.followup.send(
                "Advanced playback options are currently unavailable.",
                ephemeral=True,
            )

    async def manage_queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            queue_status, playlist = await self.cog.vlc.run_async("get_queue_snapshot")
            if not queue_status.get("queued_items"):
                await interaction.followup.send(
                    "There are no queued tracks to manage.",
                    ephemeral=True,
                )
                return

            ordered_items = sorted(
                queue_status["queued_items"].items(),
                key=lambda entry: entry[1].get("queue_order", 0),
            )
            await interaction.followup.send(
                "Select a queued track to remove or move earlier/later.",
                view=QueueManageView(self.cog, queue_status, playlist),
                ephemeral=True,
            )
        except Exception:
            self.cog.logger.exception("Could not open the queue manager")
            await interaction.followup.send(
                "The soft queue is currently unavailable.",
                ephemeral=True,
            )

    async def add_to_queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            playlist = await self.cog.vlc.run_async("get_playlist")
            playlist_items = playlist.findall(".//leaf") if playlist is not None else []
            if not playlist_items:
                await interaction.followup.send("The VLC playlist is empty or unavailable.", ephemeral=True)
                return

            await interaction.followup.send(
                "Choose a playlist item to add to the soft queue.",
                view=QueuePlaylistView(
                    self,
                    playlist_items,
                    requester_id=interaction.user.id,
                ),
                ephemeral=True,
            )
        except Exception:
            self.cog.logger.exception("Could not open the queue picker")
            await interaction.followup.send(
                "The playlist is currently unavailable.",
                ephemeral=True,
            )

