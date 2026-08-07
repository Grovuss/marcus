"""
All persistent storage for Marcus lives here: logged messages, logged
GIFs, per-guild settings, per-channel settings, and a small ring of
recently-generated responses used for duplicate prevention.

Uses aiosqlite so nothing blocks the bot's event loop.
"""
import aiosqlite
import datetime
import os
from config import CONFIG

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id);
CREATE INDEX IF NOT EXISTS idx_messages_guild ON messages(guild_id);
CREATE INDEX IF NOT EXISTS idx_messages_author ON messages(author_id);

CREATE TABLE IF NOT EXISTS gifs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    url TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    UNIQUE(channel_id, url)
);
CREATE INDEX IF NOT EXISTS idx_gifs_channel ON gifs(channel_id);
CREATE INDEX IF NOT EXISTS idx_gifs_guild ON gifs(guild_id);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id TEXT PRIMARY KEY,
    global_response_chance REAL NOT NULL,
    cooldown_seconds INTEGER NOT NULL,
    max_words INTEGER NOT NULL,
    min_words INTEGER NOT NULL,
    markov_order INTEGER NOT NULL,
    generation_mode TEXT NOT NULL,
    gif_enabled INTEGER NOT NULL,
    gif_response_chance REAL NOT NULL,
    gif_channel_local_preference INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_settings (
    channel_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    logging_enabled INTEGER NOT NULL DEFAULT 0,
    responses_enabled INTEGER NOT NULL DEFAULT 0,
    response_chance REAL,
    gif_response_chance REAL
);
CREATE INDEX IF NOT EXISTS idx_channel_settings_guild ON channel_settings(guild_id);

