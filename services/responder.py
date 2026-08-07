"""
Core response decision-making: probability roll, cooldown enforcement,
text-vs-GIF-vs-both selection, corpus fetching, generation, and
duplicate-response prevention.

Cooldown state is kept in memory (per channel) since it's short-lived
and doesn't need to survive a restart.
"""
import random
import time

import discord

from database import Database
from generator.generator import ResponseGenerator


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

    def _on_cooldown(self, channel_id: int, cooldown_seconds: int) -> bool:
        last = self._last_response_at.get(channel_id)
        if last is None:
            return False
        return (time.monotonic() - last) < cooldown_seconds

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

    async def build_response(self, message: discord.Message):
        """
        Returns a dict: {"text": str|None, "gif_url": str|None}
        or None if nothing generatable was found.
        """
        guild_settings = await self.db.get_guild_settings(message.guild.id)
        channel_settings = await self.db.get_channel_settings(message.channel.id)

        gif_enabled = bool(guild_settings["gif_enabled"])
        gif_chance = self._effective_gif_chance(guild_settings, channel_settings) if gif_enabled else 0

        roll = random.uniform(0, 100)
        want_gif_only = gif_enabled and roll < gif_chance
        # small extra chance of text + gif together, only when not already gif-only
        want_gif_with_text = False
        if gif_enabled and not want_gif_only:
            want_gif_with_text = random.uniform(0, 100) < (gif_chance / 4)

        gif_url = None
        if want_gif_only or want_gif_with_text:
            gif_url = await self._pick_gif(message.guild.id, message.channel.id, guild_settings)
            if want_gif_only and gif_url:
                self._mark_responded(message.channel.id)
                return {"text": None, "gif_url": gif_url}
            if want_gif_only and not gif_url:
                # fall through to text generation instead of responding with nothing
                want_gif_only = False

        text = await self._generate_text(message.guild.id, message.channel.id, guild_settings)
        if not text and not gif_url:
            return None

        self._mark_responded(message.channel.id)
        return {"text": text, "gif_url": gif_url if want_gif_with_text else None}

    async def _pick_gif(self, guild_id: int, channel_id: int, guild_settings: dict) -> str | None:
        prefer_local = bool(guild_settings["gif_channel_local_preference"])
        if prefer_local:
            local = await self.db.get_gifs(channel_id=channel_id)
            if local:
                return random.choice(local)
        pool = await self.db.get_gifs(guild_id=guild_id)
        if pool:
            return random.choice(pool)
        return None

    async def _generate_text(self, guild_id: int, channel_id: int, guild_settings: dict, attempts: int = 5) -> str | None:
        corpus = await self.db.get_corpus(channel_id=channel_id)
        if len(corpus) < 5:
            # not enough channel-local material yet, widen to the whole server
            corpus = await self.db.get_corpus(guild_id=guild_id)
        if not corpus:
            return None

        for _ in range(attempts):
            text = self.generator.generate(
                corpus,
                mode=guild_settings["generation_mode"],
                min_words=guild_settings["min_words"],
                max_words=guild_settings["max_words"],
            )
            if not text:
                return None
            if not await self.db.was_recently_sent(guild_id, text):
                await self.db.record_response(guild_id, text)
                return text
        # exhausted attempts trying to avoid a repeat; send it anyway
        await self.db.record_response(guild_id, text)
        return text
