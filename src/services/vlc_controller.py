from typing import Optional, Dict, Any
import asyncio
import threading
import os
import json
import xml.etree.ElementTree as ET
import requests
import logging
import uuid

class VLCError(Exception):
    """Base exception for VLC controller errors"""
    pass

class VLCPlaylistError(VLCError):
    """Exception for VLC playlist-related errors"""
    pass

class VLCController:
    """Controller for VLC HTTP interface"""
    
    def __init__(self, host: Optional[str] = None, port: Optional[int] = None, 
                 password: Optional[str] = None, queue_backup_file: str = "queue_backup.json",
                 bot = None):
        """Initialize VLC controller
        
        Args:
            host: VLC HTTP interface host (defaults to config)
            port: VLC HTTP interface port (defaults to config)
            password: VLC HTTP interface password (defaults to config)
            queue_backup_file: Path to queue backup file for persistence
        """
        from ..config import Config
        self.host = host or Config.VLC_HOST
        self.port = port or Config.VLC_PORT
        self.password = password or Config.VLC_PASSWORD
        self.queue_backup_file = queue_backup_file
        self.logger = logging.getLogger(__name__)
        self.bot = bot
        self._async_operation_lock = asyncio.Lock()
        self._operation_thread_lock = threading.RLock()
        
        # Queue management state
        self._queued_items = {}  # item_id -> queue_info
        self._shuffle_restore_queue = []  # List of items that need shuffle restored after playing
        self._pending_queue_item_id = None
        self._pending_queue_started_at = None
        
        # Load queue state from backup file
        self._load_queue_backup()

    def _load_queue_backup(self):
        """Load and migrate queue state to independent queue-entry identities."""
        try:
            if not os.path.exists(self.queue_backup_file) or os.path.getsize(self.queue_backup_file) == 0:
                return
            with open(self.queue_backup_file, "r") as backup_file:
                backup_data = json.load(backup_file)

            if isinstance(backup_data, list):
                self.logger.info("Discarding obsolete list-based queue backup")
                self._save_queue_backup()
                return
            if not isinstance(backup_data, dict):
                return

            raw_items = backup_data.get("queued_items", {})
            if not isinstance(raw_items, dict):
                raw_items = {}
            migrated = {}
            legacy_to_queue_id = {}
            for old_key, raw_info in raw_items.items():
                if not isinstance(raw_info, dict):
                    continue
                info = dict(raw_info)
                playlist_item_id = info.get("playlist_item_id", info.get("item_id", old_key))
                queue_id = str(info.get("queue_id") or uuid.uuid4())
                info["queue_id"] = queue_id
                info["playlist_item_id"] = str(playlist_item_id) if playlist_item_id is not None else None
                info["item_id"] = info["playlist_item_id"]
                migrated[queue_id] = info
                legacy_to_queue_id[str(old_key)] = queue_id
                if playlist_item_id is not None:
                    legacy_to_queue_id[str(playlist_item_id)] = queue_id

            self._queued_items = migrated
            self._shuffle_restore_queue = [
                legacy_to_queue_id.get(str(value), str(value))
                for value in backup_data.get("shuffle_restore_queue", [])
                if legacy_to_queue_id.get(str(value), str(value)) in migrated
            ]
            pending = backup_data.get("pending_queue_item_id")
            self._pending_queue_item_id = legacy_to_queue_id.get(str(pending)) if pending is not None else None
            self.logger.info(
                "Loaded queue backup: %s queued entries, %s pending shuffle restores",
                len(self._queued_items),
                len(self._shuffle_restore_queue),
            )
            if any(str(key) != str(info.get("queue_id")) for key, info in raw_items.items() if isinstance(info, dict)):
                self._save_queue_backup()
        except Exception as error:
            self.logger.error("Error loading queue backup: %s", error)
            self._queued_items = {}
            self._shuffle_restore_queue = []
            self._pending_queue_item_id = None


    def _save_queue_backup(self):
        """Save current queue state to backup file"""
        try:
            backup_data = {
                'queued_items': dict(self._queued_items),
                'shuffle_restore_queue': list(self._shuffle_restore_queue),
                'pending_queue_item_id': self._pending_queue_item_id,
                'backup_timestamp': __import__('time').time()
            }
            with open(self.queue_backup_file, 'w') as f:
                json.dump(backup_data, f, indent=2)
            self.logger.debug("Queue backup saved successfully")
        except Exception as e:
            self.logger.error(f"Error saving queue backup: {e}")

    async def run_async(self, method_name: str, *args, **kwargs):
        """Run a complete synchronous controller operation off-loop, serialized."""
        method = getattr(self, method_name, None)
        if not callable(method) or method_name.startswith("_"):
            raise ValueError(f"Unsupported VLC controller operation: {method_name}")
        async with self._async_operation_lock:
            return await asyncio.to_thread(self.run_sync, method_name, *args, **kwargs)

    def run_sync(self, method_name: str, *args, **kwargs):
        """Run a synchronous controller operation under the shared cross-thread lock."""
        method = getattr(self, method_name, None)
        if not callable(method) or method_name.startswith("_"):
            raise ValueError(f"Unsupported VLC controller operation: {method_name}")
        with self._operation_thread_lock:
            return method(*args, **kwargs)

    def play_if_not_playing(self):
        """Check VLC state and start playback without a separate read/write race."""
        status = self.get_status()
        if status is None:
            return {"available": False, "command_sent": False}
        state = status.findtext("state")
        if state == "playing":
            return {"available": True, "state": state, "command_sent": False}
        return {"available": True, "state": state, "command_sent": bool(self.play())}

    def pause_if_playing(self):
        """Check VLC state and pause playback within one serialized operation."""
        status = self.get_status()
        if status is None:
            return {"available": False, "command_sent": False}
        state = status.findtext("state")
        if state != "playing":
            return {"available": True, "state": state, "command_sent": False}
        return {"available": True, "state": state, "command_sent": bool(self.pause())}

    def play_playlist_item_at_position(self, position: int):
        """Resolve and play a playlist position without allowing interleaving."""
        playlist = self.get_playlist()
        if playlist is None:
            return {"success": False, "error": "playlist_unavailable"}
        items = playlist.findall(".//leaf")
        if not items:
            return {"success": False, "error": "playlist_empty"}
        if position < 1 or position > len(items):
            return {"success": False, "error": "position_out_of_range", "playlist_count": len(items)}
        item = items[position - 1]
        item_id = item.get("id")
        if not item_id:
            return {"success": False, "error": "item_id_missing"}
        return {"success": bool(self.play_item(item_id)), "item": item, "item_id": item_id, "playlist_count": len(items)}

    def play_item_and_capture(self, item_id: str):
        """Validate a playlist item and issue its item-specific play command atomically."""
        playlist = self.get_playlist()
        if playlist is None:
            return {"success": False, "reason": "playlist_unavailable"}
        item, position = next(
            (
                (leaf, index)
                for index, leaf in enumerate(playlist.findall(".//leaf"), start=1)
                if str(leaf.get("id")) == str(item_id)
            ),
            (None, None),
        )
        if item is None:
            return {"success": False, "reason": "item_not_found"}
        result = self.play_item(item_id)
        if result is None:
            return {"success": False, "reason": "play_command_failed"}
        return {
            "success": True,
            "item_id": str(item_id),
            "item": item,
            "position": position,
        }

    def resume_selected_item_if_current(self, expected_item_id: str, resume: bool = True):
        """Resume only the playlist item selected by the caller, if it is still current.

        Delayed command recovery must not use the generic pl_play command: another
        caller may have selected a different item while the command was waiting.
        This check and the item-specific play command run under the shared lock.
        """
        playlist = self.get_playlist()
        if playlist is None:
            return {"success": False, "reason": "playlist_unavailable", "state": None}
        current_item = next(
            (item for item in playlist.findall(".//leaf") if item.get("current")),
            None,
        )
        current_item_id = current_item.get("id") if current_item is not None else None
        if str(current_item_id) != str(expected_item_id):
            return {
                "success": False,
                "reason": "selection_changed",
                "current_item_id": current_item_id,
                "state": None,
            }

        status = self.get_status()
        if status is None:
            return {"success": False, "reason": "status_unavailable", "state": None}
        state = status.findtext("state")
        if state == "playing":
            return {"success": True, "reason": "already_playing", "state": state}
        if not resume:
            return {"success": False, "reason": "not_playing", "state": state}

        result = self.play_item(expected_item_id)
        return {
            "success": result is not None,
            "reason": "resume_sent" if result is not None else "resume_failed",
            "state": state,
        }

    def queue_playlist_item_at_position(self, position: int):
        """Resolve a playlist position and add it to the soft queue atomically."""
        playlist = self.get_playlist()
        if playlist is None:
            return {"success": False, "error": "playlist_unavailable"}
        items = playlist.findall(".//leaf")
        if not items:
            return {"success": False, "error": "playlist_empty"}
        if position < 1 or position > len(items):
            return {"success": False, "error": "position_out_of_range", "playlist_count": len(items)}
        item = items[position - 1]
        item_id = item.get("id")
        if not item_id:
            return {"success": False, "error": "item_id_missing"}
        result = self.queue_item_next(item_id)
        result["item_id"] = item_id
        result["item_name"] = item.get("name", "Unknown")
        result["playlist_position"] = position
        return result

    def queue_playlist_item_by_id(self, item_id: str):
        """Validate a stable playlist item ID and add that exact item to the soft queue."""
        playlist = self.get_playlist()
        if playlist is None:
            return {"success": False, "error": "playlist_unavailable"}
        items = playlist.findall(".//leaf")
        if not items:
            return {"success": False, "error": "playlist_empty"}

        target_id = str(item_id)
        for position, item in enumerate(items, start=1):
            if str(item.get("id")) != target_id:
                continue
            result = self.queue_item_next(target_id)
            result["item_id"] = target_id
            result["item_name"] = item.get("name", "Unknown")
            result["playlist_position"] = position
            return result

        return {"success": False, "error": "item_not_found"}

    def _current_playlist_item(self, playlist):
        """Return the current leaf and its one-based playlist position."""
        if playlist is None:
            return None, None
        items = playlist.findall(".//leaf")
        for position, item in enumerate(items, start=1):
            if item.get("current"):
                return item, position
        return None, None

    def play_next_or_advance(self):
        """Prefer the soft queue, otherwise advance and capture the selected item."""
        queued_item = self.get_next_queued_item()
        queued_result = self.play_next_queued_item() if queued_item else None
        if queued_result and queued_result.get("success"):
            return {
                "queued_result": queued_result,
                "next_success": False,
                "expected_item_id": str(
                    queued_result.get("item_id") or queued_item.get("item_id")
                ) if queued_result.get("item_id") or queued_item.get("item_id") else None,
            }
        next_result = self.next()
        if not next_result:
            return {"queued_result": queued_result, "next_success": False}
        current, position = self._current_playlist_item(self.get_playlist())
        return {
            "queued_result": queued_result,
            "next_success": True,
            "expected_item_id": current.get("id") if current is not None else None,
            "item": current,
            "position": position,
        }

    def previous_and_capture(self):
        """Move to the previous item and capture its identity in the same operation."""
        result = self.previous()
        if not result:
            return {"success": False, "expected_item_id": None}
        current, position = self._current_playlist_item(self.get_playlist())
        return {
            "success": True,
            "expected_item_id": current.get("id") if current is not None else None,
            "item": current,
            "position": position,
        }

    def get_current_item_if_unchanged(self, expected_item_id: str):
        """Return a fresh playback snapshot only while the expected item remains current."""
        status = self.get_status()
        playlist = self.get_playlist()
        if status is None or playlist is None:
            return {"success": False, "reason": "playback_unavailable"}
        current, position = self._current_playlist_item(playlist)
        current_id = current.get("id") if current is not None else None
        if str(current_id) != str(expected_item_id):
            return {
                "success": False,
                "reason": "selection_changed",
                "current_item_id": current_id,
            }
        return {
            "success": True,
            "reason": "confirmed",
            "item_id": current_id,
            "item": current,
            "position": position,
            "state": status.findtext("state"),
        }

    def set_shuffle_state(self, enabled: bool):
        """Set shuffle to a desired state without a separate read/toggle race."""
        current = self.get_shuffle_state()
        if current == enabled:
            return {"success": True, "previous": current, "enabled": current, "changed": False}
        result = self.toggle_shuffle()
        return {"success": bool(result), "previous": current, "enabled": enabled if result else current, "changed": bool(result)}

    def toggle_shuffle_state(self):
        """Read and toggle shuffle as one serialized operation."""
        current = self.get_shuffle_state()
        result = self.toggle_shuffle()
        return {"success": bool(result), "previous": current, "enabled": not current if result else current}

    def get_queue_snapshot(self):
        """Read soft-queue state and its playlist mapping atomically."""
        return self.get_queue_status(), self.get_playlist()

    def get_playback_snapshot(self):
        """Read status and playlist as one serialized observation."""
        return self.get_status(), self.get_playlist()

    def play_scheduled_item_if_unchanged(self, item_id, expected_current_item_id):
        """Play a scheduled item only while its preflight selection is still current."""
        playlist = self.get_playlist()
        if playlist is None:
            return {"success": False, "confirmed": False, "reason": "playback_unavailable"}
        current, _ = self._current_playlist_item(playlist)
        current_id = current.get("id") if current is not None else None
        if str(current_id) != str(expected_current_item_id):
            return {"success": False, "confirmed": False, "reason": "playback_changed", "current_item_id": current_id}
        target = next(
            (item for item in playlist.findall(".//leaf") if str(item.get("id")) == str(item_id)),
            None,
        )
        if target is None:
            return {"success": False, "confirmed": False, "reason": "item_not_found"}
        result = self.play_item(item_id)
        if result is None or result is False:
            return {"success": False, "confirmed": False, "reason": "play_command_failed"}
        status = self.get_status()
        confirmed_playlist = self.get_playlist()
        confirmed_item, _ = self._current_playlist_item(confirmed_playlist)
        confirmed = bool(
            status is not None and status.findtext("state") == "playing"
            and confirmed_item is not None and str(confirmed_item.get("id")) == str(item_id)
        )
        return {
            "success": True, "confirmed": confirmed,
            "reason": "confirmed" if confirmed else "awaiting_confirmation",
            "item_id": str(item_id),
            "source_item_id": str(current_id) if current_id is not None else None,
        }

    def send_command(self, command: str, params: Optional[Dict[str, str]] = None) -> Optional[ET.Element]:
        """Send command to VLC HTTP interface
        
        Args:
            command: The VLC command to send
            params: Optional command parameters
            
        Returns:
            ElementTree root element of response XML or None on failure
        """
        # Build parameters dict starting with the command
        all_params = {"command": command}
        if params:
            all_params.update(params)
        
        return self._make_request("status.xml", all_params)
    
    def _make_request(self, endpoint: str, params: Optional[Dict[str, str]] = None) -> Optional[ET.Element]:
        """Make a HTTP request to VLC interface with consistent error handling
        
        Args:
            endpoint: The endpoint path (e.g., 'status.xml', 'playlist.xml')
            params: Optional query parameters
            
        Returns:
            ElementTree root element of response XML or None on failure
        """
        try:
            url = f"http://{self.host}:{self.port}/requests/{endpoint}"
            
            response = requests.get(
                url,
                params=params,
                auth=('', self.password),
                timeout=5
            )
            
            self.logger.debug(f"VLC {endpoint} response code: {response.status_code}")
            
            if response.status_code == 200:
                return ET.fromstring(response.content)
            elif response.status_code == 401:
                self.logger.error(f"Authentication failed for {endpoint}. Using password: {self.password[:3]}...")
                return None
            else:
                self.logger.warning(f"VLC {endpoint} request failed with status {response.status_code}")
                return None
        except requests.exceptions.ConnectionError:
            self.logger.debug(f"Could not connect to VLC HTTP interface for {endpoint}")
            return None
        except requests.exceptions.Timeout:
            self.logger.warning(f"VLC {endpoint} request timed out")
            return None
        except ET.ParseError as e:
            self.logger.error(f"Failed to parse VLC {endpoint} XML: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error getting VLC {endpoint}: {e}")
            return None

    def _parse_playlist_info(self, playlist=None):
        """Parse playlist XML to extract item information
        
        Args:
            playlist: Optional playlist XML element. If None, fetches current playlist.
            
        Returns:
            dict: Mapping of item_id -> {name, position, is_current}
        """
        if playlist is None:
            playlist = self.get_playlist()
        
        if not playlist:
            return {}
        
        playlist_map = {}
        current_item_id = None
        
        for idx, item in enumerate(playlist.findall('.//leaf'), 1):
            item_id = item.get('id')
            item_name = item.get('name', 'Unknown')
            is_current = item.get('current') is not None
            
            if item_id:
                playlist_map[item_id] = {
                    'name': item_name,
                    'position': idx,
                    'is_current': is_current
                }
                
                if is_current:
                    current_item_id = item_id
        
        return playlist_map, current_item_id
        
    def get_playlist(self):
        """Get the current VLC playlist"""
        return self._make_request("playlist.xml")

    def export_playlist(self) -> Optional[list]:
        """Export current playlist to a list of dicts with basic fields.

        Returns a list like:
            [{ 'id': '123', 'name': 'Title', 'current': True/False }]

        Returns None if playlist cannot be fetched.
        """
        try:
            playlist = self.get_playlist()
            if not playlist:
                return None
            items = []
            for item in playlist.findall('.//leaf'):
                items.append({
                    'id': item.get('id'),
                    'name': item.get('name', ''),
                    'current': (item.get('current') is not None)
                })
            return items
        except Exception as e:
            self.logger.error(f"Failed to export playlist: {e}")
            return None

    def _uri_to_path(self, uri: str | None) -> Optional[str]:
        """Convert a VLC item URI to a local filesystem path when possible.

        Supports file:// URIs and plain absolute paths. Returns None if conversion
        is not possible.
        """
        try:
            if not uri:
                return None
            u = str(uri)
            # Handle file:// URIs
            if u.lower().startswith('file://'):
                import urllib.parse
                import pathlib
                # Strip the scheme
                path_part = u[7:]
                # URL-decode the path
                p = urllib.parse.unquote(path_part)
                
                # For Windows-style file URIs (file:///C:/path), remove extra leading slash
                if os.name == 'nt':
                    # Windows URIs have format: file:///C:/path/to/file
                    # After u[7:] we get: /C:/path/to/file
                    # After unquote: /C:/path/to/file
                    # We need to strip the leading / to get C:/path/to/file
                    # Check if this is a network path (file://IP/path or file://hostname/path)
                    # file:// URIs with double slashes mean network path (no leading /)
                    if not p.startswith('/'):
                        # This is a network path like: 192.168.1.100/share/path
                        # Convert to UNC path: \\192.168.1.100\share\path
                        unc_path = '\\\\' + p.replace('/', '\\')
                        self.logger.info(f"Network path conversion: {u} -> {unc_path}")
                        return unc_path
                    
                    # Windows local file: file:///C:/path
                    if len(p) > 2 and p[2] == ':':
                        # Remove the leading / to get C:/path/to/file
                        p = p[1:]
                        self.logger.debug(f"Windows local path conversion: {u} -> {p}")
                        return p
                    
                    # Fallback for other Windows paths
                    return p
                else:
                    # Unix/Linux URIs have format: file:///home/user/path
                    # After u[7:] we get: /home/user/path (correct)
                    # Check if this is a network path (file://IP/path)
                    if not p.startswith('/'):
                        # Network path on Unix - should be mounted
                        self.logger.debug(f"Unix network path: {u} -> {p}")
                        return p
                    
                    # Unix/Linux local file: file:///home/user/path
                    self.logger.debug(f"Unix local path conversion: {u} -> {p}")
                    return p
            # Fallback: if it looks like an absolute path, return as-is
            try:
                import pathlib
                pp = pathlib.Path(u)
                if pp.is_absolute():
                    return str(pp)
            except Exception:
                pass
            return None
        except Exception:
            return None

    def delete_playlist_item(self, item_id: str | int) -> bool:
        """Delete a playlist item by VLC item id using pl_delete.

        Returns True if VLC acknowledges the command.
        """
        try:
            res = self.send_command('pl_delete', {'id': str(item_id)})
            ok = res is not None
            if ok:
                self.logger.info(f"Deleted playlist item id={item_id}")
            else:
                self.logger.warning(f"VLC did not acknowledge delete for id={item_id}")
            return ok
        except Exception as e:
            self.logger.error(f"Error deleting playlist item {item_id}: {e}")
            return False

    def remove_missing_playlist_items(self) -> dict:
        """Scan the playlist and remove items whose underlying files are missing.
        
        Uses robust file existence checking that works with network shares on all platforms:
        - First tries os.path.exists() (fast, works for local files)
        - Falls back to os.stat() for network shares (more reliable on Windows)
        - Only skips deletion if file access is absolutely impossible

        Returns a dict: { 'removed': int, 'items': [ { 'id': str, 'name': str } ... ] }
        """
        result = {'removed': 0, 'items': []}
        try:
            playlist = self.get_playlist()
            if not playlist:
                return result
            removed_any = False
            for leaf in playlist.findall('.//leaf'):
                try:
                    item_id = leaf.get('id')
                    name = leaf.get('name', '')
                    uri = leaf.get('uri')
                    path = self._uri_to_path(uri) or name
                    
                    # Debug logging
                    self.logger.debug(f"Checking playlist item: name='{name}', uri='{uri}', resolved_path='{path}'")
                    
                    # Check if this is a network path (UNC path on Windows or network mount on Unix)
                    is_network_path = self._is_network_path(uri, path)
                    
                    exists = False
                    try:
                        # Ensure path is properly normalized for the OS
                        if path:
                            import pathlib
                            normalized_path = str(pathlib.Path(path))
                            
                            # Use robust file existence check that works with network shares
                            exists = self._file_exists_robust(normalized_path)
                            self.logger.debug(f"  Normalized path: '{normalized_path}', exists={exists}, is_network={is_network_path}")
                        else:
                            exists = False
                    except Exception as e:
                        self.logger.debug(f"  Error checking existence: {e}")
                        exists = False
                    
                    if not exists:

                        if item_id:
                            self.logger.warning(f"Missing file detected: '{name}' at '{path}', removing from playlist")
                            ok = self.delete_playlist_item(item_id)
                            if ok:
                                result['removed'] += 1
                                result['items'].append({'id': item_id, 'name': name})
                                removed_any = True
                        else:
                            self.logger.debug(f"Skipping deletion (no id) for missing item '{name}'")
                except Exception as e:
                    self.logger.debug(f"Error checking/removing playlist item: {e}")
            if removed_any:
                # Optionally refresh playlist to reflect changes
                try:
                    self.get_playlist()
                except Exception:
                    pass
            return result
        except Exception as e:
            self.logger.error(f"remove_missing_playlist_items error: {e}")
            return result
            return result
        except Exception as e:
            self.logger.error(f"remove_missing_playlist_items error: {e}")
            return result

    def remove_playlist_items_under(self, folder: str) -> dict:
        """Remove playlist entries under folder, leaving the current item untouched."""
        from ..config import path_is_under

        result = {"removed": 0, "items": [], "skipped_current": False}
        playlist = self.get_playlist()
        if not playlist:
            return result
        for leaf in playlist.findall(".//leaf"):
            path = self._uri_to_path(leaf.get("uri"))
            item_id = leaf.get("id")
            if not path or not item_id or not path_is_under(path, folder):
                continue
            if leaf.get("current") is not None:
                result["skipped_current"] = True
                continue
            if self.delete_playlist_item(item_id):
                result["removed"] += 1
                result["items"].append({"id": item_id, "name": leaf.get("name", "")})
        return result

    def export_playlist_xspf(self) -> Optional[str]:
        """Export current playlist in XSPF format (as a UTF-8 XML string).

        VLC can load this .xspf file directly.
        """
        try:
            playlist = self.get_playlist()
            if not playlist:
                return None

            ns = "http://xspf.org/ns/0/"
            ET.register_namespace('', ns)
            pl_el = ET.Element(ET.QName(ns, 'playlist'), version="1")
            title_el = ET.SubElement(pl_el, ET.QName(ns, 'title'))
            title_el.text = "CtrlVee Playlist Export"
            tl_el = ET.SubElement(pl_el, ET.QName(ns, 'trackList'))

            count = 0
            for leaf in playlist.findall('.//leaf'):
                uri = leaf.get('uri')
                name = leaf.get('name', '')

                # Fallback: if no uri but name looks like an absolute existing path, convert to file URI
                if not uri and name:
                    try:
                        import pathlib
                        p = pathlib.Path(name)
                        if p.is_absolute() and p.exists():
                            uri = p.resolve().as_uri()
                    except Exception:
                        pass

                if not uri:
                    # Skip items without a resolvable URI
                    continue

                tr_el = ET.SubElement(tl_el, ET.QName(ns, 'track'))
                loc_el = ET.SubElement(tr_el, ET.QName(ns, 'location'))
                loc_el.text = uri
                if name:
                    t_el = ET.SubElement(tr_el, ET.QName(ns, 'title'))
                    t_el.text = name
                count += 1

            # Always return a valid XSPF document, even if empty
            return ET.tostring(pl_el, encoding='utf-8', xml_declaration=True).decode('utf-8')
        except Exception as e:
            self.logger.error(f"Failed to export playlist as XSPF: {e}")
            return None
            
    def play(self):
        """Start or resume playback"""
        return self.send_command("pl_play")
        
    def pause(self):
        """Pause playback"""
        return self.send_command("pl_pause")
        
    def stop(self):
        """Stop playback"""
        return self.send_command("pl_stop")
        
    def next(self):
        """Play next track"""
        return self.send_command("pl_next")
        
    def previous(self):
        """Play previous track"""
        return self.send_command("pl_previous")
        
    def seek(self, seconds):
        """Seek to a specific position"""
        return self.send_command("seek", {"val": str(seconds)})

    def get_status(self, enhanced: bool = False):
        """Get current VLC status
        
        Args:
            enhanced: If True, includes playlist position metadata
            
        Returns:
            ElementTree root element of status XML or None on failure
        """
        if enhanced:
            return self._get_enhanced_status()
        return self._make_request("status.xml")
    
    def _get_enhanced_status(self):
        """Get current VLC status with enhanced metadata"""
        status_xml = self.send_command("status")
        if not status_xml:
            return None
            
        # Get playlist information
        playlist_map, current_item_id = self._parse_playlist_info()
        
        if current_item_id and current_item_id in playlist_map:
            current_info = playlist_map[current_item_id]
            total_items = len(playlist_map)
            
            # Add playlist position info to the status XML
            pos_elem = ET.Element('playlist_position')
            pos_elem.text = f"{current_info['position']}/{total_items}"
            status_xml.append(pos_elem)
                
        return status_xml
        
    def play_item(self, item_id):
        """Play a specific item from the playlist"""
        # Send direct play command with the item id
        return self.send_command("pl_play", {"id": str(item_id)})
    
    def get_shuffle_state(self):
        """Check if shuffle is currently enabled"""
        status = self.get_status()
        if status:
            random_elem = status.find('random')
            return random_elem is not None and random_elem.text == 'true'
        return False
    
    def toggle_shuffle(self):
        """Toggle shuffle mode on/off"""
        return self.send_command("pl_random")
    
    def get_repeat_state(self):
        """Check current repeat mode"""
        status = self.get_status()
        if status:
            repeat_elem = status.find('repeat')
            loop_elem = status.find('loop')
            if repeat_elem is not None and repeat_elem.text == 'true':
                return 'one'  # Repeat current item
            elif loop_elem is not None and loop_elem.text == 'true':
                return 'all'  # Repeat playlist
        return 'none'
    
    def move_item_to_position(self, item_id, position):
        """Move a playlist item to a specific position (0-based)
        
        Note: VLC 3.x has limited playlist modification support via HTTP interface.
        This method attempts to move items but may not work on all VLC versions.
        The soft queue system provides a fallback for reliable queueing.
        """
        result = self.send_command("pl_move", {"id": str(item_id), "psn": str(position)})
        if result is not None:
            self.logger.debug(f"Move command sent for item {item_id} to position {position}")
            return True
        return False
    
    def play_item_by_id(self, item_id):
        """Play a specific playlist item and verify VLC selected it.

        VLC can execute a command but return an empty/non-XML HTTP body. The
        command response alone is therefore not a reliable acknowledgement.
        """
        import time

        item_id = str(item_id)
        playlist = self.get_playlist()
        if playlist is None:
            self.logger.warning("Cannot play playlist item %s: VLC playlist is unavailable", item_id)
            return False
        if not any(str(item.get("id")) == item_id for item in playlist.findall(".//leaf")):
            self.logger.warning("Cannot play playlist item %s: item is not in VLC playlist", item_id)
            return False

        response = self.send_command("pl_play", {"id": item_id})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            playlist = self.get_playlist()
            if playlist is not None:
                current_item, _ = self._current_playlist_item(playlist)
                if current_item is not None and str(current_item.get("id")) == item_id:
                    self.logger.debug("VLC confirmed playlist item %s is now selected", item_id)
                    return True
            time.sleep(0.1)

        if response is not None:
            self.logger.warning("VLC acknowledged play for item %s but did not select it", item_id)
        else:
            self.logger.warning("VLC did not select item %s after the play command", item_id)
        return False

    def set_rate(self, rate: float) -> bool:
        """Set VLC playback rate/speed.

        Args:
            rate: Playback rate (e.g., 1.0, 1.5, 0.5)

        Returns:
            True if the command was successfully sent (response not None), False otherwise.
        """
        try:
            # VLC HTTP interface accepts a 'rate' command with 'val' parameter
            res = self.send_command('rate', {'val': str(float(rate))})
            if res is not None:
                self.logger.info(f"Set VLC playback rate to {rate}")
                return True
            else:
                self.logger.debug(f"VLC did not return a response when setting rate to {rate}")
                return False
        except Exception as e:
            self.logger.error(f"Error setting VLC rate to {rate}: {e}")
            return False

    # ==========================
    # Subtitle track management
    # ==========================
    def set_subtitle_track(self, value: int | str) -> bool:
        """Set VLC subtitle track by absolute id or relative step.

        Accepts either an integer track id (e.g., 0, 1, 2, ...), or a relative
        step string like "+1" or "-1" to cycle to next/previous when supported by VLC.

        Returns True if the command was acknowledged by VLC.
        """
        try:
            self.logger.info(f"Attempting to set subtitle track to value={value}")
            res = self.send_command('subtitle_track', {'val': str(value)})
            ok = res is not None
            if ok:
                self.logger.info(f"Set VLC subtitle track val={value} - SUCCESS")
            else:
                self.logger.warning(f"VLC did not acknowledge subtitle_track val={value}")
            return ok
        except Exception as e:
            self.logger.error(f"Error setting VLC subtitle track to {value}: {e}")
            return False

    def subtitle_next(self) -> bool:
        """Cycle to next subtitle track, if supported by VLC."""
        return self.set_subtitle_track("+1")

    def subtitle_prev(self) -> bool:
        """Cycle to previous subtitle track, if supported by VLC."""
        return self.set_subtitle_track("-1")

    # =======================
    # Audio track management
    # =======================
    def set_audio_track(self, value: int | str) -> bool:
        """Set VLC audio track by absolute id/index or relative step.

        Accepts an integer track value (e.g., 0, 1, 2, ...), or a relative
        step string like "+1" or "-1" when supported by VLC.

        Returns True if the command was acknowledged by VLC.
        """
        try:
            self.logger.info(f"Attempting to set audio track to value={value}")
            res = self.send_command('audio_track', {'val': str(value)})
            ok = res is not None
            if ok:
                self.logger.info(f"Set VLC audio track val={value} - SUCCESS")
            else:
                self.logger.warning(f"VLC did not acknowledge audio_track val={value}")
            return ok
        except Exception as e:
            self.logger.error(f"Error setting VLC audio track to {value}: {e}")
            return False

    def get_audio_tracks(self) -> Optional[list[dict]]:
        """Return available audio tracks with selection and display info.

        Parses VLC status.xml for stream information to build an audio track list.
        Each track dict has: { 'id': int, 'name': str, 'selected': bool, 'index': int, 'stream_index': int }

        Returns None if status cannot be fetched.
        """
        try:
            status = self.get_status()
            if status is None:
                return None

            node = status.find('audio')
            tracks: list[dict] = []
            if node is not None:
                for t in node.findall('track'):
                    raw_id = t.get('id')
                    tid = None
                    if raw_id is not None:
                        try:
                            tid = int(str(raw_id).strip())
                        except Exception:
                            tid = None
                    name_attr = t.get('name')
                    text_val = (t.text or '').strip()
                    name = name_attr if name_attr else (text_val if text_val else f"Track {raw_id if raw_id is not None else ''}")
                    selected = (str(t.get('selected', '')).strip().lower() in {'true', '1', 'yes'})
                    tracks.append({'id': tid, 'name': name, 'selected': selected})
                    self.logger.debug(f"Track from <audio>: id={tid}, name={name}, selected={selected}")

            streams: list[Dict[str, Any]] = []
            try:
                info_root = status.find('information')
                if info_root is not None:
                    for category in info_root.findall('category'):
                        cname = (category.get('name') or '')
                        lcname = cname.lower()
                        stream_index = None
                        try:
                            import re
                            m = re.search(r'stream\s*(\d+)', lcname)
                            if m:
                                stream_index = int(m.group(1))
                        except Exception:
                            stream_index = None

                        imap: Dict[str, str] = {}
                        for info in category.findall('info'):
                            k = (info.get('name') or '')
                            v = (info.text or '')
                            imap[k] = v

                        type_val = (imap.get('Type') or imap.get('type') or '').strip().lower()
                        if 'stream' in lcname and type_val in {'audio'}:
                            raw_id = (
                                imap.get('Track id') or imap.get('Track ID') or imap.get('track id') or
                                imap.get('TrackID') or imap.get('trackid') or imap.get('ID') or imap.get('id')
                            )
                            tid = None
                            if raw_id:
                                try:
                                    tid = int(str(raw_id).strip())
                                except Exception:
                                    tid = None

                            lang = (imap.get('Language') or imap.get('language') or '').strip()
                            desc = (imap.get('Description') or imap.get('description') or '').strip()
                            codec = (imap.get('Codec') or imap.get('codec') or '').strip()
                            channels = (
                                imap.get('Channels') or imap.get('channels') or
                                imap.get('Channel(s)') or imap.get('channel(s)') or ''
                            ).strip()

                            parts = [p for p in [lang, desc or None, codec or None, (f"{channels}ch" if channels else None)] if p]
                            name = ' / '.join(parts) if parts else (cname or 'Audio Stream')
                            streams.append({'id': tid, 'name': name, 'stream_index': stream_index})
            except Exception as e:
                self.logger.debug(f"Audio streams parse failed: {e}")

            filtered_tracks = []
            for tr in tracks:
                tid = tr.get('id')
                if tid is not None and tid < 0:
                    continue
                filtered_tracks.append(tr)
            tracks = filtered_tracks

            if streams:
                try:
                    streams.sort(key=lambda s: (s.get('stream_index') is None, s.get('stream_index')))
                except Exception:
                    pass

                id_to_idx: Dict[int, int] = {}
                name_to_idx: Dict[str, int] = {}
                id_to_stream_idx: Dict[int, int] = {}
                ui_counter = 1
                for s in streams:
                    sid = s.get('id')
                    sindex = s.get('stream_index')
                    if sid is not None and sid < 0:
                        continue
                    if sid is not None:
                        id_to_idx[sid] = ui_counter
                        if sindex is not None:
                            id_to_stream_idx[sid] = sindex
                    nm = (s.get('name') or '').strip().lower()
                    if nm:
                        name_to_idx[nm] = ui_counter
                    ui_counter += 1

                for tr in tracks:
                    ui_idx = None
                    stream_idx = None
                    tid = tr.get('id')
                    if tid is not None and tid in id_to_idx:
                        ui_idx = id_to_idx[tid]
                        stream_idx = id_to_stream_idx.get(tid)
                    else:
                        nm = (tr.get('name') or '').strip().lower()
                        if nm and nm in name_to_idx:
                            ui_idx = name_to_idx[nm]
                    tr['index'] = ui_idx
                    tr['stream_index'] = stream_idx

                if not tracks:
                    idx = 1
                    for s in streams:
                        if s.get('id') is not None and s.get('id') < 0:
                            continue
                        tracks.append({
                            'id': s.get('id'),
                            'name': s.get('name'),
                            'selected': False,
                            'index': idx,
                            'stream_index': s.get('stream_index')
                        })
                        idx += 1
            else:
                for idx, tr in enumerate(tracks, start=1):
                    tr['index'] = idx

            self.logger.debug(f"get_audio_tracks returning {len(tracks)} tracks")
            return tracks
        except Exception as e:
            self.logger.error(f"get_audio_tracks error: {e}")
            return None

    def get_subtitle_tracks(self) -> Optional[list[dict]]:
        """Return available subtitle tracks with selection info.

        Parses VLC status.xml for stream information to build subtitle track list.
        Each track dict has: { 'id': int, 'name': str, 'selected': bool, 'index': int, 'stream_index': int }
        
        Note: VLC's HTTP API doesn't reliably expose which subtitle is currently selected,
        so 'selected' will always be False unless set externally by the caller.
        
        Returns None if status cannot be fetched.
        """
        try:
            status = self.get_status()
            if status is None:
                return None
            
            # VLC uses <subtitle><track id=".." selected="true">Name</track></subtitle>
            node = status.find('subtitle')
            if node is None:
                # Some versions might use 'subtitles'
                node = status.find('subtitles')
            # Build initial track list from <subtitle>/<subtitles>
            tracks: list[dict] = []
            if node is not None:
                for t in node.findall('track'):
                    raw_id = t.get('id')
                    tid = None
                    if raw_id is not None:
                        try:
                            tid = int(str(raw_id).strip())
                        except Exception:
                            tid = None
                    name_attr = t.get('name')
                    text_val = (t.text or '').strip()
                    name = name_attr if name_attr else (text_val if text_val else f"Track {raw_id if raw_id is not None else ''}")
                    # Note: VLC HTTP API doesn't reliably report selected status
                    selected = False
                    tracks.append({'id': tid, 'name': name, 'selected': selected})
                    self.logger.debug(f"Track from <subtitle>: id={tid}, name={name}")

            # Parse stream categories to derive UI order (Stream 0, Stream 1, ...)
            streams: list[Dict[str, Any]] = []
            try:
                info_root = status.find('information')
                if info_root is not None:
                    for category in info_root.findall('category'):
                        cname = (category.get('name') or '')
                        lcname = cname.lower()
                        # Extract numeric stream index from name like "Stream 2"
                        stream_index = None
                        try:
                            import re
                            m = re.search(r'stream\s*(\d+)', lcname)
                            if m:
                                stream_index = int(m.group(1))
                        except Exception:
                            stream_index = None
                        # Collect key/values
                        imap: Dict[str, str] = {}
                        for info in category.findall('info'):
                            k = (info.get('name') or '')
                            v = (info.text or '')
                            imap[k] = v
                        type_val = (imap.get('Type') or imap.get('type') or '').strip().lower()
                        if 'stream' in lcname and type_val in {'subtitle', 'text', 'subtitles'}:
                            # Potential ID keys in different builds
                            raw_id = (imap.get('Track id') or imap.get('Track ID') or imap.get('track id') or
                                      imap.get('TrackID') or imap.get('trackid') or imap.get('ID') or imap.get('id'))
                            tid = None
                            if raw_id:
                                try:
                                    tid = int(str(raw_id).strip())
                                except Exception:
                                    tid = None
                            lang = (imap.get('Language') or imap.get('language') or '').strip()
                            desc = (imap.get('Description') or imap.get('description') or '').strip()
                            codec = (imap.get('Codec') or imap.get('codec') or '').strip()
                            parts = [p for p in [lang, desc or None, (codec if not desc else None)] if p]
                            name = ' / '.join(parts) if parts else (cname or 'Subtitle Stream')
                            streams.append({'id': tid, 'name': name, 'stream_index': stream_index})
            except Exception as e:
                self.logger.debug(f"Subtitle streams parse failed: {e}")

            # Filter out disable/off tracks (usually id=-1 or id=0 with name like "Disable" or "Off")
            # These shouldn't be in the user-facing list
            filtered_tracks = []
            for tr in tracks:
                tid = tr.get('id')
                name_lower = (tr.get('name') or '').strip().lower()
                # Skip tracks that are clearly "disable" options
                if tid is not None and tid < 0:
                    self.logger.debug(f"Filtering out disable track: id={tid}, name={tr.get('name')}")
                    continue
                if name_lower in {'disable', 'disabled', 'off', 'none'}:
                    self.logger.debug(f"Filtering out disable track by name: id={tid}, name={tr.get('name')}")
                    continue
                filtered_tracks.append(tr)
            tracks = filtered_tracks

            # If we have stream info, map tracks to UI order by id or name
            if streams:
                # Sort streams by numeric index if available, else keep order
                try:
                    streams.sort(key=lambda s: (s.get('stream_index') is None, s.get('stream_index')))
                except Exception:
                    pass
                # Build id->ui_index map (1-based, no gaps)
                id_to_idx: Dict[int, int] = {}
                name_to_idx: Dict[str, int] = {}
                # Also store stream_index for VLC API calls
                id_to_stream_idx: Dict[int, int] = {}
                ui_counter = 1
                for s in streams:
                    sid = s.get('id')
                    sindex = s.get('stream_index')
                    # Skip negative IDs in stream mapping too
                    if sid is not None and sid < 0:
                        continue
                    if sid is not None:
                        id_to_idx[sid] = ui_counter
                        if sindex is not None:
                            id_to_stream_idx[sid] = sindex
                    nm = (s.get('name') or '').strip().lower()
                    if nm:
                        name_to_idx[nm] = ui_counter
                    ui_counter += 1
                
                # Attach index to tracks for display and selection
                for tr in tracks:
                    ui_idx = None
                    stream_idx = None
                    tid = tr.get('id')
                    if tid is not None and tid in id_to_idx:
                        ui_idx = id_to_idx[tid]
                        stream_idx = id_to_stream_idx.get(tid)
                    else:
                        nm = (tr.get('name') or '').strip().lower()
                        if nm and nm in name_to_idx:
                            ui_idx = name_to_idx[nm]
                    tr['index'] = ui_idx
                    tr['stream_index'] = stream_idx


                # If tracks list was empty, create tracks purely from streams (excluding negative IDs)
                if not tracks:
                    idx = 1
                    for s in streams:
                        if s.get('id') is not None and s.get('id') < 0:
                            continue
                        tracks.append({
                            'id': s.get('id'), 
                            'name': s.get('name'), 
                            'selected': False,  # Caller must set this based on external tracking
                            'index': idx,
                            'stream_index': s.get('stream_index')
                        })
                        idx += 1
            else:
                # No stream info available, use simple enumeration for tracks
                for idx, tr in enumerate(tracks, start=1):
                    tr['index'] = idx
                    # Without stream info, we don't have stream_index, so it stays None

            self.logger.debug(f"get_subtitle_tracks returning {len(tracks)} tracks (after filtering)")
            return tracks
        except Exception as e:
            self.logger.error(f"get_subtitle_tracks error: {e}")
            return None

    def get_selected_subtitle_track_id(self) -> Optional[int]:
        """Return the currently selected subtitle track id/index, or None.
        
        Note: VLC's HTTP API doesn't reliably expose the selected subtitle track.
        This method is kept for compatibility but may not return accurate results.
        Consider tracking subtitle selection externally in your application.
        """
        tracks = self.get_subtitle_tracks()
        if not tracks:
            return None
        for tr in tracks:
            if tr.get('selected'):
                return tr.get('id') or tr.get('stream_index')
        return None
    
    def get_current_position(self):
        """Get the current item's position in the playlist"""
        playlist_map, current_item_id = self._parse_playlist_info()
        
        if current_item_id and current_item_id in playlist_map:
            return playlist_map[current_item_id]['position'] - 1  # Return 0-based position
        
        return None
    
    def _queue_id_for_playlist_item_id(self, playlist_item_id):
        """Resolve a VLC playlist item ID to its independent soft-queue entry ID."""
        if playlist_item_id is None:
            return None
        wanted = str(playlist_item_id)
        return next(
            (
                queue_id for queue_id, info in self._queued_items.items()
                if str(info.get("playlist_item_id", info.get("item_id", queue_id))) == wanted
            ),
            None,
        )

    def _queue_info_for_playlist_item_id(self, playlist_item_id):
        queue_id = self._queue_id_for_playlist_item_id(playlist_item_id)
        return (queue_id, self._queued_items.get(queue_id)) if queue_id else (None, None)

    def queue_item_next(self, item_id, restore_shuffle=True):
        """Add a VLC media reference as a new, independently identified queue entry."""
        try:
            playlist_item_id = str(item_id)
            playlist_map, _ = self._parse_playlist_info()
            item = playlist_map.get(playlist_item_id)
            if item is None:
                return {"success": False, "error": "item_not_found"}

            shuffle_was_on = self.get_shuffle_state()
            if shuffle_was_on and not self._queued_items:
                self.toggle_shuffle()

            self._compact_queue_order()
            queue_order = len(self._queued_items) + 1
            should_restore_shuffle = bool(
                (shuffle_was_on and not self._queued_items)
                or any(info.get("restore_shuffle", False) for info in self._queued_items.values())
            )
            queue_id = str(uuid.uuid4())
            queue_info = {
                "queue_id": queue_id,
                "playlist_item_id": playlist_item_id,
                "item_id": playlist_item_id,
                "item_name": item.get("name", "Unknown"),
                "queue_order": queue_order,
                "shuffle_was_on": shuffle_was_on,
                "restore_shuffle": should_restore_shuffle,
                "queued_at_time": __import__("time").time(),
                "queue_type": "soft",
            }
            self._queued_items[queue_id] = queue_info

            if should_restore_shuffle and len(self._queued_items) > 1:
                for existing_id, existing_info in self._queued_items.items():
                    if existing_id != queue_id:
                        existing_info["restore_shuffle"] = False
            if shuffle_was_on and restore_shuffle:
                self._shuffle_restore_queue.append(queue_id)

            self._save_queue_backup()
            return {
                "success": True,
                "queue_id": queue_id,
                "item_id": playlist_item_id,
                "playlist_item_id": playlist_item_id,
                "item_name": queue_info["item_name"],
                "queue_order": queue_order,
                "total_queued": len(self._queued_items),
            }
        except Exception as error:
            self.logger.error("Error soft-queuing item: %s", error)
            return {"success": False, "error": str(error)}


    def restore_shuffle_if_needed(self, was_shuffle_on):
        """
        Restore shuffle mode if it was previously enabled
        This should be called after a queued item finishes playing
        """
        if was_shuffle_on and not self.get_shuffle_state():
            self.logger.info("Restoring shuffle mode after queued item finished")
            return self.toggle_shuffle()
        return True
    
    def _compact_queue_order(self):
        """Renumber queued items so their visible positions are contiguous."""
        ordered_items = sorted(
            self._queued_items.items(),
            key=lambda entry: (
                entry[1].get("queue_order", 0),
                entry[1].get("queued_at_time", 0),
            ),
        )
        for queue_order, (_item_id, queue_info) in enumerate(ordered_items, start=1):
            queue_info["queue_order"] = queue_order

    def get_next_queued_item(self):
        """Return the earliest queue entry and its current VLC playback reference."""
        if not self._queued_items:
            return None
        queue_id, info = min(
            self._queued_items.items(),
            key=lambda entry: (
                entry[1].get("queue_order", 0),
                entry[1].get("queued_at_time", 0),
            ),
        )
        playlist_item_id = info.get("playlist_item_id", info.get("item_id", queue_id))
        return {
            "queue_id": queue_id,
            "item_id": str(playlist_item_id) if playlist_item_id is not None else None,
            "playlist_item_id": str(playlist_item_id) if playlist_item_id is not None else None,
            "item_name": info.get("item_name", "Unknown"),
            "queue_order": info.get("queue_order", 0),
            "restore_shuffle": info.get("restore_shuffle", False),
        }


    def play_queued_item_if_monitor_state(
        self,
        expected_state: str,
        expected_current_item_id: Optional[str] = None,
        require_end: bool = False,
        end_threshold_seconds: int = 3,
    ):
        """Revalidate a monitor observation and start queued playback atomically.

        This method is called through run_async/run_sync, so status, playlist,
        queue inspection, and the eventual play command share the controller's
        existing operation lock. A stale monitor observation must never
        interrupt a newer manual playback selection.
        """
        status = self.get_status()
        if status is None:
            return {"success": False, "attempted": False, "reason": "status_unavailable"}

        live_state = status.findtext("state")
        if live_state != expected_state:
            return {
                "success": False,
                "attempted": False,
                "reason": "state_changed",
                "state": live_state,
            }

        live_current_item_id = None
        if expected_current_item_id is not None:
            playlist = self.get_playlist()
            if playlist is None:
                return {"success": False, "attempted": False, "reason": "playlist_unavailable"}
            current_item = next(
                (item for item in playlist.findall(".//leaf") if item.get("current")),
                None,
            )
            live_current_item_id = current_item.get("id") if current_item is not None else None
            if str(live_current_item_id) != str(expected_current_item_id):
                return {
                    "success": False,
                    "attempted": False,
                    "reason": "current_item_changed",
                    "current_item_id": live_current_item_id,
                }

        if require_end:
            try:
                current_time = int(status.findtext("time"))
                total_length = int(status.findtext("length"))
            except (TypeError, ValueError):
                return {"success": False, "attempted": False, "reason": "end_position_unavailable"}
            if total_length <= 0 or total_length - current_time > end_threshold_seconds:
                return {
                    "success": False,
                    "attempted": False,
                    "reason": "not_at_end",
                    "time": current_time,
                    "length": total_length,
                }

        next_item = self.get_next_queued_item()
        if not next_item:
            return {"success": False, "attempted": False, "reason": "queue_empty"}
        if expected_current_item_id is not None and str(next_item["item_id"]) == str(expected_current_item_id):
            return {"success": False, "attempted": False, "reason": "queued_item_already_current"}

        result = self.play_next_queued_item()
        result["attempted"] = True
        result["reason"] = "played" if result.get("success") else "play_failed"
        return result

    def play_next_queued_item(self):
        """Play the next queue entry through its current VLC playlist reference."""
        if self._pending_queue_item_id in self._queued_items:
            return {"success": False, "error": f"Queued entry {self._pending_queue_item_id} is already starting"}
        self._pending_queue_item_id = None
        self._pending_queue_started_at = None

        next_item = self.get_next_queued_item()
        if not next_item:
            return {"success": False, "error": "No items in queue"}
        queue_id = next_item["queue_id"]
        playlist_item_id = next_item["playlist_item_id"]
        if playlist_item_id is None:
            return {"success": False, "error": "Queued media has no VLC playlist reference"}
        remaining_queue_items = len(self._queued_items)
        should_restore_shuffle = next_item.get("restore_shuffle", False)
        success = self.play_item_by_id(playlist_item_id)
        if not success:
            return {"success": False, "error": f"Failed to play item {playlist_item_id}"}

        if remaining_queue_items == 1 and should_restore_shuffle and not self.get_shuffle_state():
            self.logger.info("Restoring shuffle mode - last queued item (%s) is now playing", next_item["item_name"])
            self.toggle_shuffle()

        self._pending_queue_item_id = queue_id
        self._pending_queue_started_at = __import__("time").time()
        self.logger.info("Started queued entry %s: %s (VLC ID: %s)", queue_id, next_item["item_name"], playlist_item_id)
        return {
            "success": True,
            "queue_id": queue_id,
            "item_id": playlist_item_id,
            "playlist_item_id": playlist_item_id,
            "item_name": next_item["item_name"],
            "queue_order": next_item["queue_order"],
        }


    def check_and_handle_queue_transition(
        self,
        current_item_id,
        expected_state=None,
        finished_item_id=None,
    ):
        """Apply monitor bookkeeping only while its observed VLC item is still current.

        Queue playback is deliberately not started here. The monitor uses
        play_queued_item_if_monitor_state as the sole automatic queue-start
        operation because it revalidates state, item identity, end position,
        and queue contents together under the controller lock.
        """
        if expected_state is not None:
            status = self.get_status()
            if status is None:
                return {"stale": True, "reason": "status_unavailable", "transitions": []}
            live_state = status.findtext("state")
            if live_state != expected_state:
                return {
                    "stale": True,
                    "reason": "state_changed",
                    "state": live_state,
                    "transitions": [],
                }

            playlist = self.get_playlist()
            if playlist is None:
                return {"stale": True, "reason": "playlist_unavailable", "transitions": []}
            live_item, _ = self._current_playlist_item(playlist)
            live_item_id = live_item.get("id") if live_item is not None else None
            if str(live_item_id) != str(current_item_id):
                return {
                    "stale": True,
                    "reason": "current_item_changed",
                    "current_item_id": live_item_id,
                    "transitions": [],
                }

        transitions = []

        # A successful queue play can precede VLC reporting the new current
        # item. Ignore stale observations until the pending item is confirmed.
        if self._pending_queue_item_id is not None:
            pending_info = self._queued_items.get(self._pending_queue_item_id, {})
            pending_playlist_id = pending_info.get("playlist_item_id", pending_info.get("item_id"))
            if str(current_item_id) == str(pending_playlist_id):
                self.logger.info("VLC confirmed queued entry %s is now playing", self._pending_queue_item_id)
                self._pending_queue_item_id = None
                self._pending_queue_started_at = None
            else:
                pending_age = (
                    __import__("time").time() - self._pending_queue_started_at
                    if self._pending_queue_started_at is not None
                    else 0
                )
                if pending_age < 30:
                    return {
                        "stale": False,
                        "reason": "queue_item_pending",
                        "transitions": transitions,
                        "active_queue_items": len(self._queued_items),
                        "pending_shuffle_restores": len(self._shuffle_restore_queue),
                    }
                self.logger.warning(
                    "Queued item %s was not observed within 30 seconds; "
                    "allowing queue playback to retry",
                    self._pending_queue_item_id,
                )
                self._pending_queue_item_id = None
                self._pending_queue_started_at = None

        queue_id, queue_info = self._queue_info_for_playlist_item_id(current_item_id)
        if queue_id is not None:
            item_name = queue_info.get("item_name", "Unknown")
            self.logger.info("Soft-queued entry now playing: %s (queue ID: %s, VLC ID: %s)", item_name, queue_id, current_item_id)
            self._remove_from_queue(queue_id)
            transitions.append({
                "action": "queued_item_started",
                "queue_id": queue_id,
                "item_id": str(current_item_id),
                "item_name": item_name,
                "success": True,
            })

        if finished_item_id and str(finished_item_id) != str(current_item_id):
            self._handle_queued_item_finished(finished_item_id)
            self.set_rate(1.0)

        if transitions:
            self._save_queue_backup()

        return {
            "stale": False,
            "transitions": transitions,
            "active_queue_items": len(self._queued_items),
            "pending_shuffle_restores": len(self._shuffle_restore_queue),
        }

    def get_queue_status(self):
        """Get current queue status information"""
        return {
            "queued_items": dict(self._queued_items),
            "shuffle_restore_queue": list(self._shuffle_restore_queue),
            "shuffle_currently_on": self.get_shuffle_state()
        }
    
    def reconcile_queue_with_playlist(self):
        """Reconcile queue entries against their VLC playback references."""
        playlist = self.get_playlist()
        if playlist is None:
            return {
                "success": False,
                "reason": "playlist_unavailable",
                "removed_item_ids": [],
                "remaining": len(self._queued_items),
            }
        live_ids = {
            str(item.get("id"))
            for item in playlist.findall(".//leaf")
            if item.get("id") is not None
        }
        removed_queue_ids = []
        for queue_id, info in list(self._queued_items.items()):
            if not isinstance(info, dict):
                removed_queue_ids.append(queue_id)
                self._queued_items.pop(queue_id, None)
                continue
            playlist_item_id = info.get("playlist_item_id", info.get("item_id", queue_id))
            if playlist_item_id is None or str(playlist_item_id) not in live_ids:
                removed_queue_ids.append(queue_id)
                self._queued_items.pop(queue_id, None)

        self._shuffle_restore_queue = [
            queue_id for queue_id in dict.fromkeys(self._shuffle_restore_queue)
            if queue_id in self._queued_items
        ]
        self._compact_queue_order()
        if self._pending_queue_item_id not in self._queued_items:
            self._pending_queue_item_id = None
            self._pending_queue_started_at = None
        self._save_queue_backup()
        return {
            "success": True,
            "removed_queue_ids": removed_queue_ids,
            "removed_item_ids": removed_queue_ids,
            "remaining": len(self._queued_items),
            "queued_item_ids": [
                queue_id for queue_id, _ in sorted(
                    self._queued_items.items(),
                    key=lambda entry: entry[1].get("queue_order", 0),
                )
            ],
        }


    def clear_queue_tracking(self):
        """Clear all queue tracking state (useful for reset/cleanup)"""
        self._queued_items.clear()
        self._shuffle_restore_queue.clear()  # Also clear shuffle restore queue
        self._pending_queue_item_id = None
        self._pending_queue_started_at = None
        self._save_queue_backup()
        self.logger.info("Cleared all queue tracking state including shuffle restore queue")

    def _reconcile_queued_playlist_items(self, queue_ids, protected_item_ids=None, protected_uris=None):
        """Delete safe VLC references for queue entries, then remove their entry IDs."""
        playlist = self.get_playlist()
        if playlist is None:
            return {
                "success": False, "error": "playlist_unavailable",
                "removed_item_ids": [], "removed_queue_ids": [],
                "preserved_item_ids": [], "failed_item_ids": [str(value) for value in queue_ids],
            }
        protected_ids = {str(value) for value in (protected_item_ids or ())}
        protected_uri_values = {str(value) for value in (protected_uris or ()) if value}
        live_items = {
            str(item.get("id")): item for item in playlist.findall(".//leaf")
            if item.get("id") is not None
        }
        current, _ = self._current_playlist_item(playlist)
        current_id = str(current.get("id")) if current is not None else None
        candidate_queue_ids = [str(value) for value in queue_ids]
        candidates = {
            queue_id: str(self._queued_items[queue_id].get("playlist_item_id", self._queued_items[queue_id].get("item_id", queue_id)))
            for queue_id in candidate_queue_ids if queue_id in self._queued_items
        }
        preserved_queue_ids = []
        for queue_id, playlist_id in candidates.items():
            item = live_items.get(playlist_id)
            if item is None:
                continue
            uri = item.get("uri")
            if playlist_id == current_id or playlist_id in protected_ids or (uri and uri in protected_uri_values):
                preserved_queue_ids.append(queue_id)
                continue
            self.delete_playlist_item(playlist_id)

        confirmed = self.get_playlist()
        if confirmed is None:
            return {
                "success": False, "error": "playlist_unavailable_after_delete",
                "removed_item_ids": [], "removed_queue_ids": [],
                "preserved_item_ids": preserved_queue_ids, "failed_item_ids": candidate_queue_ids,
            }
        remaining_ids = {
            str(item.get("id")) for item in confirmed.findall(".//leaf")
            if item.get("id") is not None
        }
        removed_queue_ids, failed_queue_ids = [], []
        for queue_id, playlist_id in candidates.items():
            item_uri = live_items.get(playlist_id).get("uri") if playlist_id in live_items else None
            protected = (
                playlist_id == current_id or playlist_id in protected_ids
                or queue_id in preserved_queue_ids
                or (item_uri and item_uri in protected_uri_values)
            )
            if playlist_id not in remaining_ids or protected:
                self._remove_from_queue(queue_id)
                removed_queue_ids.append(queue_id)
            else:
                failed_queue_ids.append(queue_id)
        return {
            "success": not failed_queue_ids,
            "error": "playlist_item_delete_failed" if failed_queue_ids else None,
            "removed_queue_ids": removed_queue_ids,
            "removed_item_ids": [candidates[value] for value in removed_queue_ids],
            "preserved_item_ids": [candidates[value] for value in preserved_queue_ids if candidates[value] in remaining_ids],
            "failed_item_ids": [candidates[value] for value in failed_queue_ids],
            "failed_queue_ids": failed_queue_ids,
            "remaining": len(self._queued_items),
        }


    def clear_queue(self, protected_item_ids=None, protected_uris=None):
        """Clear soft-queue tracking and delete only safe VLC playlist entries."""
        item_ids = sorted(
            self._queued_items,
            key=lambda item_id: self._queued_items[item_id].get("queue_order", 0),
        )
        if not item_ids:
            return {
                "success": True,
                "removed_item_ids": [],
                "preserved_item_ids": [],
                "failed_item_ids": [],
                "remaining": 0,
            }
        return self._reconcile_queued_playlist_items(
            item_ids,
            protected_item_ids=protected_item_ids,
            protected_uris=protected_uris,
        )

    def remove_from_queue_by_order(self, queue_order: int, protected_item_ids=None, protected_uris=None):
        """Remove a queued item by order and reconcile its VLC playlist entry."""
        try:
            target_id = next(
                (
                    item_id for item_id, info in self._queued_items.items()
                    if info.get("queue_order") == int(queue_order)
                ),
                None,
            )
            if target_id is None:
                return {"success": False, "error": f"Queue order {queue_order} not found"}
            name = self._queued_items[target_id].get("item_name", "Unknown")
            result = self._reconcile_queued_playlist_items([target_id], protected_item_ids, protected_uris)
            result.update({"removed_item_id": target_id, "item_name": name})
            if not result["success"] and result.get("error") is None:
                result["error"] = "Could not reconcile the VLC playlist entry"
            return result
        except Exception as e:
            self.logger.error(f"remove_from_queue_by_order error: {e}")
            return {"success": False, "error": str(e)}

    def remove_from_queue_by_id(self, queue_id: str, protected_item_ids=None, protected_uris=None):
        """Remove a queue entry by its independent ID and reconcile its VLC reference."""
        try:
            queue_id = str(queue_id)
            if queue_id not in self._queued_items:
                return {"success": False, "error": "That track is no longer queued"}
            info = self._queued_items[queue_id]
            result = self._reconcile_queued_playlist_items([queue_id], protected_item_ids, protected_uris)
            result.update({
                "queue_id": queue_id,
                "removed_item_id": info.get("playlist_item_id", info.get("item_id", queue_id)),
                "item_name": info.get("item_name", "Unknown"),
            })
            if not result["success"] and result.get("error") is None:
                result["error"] = "Could not reconcile the VLC playlist entry"
            return result
        except Exception as error:
            self.logger.error("remove_from_queue_by_id error: %s", error)
            return {"success": False, "error": str(error)}


    def move_queued_item_by_id(self, queue_id: str, direction: str):
        """Move a queue entry by independent ID and persist its order."""
        try:
            queue_id = str(queue_id)
            if direction not in ("up", "down"):
                return {"success": False, "error": "Direction must be 'up' or 'down'"}
            if queue_id not in self._queued_items:
                return {"success": False, "error": "That track is no longer queued"}
            self._compact_queue_order()
            ordered = sorted(self._queued_items.items(), key=lambda entry: entry[1].get("queue_order", 0))
            current_index = next(index for index, entry in enumerate(ordered) if entry[0] == queue_id)
            target_index = current_index - 1 if direction == "up" else current_index + 1
            if target_index < 0 or target_index >= len(ordered):
                return {"success": False, "error": "That item is already at the queue boundary"}
            _, info = ordered[current_index]
            neighbor_id, neighbor_info = ordered[target_index]
            info["queue_order"], neighbor_info["queue_order"] = neighbor_info["queue_order"], info["queue_order"]
            self._save_queue_backup()
            return {
                "success": True,
                "queue_id": queue_id,
                "item_id": info.get("playlist_item_id", info.get("item_id")),
                "item_name": info.get("item_name", "Unknown"),
                "queue_order": info["queue_order"],
                "swapped_with": neighbor_id,
            }
        except Exception as error:
            self.logger.error("move_queued_item_by_id error: %s", error)
            return {"success": False, "error": str(error)}


    def remove_from_queue_by_playlist_number(self, number: int, protected_item_ids=None, protected_uris=None):
        """Remove the queued entry whose VLC reference currently occupies a playlist position."""
        try:
            playlist = self.get_playlist()
            if playlist is None:
                return {"success": False, "error": "playlist_unavailable"}
            playlist_map, _ = self._parse_playlist_info(playlist)
            playlist_id = next(
                (item_id for item_id, info in playlist_map.items() if info.get("position") == int(number)),
                None,
            )
            if playlist_id is None:
                return {"success": False, "error": f"Playlist number {number} not found"}
            queue_id = self._queue_id_for_playlist_item_id(playlist_id)
            if queue_id is None:
                return {"success": False, "error": f"Playlist #{number} is not queued"}
            return self.remove_from_queue_by_id(queue_id, protected_item_ids, protected_uris)
        except Exception as error:
            self.logger.error("remove_from_queue_by_playlist_number error: %s", error)
            return {"success": False, "error": str(error)}


    def enqueue_item(self, item_id):
        """Add an item to the end of the playlist"""
        # VLC's enqueue command - adds to end of playlist
        return self.send_command("in_enqueue", {"id": str(item_id)})

    def enqueue_path(self, file_path: str) -> bool:
        """Add a local file path to the end of the playlist.

        Uses VLC HTTP command 'in_enqueue' with 'input' parameter.
        Escapes path as a file URI.
        """
        try:
            # Convert to file URI using pathlib for correctness
            import pathlib
            p = pathlib.Path(file_path).resolve()
            uri = p.as_uri()
            result = self.send_command("in_enqueue", {"input": uri})
            return result is not None
        except Exception as e:
            self.logger.error(f"enqueue_path failed for {file_path}: {e}")
            return False
    
    def smart_queue(self, item_id, behavior="auto"):
        """
        Intelligently queue an item based on current state and user preference
        
        Args:
            item_id: The playlist item ID to queue
            behavior: Queuing behavior
                - "auto": Smart default behavior
                - "play_now": Play immediately
                - "queue_next": Queue to play next (disable shuffle temporarily)
                - "add_to_end": Add to end of playlist
        
        Returns:
            dict: Result of the queuing operation
        """
        shuffle_on = self.get_shuffle_state()
        
        if behavior == "play_now":
            return {
                "success": bool(self.play_item(item_id)),
                "action": "played_immediately",
                "shuffle_affected": False
            }
        elif behavior == "add_to_end":
            return {
                "success": bool(self.enqueue_item(item_id)),
                "action": "added_to_end",
                "shuffle_affected": False
            }
        elif behavior == "queue_next":
            return self.queue_item_next(item_id)
        else:  # "auto"
            return self.queue_item_next(item_id)

    def _remove_from_queue(self, item_id):
        """
        Remove an item from the queue and handle shuffle restoration if needed
        
        Args:
            item_id: The item ID to remove from the queue
        """
        if item_id not in self._queued_items:
            self.logger.debug(f"Item {item_id} not in queue - nothing to remove")
            return
        
        # Get the item info before removing it
        queue_info = self._queued_items[item_id]
        should_restore_shuffle = queue_info.get("restore_shuffle", False)
        
        # Remove the entry and its completion marker, preserving the obligation
        # to restore shuffle if this was the final shuffle-managed queue item.
        del self._queued_items[item_id]
        self._shuffle_restore_queue = [
            queued_id for queued_id in self._shuffle_restore_queue
            if str(queued_id) != str(item_id)
        ]
        self._compact_queue_order()
        if self._pending_queue_item_id == item_id:
            self._pending_queue_item_id = None
            self._pending_queue_started_at = None
        self.logger.debug(f"Removed item {item_id} from queue")

        if self._queued_items and should_restore_shuffle:
            last_item_id, last_item = max(
                self._queued_items.items(),
                key=lambda entry: entry[1].get("queue_order", 0),
            )
            last_item["restore_shuffle"] = True
            if last_item_id not in self._shuffle_restore_queue:
                self._shuffle_restore_queue.append(last_item_id)
        elif not self._queued_items:
            if should_restore_shuffle:
                self.restore_shuffle_if_needed(True)
            self._shuffle_restore_queue.clear()
            self.logger.debug("Queue is empty - cleared shuffle restore queue")

        # Save the updated queue state
        self._save_queue_backup()
        self.logger.debug(f"Queue state saved after removing item {item_id}")

    def finish_queued_item(self, item_id):
        """Public serialized-operation entry point for queue completion cleanup."""
        return self._handle_queued_item_finished(item_id)

    def _handle_queued_item_finished(self, item_id):
        """Clean up shuffle restoration bookkeeping for a finished VLC media item."""
        queue_id = self._queue_id_for_playlist_item_id(item_id)
        if queue_id is None:
            # Backups created before queue-entry IDs stored VLC IDs in this list.
            queue_id = str(item_id) if str(item_id) in self._shuffle_restore_queue else None
        if queue_id is None or queue_id not in self._shuffle_restore_queue:
            self.logger.debug("Item %s finished without a shuffle-restore marker", item_id)
            return
        self._shuffle_restore_queue.remove(queue_id)
        self._save_queue_backup()


    def _file_exists_robust(self, path: str) -> bool:
        """Check if a file exists using multiple methods for reliability.
        
        Works with both local files and network shares:
        1. First tries os.path.exists() (fast, works for local)
        2. Falls back to os.stat() (works better with network shares)
        
        Returns True only if the file definitely exists.
        """
        try:
            # First, try the fast method
            if os.path.exists(path):
                return True
            
            # If that fails, try os.stat() which works better with network shares
            # os.stat() will raise FileNotFoundError if file doesn't exist
            try:
                os.stat(path)
                self.logger.debug(f"File exists (detected via os.stat): {path}")
                return True
            except (FileNotFoundError, OSError) as e:
                self.logger.debug(f"File does not exist (os.stat failed): {path} - {type(e).__name__}")
                return False
        except Exception as e:
            self.logger.debug(f"Unexpected error checking file: {path} - {e}")
            return False
    
    def _is_network_path(self, uri: str | None, path: str | None) -> bool:
        """Check if a path is a network path that should be protected from deletion.
        
        Detects:
        - file://IP_ADDRESS/... (SMB shares via IP, e.g., file://192.168.1.100/share/file)
        - file://HOSTNAME/... (SMB shares via hostname)
        - UNC paths (\\server\share, //server/share)
        - Linux network mounts (/mnt/, /media/)
        """
        import re
        
        # Check the raw URI first
        if uri:
            uri_lower = str(uri).lower()
            # file://192.168.x.x/... pattern
            if re.search(r'file://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/', uri_lower):
                self.logger.debug(f"Detected network path (SMB via IP): {uri}")
                return True
            # file://hostname/... pattern (non-IP domain/hostname)
            # file:// URLs with network hosts don't have triple slashes after file://
            if uri_lower.startswith('file://') and not uri_lower.startswith('file:///'):
                # file://hostname/path (only two slashes) indicates network path
                self.logger.debug(f"Detected network path (SMB via hostname): {uri}")
                return True
        
        # Check the resolved path
        if path:
            path_str = str(path)
            # UNC paths: \\server\share or //server/share
            if path_str.startswith('\\\\') or path_str.startswith('//'):
                self.logger.debug(f"Detected network path (UNC): {path}")
                return True
            # Linux network mounts
            if path_str.startswith('/mnt/') or path_str.startswith('/media/'):
                self.logger.debug(f"Detected network mount: {path}")
                return True
        
        return False


# Error classes for VLC operations
class VLCConnectionError(VLCError):
    """Raised when there's an error connecting to VLC"""
    pass

class VLCCommandError(VLCError):
    """Raised when a VLC command fails"""
    pass
