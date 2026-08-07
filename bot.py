"""
Marcus - a server-specific "learns from the community" Discord bot.

Logs messages/GIFs from configured channels into a local SQLite corpus,
then occasionally recombines that corpus into (deliberately) stupid
generated responses. No external LLM or cloud AI API is used anywhere.

Run with: python bot.py
"""
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from config import CONFIG
from database import Database
from generator.generator import ResponseGenerator
from services.message_logger import is_loggable_text
from services.gif_logger import extract_gif_urls
from services.responder import Responder

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("marcus")

EXTENSIONS = [
    "commands.message",
    "commands.responsechance",
    "commands.channel",
    "commands.corpus",
    "commands.marcus",
    "commands.trigger",
]

DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")


class MarcusBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        # members intent is not required for anything Marcus does

        super().__init__(command_prefix="!marcus-unused!", intents=intents)

        self.bot_name = CONFIG["bot"]["name"]
        self.db = Database()
        self.generator = ResponseGenerator(order=CONFIG["generation"]["markov_order"])
        self.responder = Responder(self.db, self.generator)

    async def setup_hook(self):
        await self.db.connect()
        log.info("Database connected.")

        for ext in EXTENSIONS:
            await self.load_extension(ext)
            log.info("Loaded extension: %s", ext)

        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to dev guild %s", len(synced), DEV_GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands.", len(synced))

    async def on_ready(self):
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)
        activity = discord.Activity(type=discord.ActivityType.watching, name="the server lose its mind")
        await self.change_presence(activity=activity)

    async def close(self):
        await self.db.close()
        await super().close()

    async def on_message(self, message: discord.Message):
        # Ignore ourselves and every other bot outright.
        if message.author.bot:
            return
        if not message.guild:
            return  # DMs are out of scope

        channel_settings = await self.db.get_channel_settings(message.channel.id)

        # --- Logging ---
        if channel_settings and channel_settings["logging_enabled"]:
            should_log, cleaned = is_loggable_text(message)
            if should_log:
                await self.db.log_message(
                    message_id=message.id,
                    channel_id=message.channel.id,
                    guild_id=message.guild.id,
                    author_id=message.author.id,
                    content=cleaned,
                )

            for url in extract_gif_urls(message):
                await self.db.log_gif(
                    message_id=message.id,
                    channel_id=message.channel.id,
                    guild_id=message.guild.id,
                    author_id=message.author.id,
                    url=url,
                )

        # --- Responding ---
        if await self.responder.should_respond(message):
            result = await self.responder.build_response(message)
            if result:
                await self._send_response(message.channel, result)

    async def _send_response(self, channel: discord.abc.Messageable, result: dict):
        parts = []
        if result.get("text"):
            parts.append(result["text"])
        if result.get("gif_url"):
            parts.append(result["gif_url"])
        content = "\n".join(parts)
        if not content:
            return
        await channel.send(content)


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill in your bot token."
        )

    bot = MarcusBot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
