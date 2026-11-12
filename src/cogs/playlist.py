from typing import Optional, List, Tuple
import asyncio
import discord
from discord.ext import commands
import logging
import re
from pathlib import Path
from ..utils.media_utils import MediaUtils
from ..config import Config
from ..utils.command_utils import format_cmd_inline

logger = logging.getLogger(__name__)

class PlaylistView(discord.ui.View):
    def __init__(self, items, items_per_page: Optional[int] = None):
        super().__init__(timeout=300)  # 5 minute timeout
        self.items = items
        # Default to Config if not provided
        self.items_per_page = max(1, items_per_page or Config.ITEMS_PER_PAGE)
        self.max_chars_per_page = 1000
        self.current_page = 1
        self.pages = self._build_pages()
        self.total_pages = max(1, len(self.pages))

    def _build_pages(self) -> List[List[str]]:
        """Paginate playlist lines by item-count and field character limits."""
        pages: List[List[str]] = []
        current_page: List[str] = []
        current_chars = 0

        for i, item in enumerate(self.items, start=1):
            name = item.get('name', '')
            icon = MediaUtils.get_media_icon(name)
            basename = MediaUtils.clean_filename_for_display(name)
            line = f"{icon}`{i}` {basename}"

            # Ensure one very long title still fits safely in a page.
            if len(line) > self.max_chars_per_page:
                keep = max(0, self.max_chars_per_page - len(f"{icon}`{i}` ..."))
                line = f"{icon}`{i}` {basename[:keep]}..."

            projected = current_chars + len(line) + (1 if current_page else 0)
            would_overflow_chars = projected > self.max_chars_per_page
            would_overflow_items = len(current_page) >= self.items_per_page

            if current_page and (would_overflow_chars or would_overflow_items):
                pages.append(current_page)
                current_page = []
                current_chars = 0

            current_page.append(line)
            current_chars += len(line) + (1 if len(current_page) > 1 else 0)

        if current_page:
            pages.append(current_page)

        return pages

    def build_embed(self) -> discord.Embed:
        page_idx = self.current_page - 1
        page_lines = self.pages[page_idx] if self.pages else []

        # Compute global displayed range for this page.
        start = sum(len(p) for p in self.pages[:page_idx]) + 1 if page_lines else 0
        end = start + len(page_lines) - 1 if page_lines else 0

        embed = discord.Embed(
            title="VLC Playlist",
            color=discord.Color.blue()
        )
        embed.add_field(
            name=f"📋 Page {self.current_page}/{self.total_pages}",
            value="\n".join(page_lines) if page_lines else "No items in playlist",
            inline=False
        )
        if start and end:
            embed.set_footer(text=f"📑 Showing items {start}-{end} of {len(self.items)}")
        else:
            embed.set_footer(text=f"📑 Showing items 0-0 of {len(self.items)}")
        return embed

    @discord.ui.button(label="⏮️", style=discord.ButtonStyle.secondary)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 1
        await self.update_message(interaction)

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.primary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(1, self.current_page - 1)
        await self.update_message(interaction)

    @discord.ui.button(label="📖", style=discord.ButtonStyle.success)
    async def goto_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = PageSelectModal(self.total_pages, self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min(self.total_pages, self.current_page + 1)
        await self.update_message(interaction)

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.secondary)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = self.total_pages
        await self.update_message(interaction)

    async def update_message(self, interaction: discord.Interaction):
        """Update the playlist view message"""
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