CREATE TABLE IF NOT EXISTS recent_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recent_responses_guild ON recent_responses(guild_id);
"""

MAX_RECENT_RESPONSES_PER_GUILD = 25


def _now() -> str:
    return datetime.datetime.utcnow().isoformat()


class Database:
    def __init__(self, path: str = None):
        self.path = path or os.getenv("DB_PATH") or CONFIG["database"]["path"]
        self._db: aiosqlite.Connection = None

    async def connect(self):
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    # ------------------------------------------------------------------
    # Guild settings
    # ------------------------------------------------------------------
    async def ensure_guild(self, guild_id: int):
        cur = await self._db.execute(
            "SELECT 1 FROM guild_settings WHERE guild_id = ?", (str(guild_id),)
        )
        row = await cur.fetchone()
        if row:
            return
        r = CONFIG["response"]
        g = CONFIG["gif"]
        gen = CONFIG["generation"]
        await self._db.execute(
            """INSERT INTO guild_settings
               (guild_id, global_response_chance, cooldown_seconds, max_words,
                min_words, markov_order, generation_mode, gif_enabled,
                gif_response_chance, gif_channel_local_preference)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(guild_id),
                r["global_chance"],
                r["cooldown_seconds"],
                r["max_words"],
                r["min_words"],
                gen["markov_order"],
                gen["mode"],
                1 if g["enabled"] else 0,
                g["response_chance"],
                1 if g["channel_local_preference"] else 0,
            ),
        )
        await self._db.commit()

    async def get_guild_settings(self, guild_id: int) -> dict:
        await self.ensure_guild(guild_id)
        cur = await self._db.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (str(guild_id),)
        )
        row = await cur.fetchone()
        return dict(row)

    async def set_guild_setting(self, guild_id: int, field: str, value):
        await self.ensure_guild(guild_id)
        allowed = {
            "global_response_chance", "cooldown_seconds", "max_words",
            "min_words", "markov_order", "generation_mode", "gif_enabled",
            "gif_response_chance", "gif_channel_local_preference",
        }
        if field not in allowed:
            raise ValueError(f"Unknown guild setting: {field}")
        await self._db.execute(
            f"UPDATE guild_settings SET {field} = ? WHERE guild_id = ?",
            (value, str(guild_id)),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Channel settings
    # ------------------------------------------------------------------
    async def get_channel_settings(self, channel_id: int) -> dict | None:
        cur = await self._db.execute(
            "SELECT * FROM channel_settings WHERE channel_id = ?", (str(channel_id),)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_channel_settings(self, guild_id: int) -> list[dict]:
        cur = await self._db.execute(
            "SELECT * FROM channel_settings WHERE guild_id = ? ORDER BY channel_id",
            (str(guild_id),),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def add_channel(self, channel_id: int, guild_id: int):
        """Register a channel with logging + responses enabled."""
        existing = await self.get_channel_settings(channel_id)
        if existing:
            await self._db.execute(
                "UPDATE channel_settings SET logging_enabled = 1, "
                "responses_enabled = 1 WHERE channel_id = ?",
                (str(channel_id),),
            )
        else:
            await self._db.execute(
                """INSERT INTO channel_settings
                   (channel_id, guild_id, logging_enabled, responses_enabled,
                    response_chance, gif_response_chance)
                   VALUES (?, ?, 1, 1, NULL, NULL)""",
                (str(channel_id), str(guild_id)),
            )
        await self._db.commit()

    async def remove_channel(self, channel_id: int):
        """Disable logging + responses for a channel (data is kept)."""
        await self._db.execute(
            "UPDATE channel_settings SET logging_enabled = 0, "
            "responses_enabled = 0 WHERE channel_id = ?",
            (str(channel_id),),
        )
        await self._db.commit()

    async def set_channel_responses_enabled(self, channel_id: int, enabled: bool):
        await self._db.execute(
            "UPDATE channel_settings SET responses_enabled = ? WHERE channel_id = ?",
            (1 if enabled else 0, str(channel_id)),
        )
        await self._db.commit()

    async def set_channel_response_chance(self, channel_id: int, value: float | None):
        await self._db.execute(
            "UPDATE channel_settings SET response_chance = ? WHERE channel_id = ?",
            (value, str(channel_id)),
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Message logging
    # ------------------------------------------------------------------
    async def log_message(self, message_id, channel_id, guild_id, author_id, content):
        await self._db.execute(
            """INSERT INTO messages (message_id, channel_id, guild_id, author_id, content, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(message_id), str(channel_id), str(guild_id), str(author_id), content, _now()),
        )
        await self._db.commit()

    async def get_corpus(self, channel_id: int = None, guild_id: int = None, limit: int = 5000) -> list[str]:
        """Returns a list of logged message text, most recent first."""
        if channel_id is not None:
            cur = await self._db.execute(
                "SELECT content FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
                (str(channel_id), limit),
            )
        elif guild_id is not None:
            cur = await self._db.execute(
                "SELECT content FROM messages WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
                (str(guild_id), limit),
            )
        else:
            cur = await self._db.execute(
                "SELECT content FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            )
        rows = await cur.fetchall()
        return [r["content"] for r in rows]

    async def delete_channel_messages(self, channel_id: int) -> int:
        cur = await self._db.execute(
            "DELETE FROM messages WHERE channel_id = ?", (str(channel_id),)
        )
        await self._db.commit()
        return cur.rowcount

    async def delete_guild_messages(self, guild_id: int) -> int:
        cur = await self._db.execute(
            "DELETE FROM messages WHERE guild_id = ?", (str(guild_id),)
        )
        await self._db.commit()
        return cur.rowcount

    async def delete_user_messages(self, guild_id: int, author_id: int) -> int:
        cur = await self._db.execute(
            "DELETE FROM messages WHERE guild_id = ? AND author_id = ?",
            (str(guild_id), str(author_id)),
        )
        await self._db.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # GIF logging
    # ------------------------------------------------------------------
    async def log_gif(self, message_id, channel_id, guild_id, author_id, url) -> bool:
        """Returns True if inserted, False if it was a duplicate for that channel."""
        try:
            await self._db.execute(
                """INSERT INTO gifs (message_id, channel_id, guild_id, author_id, url, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(message_id), str(channel_id), str(guild_id), str(author_id), url, _now()),
            )
            await self._db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get_gifs(self, channel_id: int = None, guild_id: int = None, limit: int = 2000) -> list[str]:
        if channel_id is not None:
            cur = await self._db.execute(
                "SELECT url FROM gifs WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
                (str(channel_id), limit),
            )
        elif guild_id is not None:
            cur = await self._db.execute(
                "SELECT url FROM gifs WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
                (str(guild_id), limit),
            )
        else:
            cur = await self._db.execute("SELECT url FROM gifs ORDER BY id DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [r["url"] for r in rows]

    async def delete_channel_gifs(self, channel_id: int) -> int:
        cur = await self._db.execute("DELETE FROM gifs WHERE channel_id = ?", (str(channel_id),))
        await self._db.commit()
        return cur.rowcount

    async def delete_guild_gifs(self, guild_id: int) -> int:
        cur = await self._db.execute("DELETE FROM gifs WHERE guild_id = ?", (str(guild_id),))
        await self._db.commit()
        return cur.rowcount

    async def delete_user_gifs(self, guild_id: int, author_id: int) -> int:
        cur = await self._db.execute(
            "DELETE FROM gifs WHERE guild_id = ? AND author_id = ?",
            (str(guild_id), str(author_id)),
        )
        await self._db.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Corpus statistics
    # ------------------------------------------------------------------
    async def get_stats(self, guild_id: int, channel_id: int = None) -> dict:
        where_msg = "WHERE guild_id = ?"
        where_gif = "WHERE guild_id = ?"
        params = [str(guild_id)]
        if channel_id is not None:
            where_msg = "WHERE channel_id = ?"
            where_gif = "WHERE channel_id = ?"
            params = [str(channel_id)]

        cur = await self._db.execute(f"SELECT COUNT(*) AS c FROM messages {where_msg}", params)
        message_count = (await cur.fetchone())["c"]

        cur = await self._db.execute(f"SELECT COUNT(*) AS c FROM gifs {where_gif}", params)
        gif_count = (await cur.fetchone())["c"]

        cur = await self._db.execute(
            f"SELECT COUNT(DISTINCT author_id) AS c FROM messages {where_msg}", params
        )
        unique_users = (await cur.fetchone())["c"]

        cur = await self._db.execute(
            f"SELECT COUNT(DISTINCT channel_id) AS c FROM messages {where_msg}", params
        )
        channel_count = (await cur.fetchone())["c"]

        cur = await self._db.execute(
            f"SELECT MIN(timestamp) AS t FROM messages {where_msg}", params
        )
        oldest = (await cur.fetchone())["t"]

        cur = await self._db.execute(
            f"SELECT MAX(timestamp) AS t FROM messages {where_msg}", params
        )
        newest = (await cur.fetchone())["t"]

        return {
            "message_count": message_count,
            "gif_count": gif_count,
            "unique_users": unique_users,
            "channel_count": channel_count,
            "oldest": oldest,
            "newest": newest,
        }

    # ------------------------------------------------------------------
    # Recent responses (duplicate prevention)
    # ------------------------------------------------------------------
    async def was_recently_sent(self, guild_id: int, content: str) -> bool:
        cur = await self._db.execute(
            """SELECT 1 FROM recent_responses
               WHERE guild_id = ? AND content = ?
               ORDER BY id DESC LIMIT 1""",
            (str(guild_id), content),
        )
        row = await cur.fetchone()
        return row is not None

    async def record_response(self, guild_id: int, content: str):
        await self._db.execute(
            "INSERT INTO recent_responses (guild_id, content, created_at) VALUES (?, ?, ?)",
            (str(guild_id), content, _now()),
        )
        # trim to keep only the most recent N per guild
        await self._db.execute(
            """DELETE FROM recent_responses WHERE guild_id = ? AND id NOT IN (
                   SELECT id FROM recent_responses WHERE guild_id = ?
                   ORDER BY id DESC LIMIT ?
               )""",
            (str(guild_id), str(guild_id), MAX_RECENT_RESPONSES_PER_GUILD),
        )
        await self._db.commit()
