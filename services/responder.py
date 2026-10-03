"""
Core response decision-making: probability roll, cooldown enforcement,
text-vs-GIF-vs-both selection, corpus fetching, generation, and
duplicate-response prevention.

In servers, Marcus only ever draws on what was logged in the channel he
is replying in - never another channel or another server. In DMs he
draws on everything he has logged, from every server.

Cooldown state is kept in memory (per channel) since it's short-lived
and doesn't need to survive a restart.
"""
import random
import time

import discord

from config import CONFIG
from database import Database
from generator.generator import ResponseGenerator

DM_SCOPE = 0  # recent_responses key used for duplicate prevention in DMs


class Responder:
    def __init__(self, db: Database, generator: ResponseGenerator):
        self.db = db
        self.generator = generator
        self._last_response_at: dict[int, float] = {}  # channel_id -> monotonic time

    def _effective_response_chance(self, guild_settings: dict, channel_settings: dict | None) -> float:
        if channel_settings and channel_settings.get("response_chance") is not None:
            return float(channel_settings["response_chance"])
        return float(guild_settings["global_response_chance"])

    def _effective_gif_chance(self, guild_settings: dict, channel_settings: dict | None) -> float:
        if channel_settings and channel_settings.get("gif_response_chance") is not None:
            return float(channel_settings["gif_response_chance"])
        return float(guild_settings["gif_response_chance"])

    def seconds_remaining_on_cooldown(self, channel_id: int, cooldown_seconds: int) -> float:
        last = self._last_response_at.get(channel_id)
        if last is None:
            return 0.0
        return max(0.0, cooldown_seconds - (time.monotonic() - last))

    def _on_cooldown(self, channel_id: int, cooldown_seconds: int) -> bool:
        return self.seconds_remaining_on_cooldown(channel_id, cooldown_seconds) > 0

    def _mark_responded(self, channel_id: int):
        self._last_response_at[channel_id] = time.monotonic()

    async def should_respond(self, message: discord.Message) -> bool:
        channel_settings = await self.db.get_channel_settings(message.channel.id)
        if not channel_settings or not channel_settings["responses_enabled"]:
            return False

        guild_settings = await self.db.get_guild_settings(message.guild.id)

        if self._on_cooldown(message.channel.id, guild_settings["cooldown_seconds"]):
            return False

        chance = self._effective_response_chance(guild_settings, channel_settings)
        roll = random.uniform(0, 100)
        return roll < chance

    async def take_queued(self, channel_id: int) -> dict | None:
        """An admin-queued message for this channel, if one is waiting (consumes it)."""
        content = await self.db.pop_queued(channel_id)
        if not content:
            return None
        self._mark_responded(channel_id)
        return {"text": content, "gif_url": None}

    async def build_response(self, message: discord.Message):
        return await self.generate_response(message.guild.id, message.channel.id)

    async def generate_response(self, guild_id: int, channel_id: int):
        """
        Returns a dict: {"text": str|None, "gif_url": str|None}
        or None if nothing generatable was found in this channel's corpus.
        """
        guild_settings = await self.db.get_guild_settings(guild_id)
        channel_settings = await self.db.get_channel_settings(channel_id)

        gif_enabled = bool(guild_settings["gif_enabled"])
        gif_chance = self._effective_gif_chance(guild_settings, channel_settings) if gif_enabled else 0

        result = await self._compose(
            corpus_loader=lambda: self.db.get_corpus(channel_id=channel_id),
            gif_loader=lambda: self.db.get_gifs(channel_id=channel_id),
            gif_chance=gif_chance,
            gen_settings=guild_settings,
            dedupe_scope=guild_id,
        )
        if result:
            self._mark_responded(channel_id)
        return result

    async def generate_dm_response(self):
        """A response for a DM, drawn from every server (except channels hidden via /channel dms)."""
        g = CONFIG["gif"]
        r = CONFIG["response"]
        gen_settings = {
            "generation_mode": CONFIG["generation"]["mode"],
            "min_words": r["min_words"],
            "max_words": r["max_words"],
        }
        return await self._compose(
            corpus_loader=lambda: self.db.get_dm_corpus(),
            gif_loader=lambda: self.db.get_dm_gifs(),
            gif_chance=g["response_chance"] if g["enabled"] else 0,
            gen_settings=gen_settings,
            dedupe_scope=DM_SCOPE,
        )

    async def _compose(self, corpus_loader, gif_loader, gif_chance: float, gen_settings: dict, dedupe_scope: int):
        roll = random.uniform(0, 100)
        want_gif_only = gif_chance > 0 and roll < gif_chance
        # small extra chance of text + gif together, only when not already gif-only
        want_gif_with_text = False
        if gif_chance > 0 and not want_gif_only:
            want_gif_with_text = random.uniform(0, 100) < (gif_chance / 4)

        gif_url = None
        if want_gif_only or want_gif_with_text:
            pool = await gif_loader()
            gif_url = random.choice(pool) if pool else None
            if want_gif_only and gif_url:
                return {"text": None, "gif_url": gif_url}
            # no GIFs available: fall through to text instead of responding with nothing

        text = await self._generate_text(await corpus_loader(), gen_settings, dedupe_scope)
        if not text and not gif_url:
            return None
        return {"text": text, "gif_url": gif_url if want_gif_with_text else None}

    async def _pick_gif(self, guild_id: int, channel_id: int, guild_settings: dict) -> str | None:
        pool = await self.db.get_gifs(channel_id=channel_id)
        return random.choice(pool) if pool else None

    async def _generate_text(self, corpus: list[str], gen_settings: dict, dedupe_scope: int,
                             attempts: int = 5) -> str | None:
        if not corpus:
            return None

        for _ in range(attempts):
            text = self.generator.generate(
                corpus,
                mode=gen_settings["generation_mode"],
                min_words=gen_settings["min_words"],
                max_words=gen_settings["max_words"],
            )
            if not text:
                return None
            if not await self.db.was_recently_sent(dedupe_scope, text):
                await self.db.record_response(dedupe_scope, text)
                return text
        # exhausted attempts trying to avoid a repeat; send it anyway
        await self.db.record_response(dedupe_scope, text)
        return text
