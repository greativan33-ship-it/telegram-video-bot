import sqlite3
from pathlib import Path

from .config import QUALITIES


class Preferences:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS preferences "
                "(user_id INTEGER PRIMARY KEY, quality TEXT NOT NULL)"
            )

    def get_quality(self, user_id: int, default: str) -> str:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT quality FROM preferences WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row[0] if row and row[0] in QUALITIES else default

    def set_quality(self, user_id: int, quality: str) -> None:
        if quality not in QUALITIES:
            raise ValueError("Unknown quality")
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO preferences(user_id, quality) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET quality = excluded.quality",
                (user_id, quality),
            )
