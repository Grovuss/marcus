"""
/message - admin testing/debug command.

/message send text: manually feed a line of text into the corpus, as if
    it had been logged from a real message. Handy for seeding a corpus
    or testing generation without waiting for real chat activity.
/message generate [channel]: generate a test response on demand,
    without needing an incoming message to trigger it. Shown only to
    the admin who ran it (ephemeral) so it doesn't spam the channel.
"""
import discord
from discord import app_commands
from discord.ext import commands

from services.message_logger import sanitize_content


class MessageCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    message_group = app_commands.Group(
        name="message",
        description="Manually feed text into Marcus or test generation.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @message_group.command(name="send", description="Manually log a line of text into this channel's corpus.")
    @app_commands.describe(text="The text to add to the corpus")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def send(self, interaction: discord.Interaction, text: str):
        cleaned = sanitize_content(text)
        if not cleaned:
            await interaction.response.send_message(
                "That text didn't leave anything usable after cleanup (links/mentions get stripped).",
                ephemeral=True,
            )
            return

        await self.bot.db.log_message(
            message_id=f"manual-{interaction.id}",
            channel_id=interaction.channel_id,
            guild_id=interaction.guild_id,
            author_id=interaction.user.id,
            content=cleaned,
        )
        await interaction.response.send_message(
            f"Logged into the corpus for this channel:\n> {cleaned}", ephemeral=True
        )

    @message_group.command(name="generate", description="Generate a test response right now, without waiting for chat activity.")
    @app_commands.describe(channel="Which channel's corpus to draw from (defaults to this channel)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def generate(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        target_channel = channel or interaction.channel
        guild_settings = await self.bot.db.get_guild_settings(interaction.guild_id)

        corpus = await self.bot.db.get_corpus(channel_id=target_channel.id)
        if len(corpus) < 5:
            corpus = await self.bot.db.get_corpus(guild_id=interaction.guild_id)

        if not corpus:
            await interaction.response.send_message(
                "There's no corpus to generate from yet. Log some messages first "
                "(enable a channel with `/channel add` or use `/message send`).",
                ephemeral=True,
            )
            return

        text = self.bot.generator.generate(
            corpus,
            mode=guild_settings["generation_mode"],
            min_words=guild_settings["min_words"],
            max_words=guild_settings["max_words"],
        )
        gif_url = await self.bot.responder._pick_gif(interaction.guild_id, target_channel.id, guild_settings)

        lines = [f"**Test generation** (from #{target_channel.name}'s corpus):"]
        lines.append(text if text else "*(generation failed - corpus may be too small)*")
        if gif_url:
            lines.append(f"Possible GIF pick: {gif_url}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to use this.", ephemeral=True
            )
        else:
            raise error


async def setup(bot):
    await bot.add_cog(MessageCog(bot))
