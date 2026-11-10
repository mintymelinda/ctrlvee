import json
import logging
import os

logger = logging.getLogger(__name__)

FINISHED_FRACTION = 0.85
BUCKET_SECONDS = 60


class StatsService:
    """Per-user watch time and completed titles, persisted in a local JSON file."""

    def __init__(self, path: str = "watch_stats.json", excluded=()):
        self.path = path
        self.excluded = {str(user_id) for user_id in excluded}
        self.users = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as file:
                return json.load(file)
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as error:
            if os.path.exists(self.path):
                logger.error("Unreadable watch stats file %s: %s", self.path, error)
                corrupt_path = f"{self.path}.corrupt"
                if not os.path.exists(corrupt_path):
                    os.replace(self.path, corrupt_path)
            return {}

    def save(self) -> None:
        temporary_path = f"{self.path}.tmp"
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(self.users, file)
        os.replace(temporary_path, self.path)

    def record(
        self,
        viewers: dict,
        title_key: str | None,
        position: int,
        length: int,
        seconds: int,
    ) -> None:
        """Credit viewers for time watched and track distinct runtime buckets."""
        bucket = position // BUCKET_SECONDS
        needed = -(-length // BUCKET_SECONDS) * FINISHED_FRACTION
        for user_id, name in viewers.items():
            user = self.users.setdefault(
                str(user_id),
                {"name": name, "seconds": 0, "finished": [], "progress": {}},
            )
            user["name"] = name
            user["seconds"] += seconds
            if not title_key or length <= 0 or title_key in user["finished"]:
                continue
            seen = user["progress"].setdefault(title_key, [])
            if bucket not in seen:
                seen.append(bucket)
            if len(seen) >= needed:
                user["finished"].append(title_key)
                del user["progress"][title_key]

    def ladders(self, limit: int = 10) -> tuple[list, list]:
        """Return watch-time and completed-title rankings."""
        users = [
            user for user_id, user in self.users.items()
            if user_id not in self.excluded
        ]
        hours = sorted(
            ((user["name"], user["seconds"]) for user in users if user["seconds"]),
            key=lambda row: -row[1],
        )
        finished = sorted(
            ((user["name"], len(user["finished"])) for user in users if user["finished"]),
            key=lambda row: -row[1],
        )
        return hours[:limit], finished[:limit]