class PageSelectModal(discord.ui.Modal, title='Go to Page'):
    def __init__(self, total_pages: int, view: PlaylistView):
        super().__init__()
        self.total_pages = total_pages
        self.view = view
        
        self.page_input = discord.ui.TextInput(
            label=f'Enter page number (1-{total_pages})',
            placeholder='Enter a number',
            min_length=1,
            max_length=len(str(total_pages)),
            required=True
        )
        self.add_item(self.page_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            page = int(self.page_input.value)
            if 1 <= page <= self.total_pages:
                self.view.current_page = page
                await self.view.update_message(interaction)
            else:
                await interaction.response.send_message(
                    f"Please enter a valid page number between 1 and {self.total_pages}",
                    ephemeral=True
                )
        except ValueError:
            await interaction.response.send_message(
                "Please enter a valid number",
                ephemeral=True
            )


class SearchResultsView(discord.ui.View):
    def __init__(self, query: str, pages: List[List[str]], total_matches: int):
        super().__init__(timeout=300)
        self.query = query
        self.pages = pages
        self.total_matches = total_matches
        self.current_page = 1
        self.total_pages = max(1, len(pages))

    @discord.ui.button(label="⏮️", style=discord.ButtonStyle.secondary)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 1
        await self.update_message(interaction)

    @discord.ui.button(label="◀️", style=discord.ButtonStyle.primary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(1, self.current_page - 1)
        await self.update_message(interaction)

    @discord.ui.button(label="📖", style=discord.ButtonStyle.success)
    async def goto_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = PageSelectModal(self.total_pages, self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min(self.total_pages, self.current_page + 1)
        await self.update_message(interaction)

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.secondary)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = self.total_pages
        await self.update_message(interaction)

    async def update_message(self, interaction: discord.Interaction):
        page_idx = self.current_page - 1
        page_lines = self.pages[page_idx] if self.pages else []

        # Compute item range shown in this page for clearer navigation context.
        start = sum(len(p) for p in self.pages[:page_idx]) + 1 if page_lines else 0
        end = start + len(page_lines) - 1 if page_lines else 0

        embed = discord.Embed(
            title="🔍 Search Results",
            description=f"Search query: '{self.query}'",
            color=discord.Color.blue()
        )
        embed.add_field(
            name=f"Found {self.total_matches} matches • Page {self.current_page}/{self.total_pages}",
            value="\n".join(page_lines) if page_lines else "No matches on this page",
            inline=False
        )
        if start and end:
            footer = f"💡 Use {format_cmd_inline('play_num <number>')} to play an item • Showing {start}-{end} of {self.total_matches}"
        else:
            footer = f"💡 Use {format_cmd_inline('play_num <number>')} to play an item"
        embed.set_footer(text=footer)

        await interaction.response.edit_message(embed=embed, view=self)

class PlaySearchView(discord.ui.View):
    """Requester-scoped, paginated button picker for ambiguous playlist searches."""

    PAGE_SIZE = 5
    MAX_RESULTS = 25

    def __init__(self, cog, requester_id: int, results: List[Tuple[int, dict]]):
        super().__init__(timeout=120)
        self.cog = cog
        self.requester_id = requester_id
        self.results = results[:self.MAX_RESULTS]
        self.page = 0
        self.message = None
        self.total_matches = len(results)
        self._refresh_items()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.results) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def _page_results(self, page: int) -> List[Tuple[int, dict]]:
        start = page * self.PAGE_SIZE
        return self.results[start:start + self.PAGE_SIZE]

    def _build_embed(self) -> discord.Embed:
        lines = []
        for playlist_num, item in self._page_results(self.page):
            name = MediaUtils.clean_filename_for_display(item.get("name", ""), max_length=100)
            lines.append(f"#{playlist_num} {name}")
        embed = discord.Embed(
            title=f"Choose a match ({self.total_matches} total)",
            description="\n".join(lines) or "No results on this page.",
            color=discord.Color.blurple(),
        )
        if self.total_matches > len(self.results):
            embed.description += (
                f"\n\nShowing the top {len(self.results)} matches. "
                "Refine your search to find other results."
            )
        if self.page_count > 1:
            embed.set_footer(text=f"Page {self.page + 1} of {self.page_count}")
        return embed

    def _refresh_items(self) -> None:
        self.clear_items()
        for playlist_num, item in self._page_results(self.page):
            title = MediaUtils.clean_filename_for_display(item.get("name", ""), max_length=68)
            button = discord.ui.Button(
                label=f"#{playlist_num} {title}"[:80],
                style=discord.ButtonStyle.primary,
                row=0,
            )

            async def choose(interaction: discord.Interaction, number=playlist_num):
                await self._select(interaction, number)

            button.callback = choose
            self.add_item(button)

        if self.page_count > 1:
            previous = discord.ui.Button(
                label="Previous",
                style=discord.ButtonStyle.secondary,
                disabled=self.page == 0,
                row=1,
            )
            next_page = discord.ui.Button(
                label="Next",
                style=discord.ButtonStyle.secondary,
                disabled=self.page >= self.page_count - 1,
                row=1,
            )
            previous.callback = self._previous
            next_page.callback = self._next
            self.add_item(previous)
            self.add_item(next_page)

        cancel = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            row=1,
        )
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "That picker belongs to someone else.", ephemeral=True
            )
            return False
        return True

    async def _previous(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._refresh_items()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        self._refresh_items()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Playback selection cancelled.", embed=None, view=None
        )

    async def _select(self, interaction: discord.Interaction, playlist_num: int) -> None:
        pair = next((result for result in self.results if result[0] == playlist_num), None)
        if pair is None:
            await interaction.response.send_message(
                "That result is no longer available.", ephemeral=True
            )
            return
        _, item = pair

        playback_cog = self.cog.bot.get_cog("PlaybackCommands")
        if playback_cog:
            allowed, reason = await playback_cog._playback_guard(interaction.user)
            if not allowed:
                await interaction.response.send_message(reason, ephemeral=True)
                return

        item_id = item.get("id")
        if not item_id:
            await interaction.response.send_message(
                "That result no longer has a valid VLC item ID.", ephemeral=True
            )
            return

        self.stop()
        await interaction.response.edit_message(
            content=f"Loading item #{playlist_num}...", embed=None, view=None
        )
        confirmation = await self.cog._play_and_confirm_item(
            item_id, interaction.user
        )
        if not confirmation.get("success"):
            await interaction.followup.send(
                "VLC did not confirm that selection became the current playing item.",
                ephemeral=True,
            )
            return

        await self.cog._send_search_metadata(
            interaction.followup.send,
            confirmation["item"],
            confirmation["position"],
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class PlaylistCommands(commands.Cog):
    def __init__(self, bot: commands.Bot, vlc_controller, tmdb_service, watch_service):
        self.bot = bot
        self.vlc = vlc_controller
        self.tmdb = tmdb_service
        self.watch_service = watch_service

    def _media_library(self):
        return getattr(self.watch_service, "media_library", None)

    async def _queue_library_paths(self, paths: List[str]) -> dict:
        library = self._media_library()
        if library is None or any(not library.is_available(path) for path in paths):
            return {"success": False, "error": "one_or_more_media_files_are_missing_or_outside_the_library"}
        entries = [
            {"media_uri": Path(path).resolve().as_uri(), "item_name": Path(path).name}
            for path in paths
        ]
        return await self.vlc.run_async("queue_media_batch", entries)

    @commands.command(name="media_search")
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def media_search(self, ctx: commands.Context, *, query: str):
        """Search all indexed media files on the configured NAS mounts."""
        library = self._media_library()
        if library is None or not library.roots:
            await ctx.send("The media library is not configured. Set MEDIA_LIBRARY_ROOTS to the mounted media paths.")
            return
        status = library.status()
        if status["indexing"] and status["indexed"] == 0:
            await ctx.send("The media library is still building its initial index. Try again when indexing finishes.")
            return
        results = await asyncio.to_thread(library.search, query, 20)
        seasons = await asyncio.to_thread(library.find_seasons, query)
        if not results:
            suffix = " The initial index may still be running." if status["indexing"] else ""
            await ctx.send(f"No indexed media matched **{query}**.{suffix}")
            return
        lines = []
        for index, item in enumerate(results, start=1):
            kind = "Episode" if item["media_type"] == "episode" else "Audio" if item["media_type"] == "audio" else "Video"
            lines.append(f"{index}. **{kind}** — {item['name']}\n<{item['path']}>")
        embed = discord.Embed(
            title=f"Media Library Search: {query}",
            description="\n\n".join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        footer = f"Showing up to {len(results)} matches from the NAS index. Use queue_media with a unique title or full path."
        if status["indexing"]:
            footer += " Initial indexing is still running; results may be incomplete."
        if seasons:
            season_summary = ", ".join(
                f"Season {item['season']} ({item['episode_count']} episodes)"
                for item in seasons
            )
            embed.add_field(
                name="Available TV seasons",
                value=(season_summary[:1000] + ("…" if len(season_summary) > 1000 else ""))
                      + "\nQueue one with queue_season <number> <series name>.",
                inline=False,
            )
        embed.set_footer(text=footer)
        await ctx.send(embed=embed)

    @commands.command(name="queue_media")
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def queue_media(self, ctx: commands.Context, *, query: str):
        """Queue one uniquely matched file from the indexed NAS media library."""
        library = self._media_library()
        if library is None or not library.roots:
            await ctx.send("The media library is not configured. Set MEDIA_LIBRARY_ROOTS to the mounted media paths.")
            return
        results = await asyncio.to_thread(library.search, query, 100)
        exact = [
            item for item in results
            if item["path"].casefold() == query.casefold()
            or item["name"].casefold() == query.casefold()
        ]
        selected = exact if exact else results
        if len(selected) != 1:
            if not selected:
                await ctx.send(f"No indexed media matched **{query}**.")
            else:
                examples = "\n".join(f"• {item['path']}" for item in selected[:8])
                await ctx.send(
                    f"That search is ambiguous ({len(selected)} matches). Refine it or use the full path:\n{examples}"
                )
            return
        item = selected[0]
        result = await self._queue_library_paths([item["path"]])
        if not result.get("success"):
            await ctx.send(f"Could not queue that media file: {result.get('error', 'unknown error')}")
            return
        await ctx.send(f"Added **{item['name']}** to the queue at position {result['total_queued']}.")

    @commands.command(name="queue_season")
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def queue_season(self, ctx: commands.Context, season: int, *, series: str):
        """Queue all uniquely identified episodes in a season, in episode order."""
        if season < 0:
            await ctx.send("Season number must be zero or greater.")
            return
        library = self._media_library()
        if library is None or not library.roots:
            await ctx.send("The media library is not configured. Set MEDIA_LIBRARY_ROOTS to the mounted media paths.")
            return
        episodes = await asyncio.to_thread(library.find_season, series, season, 2000)
        if not episodes:
            await ctx.send(f"No indexed episodes found for **{series}**, season {season}.")
            return
        if len(episodes) >= 2000:
            await ctx.send("This season returned the library safety limit of 2,000 files. Narrow the series search before queueing.")
            return

        episode_versions = {}
        for item in episodes:
            episode_versions.setdefault(item["episode"], []).append(item)
        duplicates = {
            number: items for number, items in episode_versions.items()
            if len(items) > 1
        }
        if duplicates:
            details = []
            for number, items in sorted(duplicates.items()):
                details.append(f"E{number:02d}: " + " | ".join(item["name"] for item in items[:3]))
            await ctx.send(
                "Multiple files exist for the same episode number, so I did not queue an ambiguous season. "
                "Choose preferred versions with queue_media and the full path:\n"
                + "\n".join(details[:8])
            )
            return

        paths = [item["path"] for item in episodes]
        result = await self._queue_library_paths(paths)
        if not result.get("success"):
            await ctx.send(f"Could not queue the season: {result.get('error', 'unknown error')}")
            return
        preview = "\n".join(
            f"{item['episode']:02d}. {item['name']}" for item in episodes[:10]
        )
        if len(episodes) > 10:
            preview += f"\n… and {len(episodes) - 10} more"
        await ctx.send(
            f"Queued **{len(episodes)} episodes** of **{series} — Season {season}** "
            f"in episode order (queue positions {result['total_queued'] - len(episodes) + 1}–{result['total_queued']}).\n"
            + preview
        )

    async def _play_and_confirm_item(self, item_id: str, member=None) -> dict:
        """Play a live playlist item and confirm it is current before publishing ownership."""
        playback_cog = self.bot.get_cog("PlaybackCommands")
        selected = await self.vlc.run_async("play_item_and_capture", item_id)
        if not selected.get("success"):
            return selected

        if playback_cog:
            playback_cog._command_initiated_item_id = str(item_id)

        confirmation = None
        for attempt in range(5):
            await asyncio.sleep(1)
            confirmation = await self.vlc.run_async(
                "get_current_item_if_unchanged", item_id
            )
            if not confirmation.get("success"):
                if confirmation.get("reason") == "selection_changed":
                    break
                continue
            if confirmation.get("state") == "playing":
                if playback_cog and member is not None:
                    playback_cog._seat_playback_owner(member, item_id)
                return confirmation

        if playback_cog and playback_cog._command_initiated_item_id == str(item_id):
            playback_cog._command_initiated_item_id = None
        if confirmation and confirmation.get("reason") == "selection_changed":
            return confirmation
        return {
            "success": False,
            "reason": "playback_not_confirmed",
            "item_id": str(item_id),
        }

    async def _get_playlist_items(self) -> List[dict]:
        """Get current playlist items"""
        playlist_xml = await self.vlc.run_async("get_playlist")
        if playlist_xml is not None:
            return playlist_xml.findall('.//leaf')
        return []

    async def _find_item_by_id(self, item_id: str) -> Tuple[Optional[dict], int]:
        """Find an item and its index in the playlist by ID"""
        items = await self._get_playlist_items()
        for i, item in enumerate(items):
            if item.get('id') == item_id:
                return item, i
        return None, -1

    def _normalize_search_text(self, text: str) -> Tuple[str, str, List[str]]:
        """Normalize text for fuzzy search matching.

        Returns:
            Tuple of (normalized_with_spaces, compact_alnum, tokens)
        """
        lowered = (text or '').lower()
        # Convert common separators to spaces first, then strip other punctuation.
        normalized = re.sub(r'[._\-]+', ' ', lowered)
        normalized = re.sub(r'[^a-z0-9]+', ' ', normalized)
        normalized = ' '.join(normalized.split())
        compact = normalized.replace(' ', '')
        tokens = normalized.split() if normalized else []
        return normalized, compact, tokens

    def _truncate_middle(self, text: str, max_len: int, min_tail: int = 22) -> str:
        """Truncate text from the middle while preserving the end details.

        This helps keep suffix details like `S01E08` visible in long media titles.
        """
        if max_len <= 0:
            return ""
        if len(text) <= max_len:
            return text
        if max_len <= 3:
            return "." * max_len

        # Keep a meaningful suffix (episode/season tags are often near the end).
        tail = min(min_tail, max_len - 3)
        head = max_len - 3 - tail

        # Ensure both sides remain visible even on tighter limits.
        if head < 8:
            head = min(8, max_len - 3)
            tail = max(0, max_len - 3 - head)

        return f"{text[:head]}...{text[-tail:]}"

    def _score_match(self, query_norm: str, query_compact: str, query_tokens: List[str], item_name: str) -> int:
        """Return a relevance score for query vs item name.

        Higher score means better match.
        """
        item_norm, item_compact, _ = self._normalize_search_text(item_name)
        if not item_norm:
            return 0

        score = 0

        # Strong exact matches
        if query_norm == item_norm:
            score += 1000
        if query_compact and query_compact == item_compact:
            score += 900

        # Strong substring matches
        if query_norm and query_norm in item_norm:
            score += 700
        if query_compact and query_compact in item_compact:
            score += 650

        # Token coverage and order
        if query_tokens:
            matched = sum(1 for token in query_tokens if token in item_norm)
            score += matched * 80
            if matched == len(query_tokens):
                score += 120

            # Prefer exact token sequence starts (prefix-like behavior)
            item_tokens = item_norm.split()
            if len(query_tokens) <= len(item_tokens) and item_tokens[:len(query_tokens)] == query_tokens:
                score += 160

        # Small preference for tighter titles (reduces noisy broad matches)
        try:
            score -= abs(len(item_compact) - len(query_compact))
        except Exception:
            pass

        return max(0, score)

    async def _search_items(self, query: str) -> List[Tuple[int, dict]]:
        """Search for items in the playlist
        
        Args:
            query: Search terms
            
        Returns:
            List of tuples (position, item) where position is 1-based index
        """
        items = await self._get_playlist_items()
        query_norm, query_compact, query_tokens = self._normalize_search_text(query)
        if not query_tokens:
            return []

        scored_results: List[Tuple[int, int, dict]] = []
        for i, item in enumerate(items):
            raw_name = item.get('name', '')
            item_norm, item_compact, _ = self._normalize_search_text(raw_name)

            # Accept multiple match strategies to handle spacing/punctuation variants.
            matches = (
                (query_norm and query_norm in item_norm) or
                (query_compact and query_compact in item_compact) or
                all(token in item_norm for token in query_tokens)
            )

            if matches:
                score = self._score_match(query_norm, query_compact, query_tokens, raw_name)
                scored_results.append((score, i + 1, item))

        # Rank by best score first; use playlist position as stable tiebreaker.
        scored_results.sort(key=lambda r: (-r[0], r[1]))

        return [(pos, itm) for _, pos, itm in scored_results]

    def _build_search_pages(self, results: List[Tuple[int, dict]]) -> List[List[str]]:
        """Paginate search lines while respecting Discord embed field limits.

        Pages are constrained by both max line count (`Config.ITEMS_PER_PAGE`) and
        max rendered characters to avoid hitting Discord's 1024-char field cap.
        """
        max_items_per_page = max(1, int(getattr(Config, 'ITEMS_PER_PAGE', 20)))
        max_chars_per_page = 1000

        pages: List[List[str]] = []
        current_page: List[str] = []
        current_chars = 0

        for playlist_num, item in results:
            name = item.get('name', '')
            icon = MediaUtils.get_media_icon(name)
            # Use a longer pre-clean limit for search results so episode suffixes
            # (e.g. S01E08) are not lost before page-level truncation is applied.
            basename = MediaUtils.clean_filename_for_display(name, max_length=240)
            prefix = f"{icon}`{playlist_num}` "
            max_basename_len = max(0, max_chars_per_page - len(prefix))
            display_name = self._truncate_middle(basename, max_basename_len)
            line = f"{prefix}{display_name}"

            projected = current_chars + len(line) + (1 if current_page else 0)
            would_overflow_chars = projected > max_chars_per_page
            would_overflow_items = len(current_page) >= max_items_per_page

            if current_page and (would_overflow_chars or would_overflow_items):
                pages.append(current_page)
                current_page = []
                current_chars = 0

            current_page.append(line)
            current_chars += len(line) + (1 if len(current_page) > 1 else 0)

        if current_page:
            pages.append(current_page)

        return pages

    @commands.command(name='search')
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def search_playlist(self, ctx: commands.Context, *, query: str):
        """Search for items in the playlist"""
        try:
            results = await self._search_items(query)
            
            # Create embed
            embed = discord.Embed(
                title="🔍 Search Results",
                description=f"Search query: '{query}'",
                color=discord.Color.blue()
            )
            
            if results:
                pages = self._build_search_pages(results)
                first_page = pages[0] if pages else []
                embed.add_field(
                    name=f"Found {len(results)} matches • Page 1/{max(1, len(pages))}",
                    value="\n".join(first_page) if first_page else "No matches found in the playlist",
                    inline=False
                )
                shown = len(first_page)
                if shown:
                    embed.set_footer(
                        text=(
                            f"💡 Use {format_cmd_inline('play_num <number>')} to play an item "
                            f"• Showing 1-{shown} of {len(results)}"
                        )
                    )
                else:
                    embed.set_footer(text=f"💡 Use {format_cmd_inline('play_num <number>')} to play an item")

                if len(pages) > 1:
                    view = SearchResultsView(query=query, pages=pages, total_matches=len(results))
                    await ctx.send(embed=embed, view=view)
                else:
                    await ctx.send(embed=embed)
            else:
                embed.add_field(
                    name="No Results",
                    value="No matches found in the playlist",
                    inline=False
                )
                await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"Error searching playlist: {e}")
            await ctx.send(f'Error searching playlist: {str(e)}')

    async def _send_search_metadata(self, send, item, playlist_num: int) -> None:
        """Send selected-item metadata without blocking the Discord event loop."""
        try:
            search_title, search_year = MediaUtils.parse_movie_filename(item.get("name", ""))
            movie_embed = await asyncio.to_thread(
                self.tmdb.get_movie_metadata,
                search_title,
                search_year,
                file_path=item.get("uri"),
            )
            edition_tag = MediaUtils.extract_edition_tag(item.get("name", ""))
            if movie_embed:
                if edition_tag:
                    movie_embed.add_field(name="Edition", value=edition_tag, inline=True)
                movie_embed.set_footer(text=f"Now Playing #{playlist_num}")
                await send(embed=movie_embed)
            else:
                name = item.get("name", "")
                icon = MediaUtils.get_media_icon(name)
                basename = MediaUtils.clean_filename_for_display(name)
                await send(f"Playing: {icon} #{playlist_num} {basename}")
        except Exception as e:
            logger.error("Error sending selected search metadata: %s", e)

    @commands.command(name='play_search')
    @commands.has_any_role(*Config.ALLOWED_ROLES)
    async def play_search(self, ctx: commands.Context, *, query: str):
        """Search the playlist and play a unique match or show an interactive picker."""
        try:
            results = await self._search_items(query)
            if not results:
                await ctx.send("No matches found in playlist")
                return

            if len(results) == 1:
                playlist_num, item = results[0]
                playback_cog = self.bot.get_cog("PlaybackCommands")
                if playback_cog:
                    allowed, reason = await playback_cog._playback_guard(ctx.author)
                    if not allowed:
                        await ctx.send(reason)
                        return
                item_id = item.get("id")
                if not item_id:
                    await ctx.send("Error: The selected item no longer has a valid VLC item ID")
                    return
                await ctx.send(f"Loading item #{playlist_num}...")
                confirmation = await self._play_and_confirm_item(item_id, ctx.author)
                if not confirmation.get("success"):
                    await ctx.send(
                        "VLC did not confirm that selection became the current playing item."
                    )
                    return
                await self._send_search_metadata(
                    ctx.send, confirmation["item"], confirmation["position"]
                )
                return

            view = PlaySearchView(self, ctx.author.id, results)
            embed = view._build_embed()
            message = await ctx.send(embed=embed, view=view)
            view.message = message
        except Exception as e:
            logger.error("Error in play_search: %s", e)
            await ctx.send(f"Error searching and playing: {str(e)}")

    @commands.command(name='list')
    async def list_playlist(self, ctx: commands.Context):
        """List items in the playlist with interactive navigation"""
        try:
            items = await self._get_playlist_items()
            if not items:
                await ctx.send('Playlist is empty')
                return

            view = PlaylistView(items, items_per_page=Config.ITEMS_PER_PAGE)
            embed = view.build_embed()
            
            # Add media library size to footer
            size_bytes = self.watch_service.get_total_media_size() if self.watch_service else 0
            def human_size(num):
                for unit in ['B','KB','MB','GB','TB']:
                    if num < 1024.0:
                        return f"{num:.2f} {unit}"
                    num /= 1024.0
                return f"{num:.2f} PB"
            # Preserve the page range and append media size.
            page_footer = embed.footer.text or ""
            embed.set_footer(text=f"{page_footer} | Media Library Size: {human_size(size_bytes)}")
            await ctx.send(embed=embed, view=view)
            
        except Exception as e:
            await ctx.send(f'Error listing playlist: {str(e)}')
