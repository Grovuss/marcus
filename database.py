"""
All persistent storage for Marcus lives here: logged messages, logged
GIFs, per-guild settings, per-channel settings, and a small ring of
recently-generated responses used for duplicate prevention.

Uses aiosqlite so nothing blocks the bot's event loop.
"""
import aiosqlite
import datetime
import os
import re
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
    gif_response_chance REAL,
    dm_hidden INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_channel_settings_guild ON channel_settings(guild_id);

CREATE TABLE IF NOT EXISTS recent_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recent_responses_guild ON recent_responses(guild_id);

CREATE INDEX IF NOT EXISTS idx_messages_message_id ON messages(message_id);
CREATE INDEX IF NOT EXISTS idx_gifs_message_id ON gifs(message_id);

-- Words/phrases/links Marcus must never remember (applies to every server).
CREATE TABLE IF NOT EXISTS memory_filter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at TEXT NOT NULL
);

-- Messages an admin queued to be Marcus's next post in a channel.
CREATE TABLE IF NOT EXISTS queued_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queued_messages_channel ON queued_messages(channel_id);
"""

MAX_RECENT_RESPONSES_PER_GUILD = 25

# WHERE clause (for messages/gifs) excluding channels marked hidden from DMs.
DM_VISIBLE = "channel_id NOT IN (SELECT channel_id FROM channel_settings WHERE dm_hidden = 1)"


def _now() -> str:
    return datetime.datetime.utcnow().isoformat()


def _like(text: str) -> str:
    """Escape a user search string for use in LIKE ... ESCAPE '\\'."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _compile_filter(patterns: list[str]) -> re.Pattern | None:
    """One case-insensitive regex matching any filter entry as a whole word/phrase."""
    if not patterns:
        return None
    alternatives = "|".join(re.escape(p) for p in sorted(patterns, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


class Database:
    def __init__(self, path: str = None):
        self.path = path or os.getenv("DB_PATH") or CONFIG["database"]["path"]
        self._db: aiosqlite.Connection = None
        self._filter_re: re.Pattern | None = None

    async def connect(self):
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()
        await self._reload_filter()

    async def _migrate(self):
        """Add columns introduced after a database was first created."""
        cur = await self._db.execute("PRAGMA table_info(channel_settings)")
        columns = {r["name"] for r in await cur.fetchall()}
        if "dm_hidden" not in columns:
            await self._db.execute(
                "ALTER TABLE channel_settings ADD COLUMN dm_hidden INTEGER NOT NULL DEFAULT 0"
            )

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

    async def set_channel_dm_hidden(self, channel_id: int, guild_id: int, hidden: bool):
        """Keep (or stop keeping) a channel's memory out of DM responses."""
        await self._db.execute(
            """INSERT INTO channel_settings (channel_id, guild_id, dm_hidden) VALUES (?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET dm_hidden = excluded.dm_hidden""",
            (str(channel_id), str(guild_id), 1 if hidden else 0),
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
    async def log_message(self, message_id, channel_id, guild_id, author_id, content) -> bool:
        """Returns False (and stores nothing) if the text hits the memory filter."""
        if self.is_filtered(content):
            return False
        await self._db.execute(
            """INSERT INTO messages (message_id, channel_id, guild_id, author_id, content, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(message_id), str(channel_id), str(guild_id), str(author_id), content, _now()),
        )
        await self._db.commit()
        return True

    async def get_dm_corpus(self, limit: int = 5000) -> list[str]:
        """A random sample of message text from every server, minus channels hidden from DMs."""
        cur = await self._db.execute(
            f"SELECT content FROM messages WHERE {DM_VISIBLE} ORDER BY RANDOM() LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [r["content"] for r in rows if not self.is_filtered(r["content"])]

    async def get_dm_gifs(self, limit: int = 2000) -> list[str]:
        """A random sample of GIFs from every server, minus channels hidden from DMs."""
        cur = await self._db.execute(
            f"SELECT url FROM gifs WHERE {DM_VISIBLE} ORDER BY RANDOM() LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [r["url"] for r in rows if not self.is_filtered(r["url"])]

    async def get_corpus(self, channel_id: int = None, guild_id: int = None, limit: int = 5000) -> list[str]:
        """Returns a list of logged message text, most recent first. Filtered entries are skipped."""
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
        return [r["content"] for r in rows if not self.is_filtered(r["content"])]

    async def get_timeline(self, channel_id: int = None, query: str = None,
                           limit: int = 100, offset: int = 0,
                           after_message_row: int = None, after_gif_row: int = None) -> list[dict]:
        """
        Saved Discord messages for the dashboard, newest first, with each
        message's text and GIFs grouped together. Optionally limited to
        one channel and/or to messages whose text or GIF URL contains
        `query`, and/or to messages with rows newer than the given row
        ids (for live updates). Each entry: {message_id, channel_id,
        guild_id, author_id, timestamp, texts: [{id, content}], gifs: [{id, url}]}.
        """
        where, params = [], []
        if channel_id is not None:
            where.append("channel_id = ?")
            params.append(str(channel_id))
        msg_where = list(where)
        gif_where = list(where)
        msg_params = list(params)
        gif_params = list(params)
        if after_message_row is not None:
            msg_where.append("id > ?")
            msg_params.append(after_message_row)
        if after_gif_row is not None:
            gif_where.append("id > ?")
            gif_params.append(after_gif_row)
        if query:
            msg_where.append("content LIKE ? ESCAPE '\\'")
            msg_params.append(_like(query))
            gif_where.append("url LIKE ? ESCAPE '\\'")
            gif_params.append(_like(query))

        def clause(parts):
            return ("WHERE " + " AND ".join(parts)) if parts else ""

        cur = await self._db.execute(
            f"""SELECT message_id, channel_id, guild_id, author_id, MIN(timestamp) AS timestamp FROM (
                    SELECT message_id, channel_id, guild_id, author_id, timestamp FROM messages {clause(msg_where)}
                    UNION ALL
                    SELECT message_id, channel_id, guild_id, author_id, timestamp FROM gifs {clause(gif_where)}
                ) GROUP BY message_id, channel_id ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
            msg_params + gif_params + [limit, offset],
        )
        entries = [dict(r, texts=[], gifs=[]) for r in await cur.fetchall()]
        if not entries:
            return entries

        by_key = {(e["message_id"], e["channel_id"]): e for e in entries}
        ids = list({e["message_id"] for e in entries})
        marks = ",".join("?" * len(ids))
        cur = await self._db.execute(
            f"SELECT id, message_id, channel_id, content FROM messages WHERE message_id IN ({marks}) ORDER BY id", ids
        )
        for r in await cur.fetchall():
            if (r["message_id"], r["channel_id"]) in by_key:
                by_key[(r["message_id"], r["channel_id"])]["texts"].append({"id": r["id"], "content": r["content"]})
        cur = await self._db.execute(
            f"SELECT id, message_id, channel_id, url FROM gifs WHERE message_id IN ({marks}) ORDER BY id", ids
        )
        for r in await cur.fetchall():
            if (r["message_id"], r["channel_id"]) in by_key:
                by_key[(r["message_id"], r["channel_id"])]["gifs"].append({"id": r["id"], "url": r["url"]})
        return entries

    async def max_row_ids(self) -> tuple[int, int]:
        """Newest (messages.id, gifs.id) - the live-update cursor for the dashboard."""
        cur = await self._db.execute(
            "SELECT (SELECT IFNULL(MAX(id), 0) FROM messages) AS m, (SELECT IFNULL(MAX(id), 0) FROM gifs) AS g"
        )
        row = await cur.fetchone()
        return row["m"], row["g"]

    async def delete_message_row(self, row_id: int) -> int:
        cur = await self._db.execute("DELETE FROM messages WHERE id = ?", (row_id,))
        await self._db.commit()
        return cur.rowcount

    async def delete_gif_row(self, row_id: int) -> int:
        cur = await self._db.execute("DELETE FROM gifs WHERE id = ?", (row_id,))
        await self._db.commit()
        return cur.rowcount

    async def count_by_channel(self, guild_id: int) -> dict[str, dict]:
        """{channel_id: {"messages": n, "gifs": n}} for every channel with logged data."""
        counts: dict[str, dict] = {}
        for table, key in (("messages", "messages"), ("gifs", "gifs")):
            cur = await self._db.execute(
                f"SELECT channel_id, COUNT(*) AS c FROM {table} WHERE guild_id = ? GROUP BY channel_id",
                (str(guild_id),),
            )
            for row in await cur.fetchall():
                counts.setdefault(row["channel_id"], {"messages": 0, "gifs": 0})[key] = row["c"]
        return counts

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
        """Returns True if inserted, False if it was a duplicate for that channel or filtered."""
        if self.is_filtered(url):
            return False
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
        return [r["url"] for r in rows if not self.is_filtered(r["url"])]

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

    # ------------------------------------------------------------------
    # Memory filter (words/phrases/links that are never remembered)
    # ------------------------------------------------------------------
    async def _reload_filter(self):
        cur = await self._db.execute("SELECT pattern FROM memory_filter")
        self._filter_re = _compile_filter([r["pattern"] for r in await cur.fetchall()])

    def is_filtered(self, text: str) -> bool:
        return bool(self._filter_re and text and self._filter_re.search(text))

    async def list_filter(self) -> list[dict]:
        cur = await self._db.execute("SELECT id, pattern, created_at FROM memory_filter ORDER BY pattern")
        return [dict(r) for r in await cur.fetchall()]

    async def add_filter(self, pattern: str) -> bool:
        """Returns False if the pattern was already in the filter."""
        try:
            await self._db.execute(
                "INSERT INTO memory_filter (pattern, created_at) VALUES (?, ?)", (pattern, _now())
            )
            await self._db.commit()
        except aiosqlite.IntegrityError:
            return False
        await self._reload_filter()
        return True

    async def remove_filter(self, filter_id: int):
        await self._db.execute("DELETE FROM memory_filter WHERE id = ?", (filter_id,))
        await self._db.commit()
        await self._reload_filter()

    async def purge_filtered(self) -> int:
        """Deletes every already-saved message/GIF that matches the current filter."""
        if not self._filter_re:
            return 0
        removed = 0
        for table, column in (("messages", "content"), ("gifs", "url")):
            cur = await self._db.execute(f"SELECT id, {column} AS v FROM {table}")
            ids = [r["id"] for r in await cur.fetchall() if self.is_filtered(r["v"])]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                await self._db.execute(
                    f"DELETE FROM {table} WHERE id IN ({','.join('?' * len(chunk))})", chunk
                )
            removed += len(ids)
        await self._db.commit()
        return removed

    # ------------------------------------------------------------------
    # Queued messages (admin-chosen next post for a channel)
    # ------------------------------------------------------------------
    async def queue_message(self, channel_id: int, guild_id: int, content: str):
        await self._db.execute(
            "INSERT INTO queued_messages (channel_id, guild_id, content, created_at) VALUES (?, ?, ?, ?)",
            (str(channel_id), str(guild_id), content, _now()),
        )
        await self._db.commit()

    async def list_queue(self, channel_id: int) -> list[dict]:
        cur = await self._db.execute(
            "SELECT id, content, created_at FROM queued_messages WHERE channel_id = ? ORDER BY id",
            (str(channel_id),),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def pop_queued(self, channel_id: int) -> str | None:
        """Removes and returns the oldest queued message for a channel, if any."""
        cur = await self._db.execute(
            "SELECT id, content FROM queued_messages WHERE channel_id = ? ORDER BY id LIMIT 1",
            (str(channel_id),),
        )
        row = await cur.fetchone()
        if not row:
            return None
        await self._db.execute("DELETE FROM queued_messages WHERE id = ?", (row["id"],))
        await self._db.commit()
        return row["content"]

    async def remove_queued(self, queue_id: int):
        await self._db.execute("DELETE FROM queued_messages WHERE id = ?", (queue_id,))
        await self._db.commit()
