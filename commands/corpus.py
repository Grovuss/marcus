"""
/corpus stats [channel]
/corpus clear [channel] [user] [all]

`clear` is destructive and requires exactly one of channel / user / all
to avoid accidentally wiping everything.
"""
import datetime

import discord
from discord import app_commands
from discord.ext import commands


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "N/A"
    try:
        dt = datetime.datetime.fromisoformat(ts)
        return f"<t:{int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())}:R>"
    except ValueError:
        return ts


class CorpusCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    corpus_group = app_commands.Group(
        name="corpus",
        description="Corpus statistics and management.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @corpus_group.command(name="stats", description="Show corpus statistics for the server or a specific channel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def stats(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        stats = await self.bot.db.get_stats(interaction.guild_id, channel_id=channel.id if channel else None)

        title = f"Corpus Stats - #{channel.name}" if channel else "Corpus Stats - Server-wide"
        embed = discord.Embed(title=title, color=discord.Color.green())
        embed.add_field(name="Messages logged", value=f"{stats['message_count']:,}")
        embed.add_field(name="GIFs logged", value=f"{stats['gif_count']:,}")
        if not channel:
            embed.add_field(name="Channels contributing", value=f"{stats['channel_count']:,}")
        embed.add_field(name="Unique users", value=f"{stats['unique_users']:,}")
        embed.add_field(name="Oldest message", value=_fmt_ts(stats["oldest"]))
        embed.add_field(name="Newest message", value=_fmt_ts(stats["newest"]))

        await interaction.response.send_message(embed=embed)

    @corpus_group.command(name="clear", description="Delete logged data. Specify exactly one of channel / user / all.")
    @app_commands.describe(
        channel="Clear all corpus data logged in this channel",
        user="Clear all corpus data logged by this user (across the server)",
        all="Clear the ENTIRE server corpus - irreversible",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def clear(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel = None,
        user: discord.Member = None,
        all: bool = False,
    ):
        selected = sum(1 for x in (channel, user, all) if x)
        if selected != 1:
            await interaction.response.send_message(
                "Specify exactly one of `channel`, `user`, or `all: True`.", ephemeral=True
            )
            return

        if all:
            msg_count = await self.bot.db.delete_guild_messages(interaction.guild_id)
            gif_count = await self.bot.db.delete_guild_gifs(interaction.guild_id)
            await interaction.response.send_message(
                f"Cleared the **entire server corpus**: {msg_count:,} messages and {gif_count:,} GIFs deleted."
            )
        elif channel:
            msg_count = await self.bot.db.delete_channel_messages(channel.id)
            gif_count = await self.bot.db.delete_channel_gifs(channel.id)
            await interaction.response.send_message(
                f"Cleared corpus for {channel.mention}: {msg_count:,} messages and {gif_count:,} GIFs deleted."
            )
        elif user:
            msg_count = await self.bot.db.delete_user_messages(interaction.guild_id, user.id)
            gif_count = await self.bot.db.delete_user_gifs(interaction.guild_id, user.id)
            await interaction.response.send_message(
                f"Cleared all logged data from {user.mention}: {msg_count:,} messages and {gif_count:,} GIFs deleted."
            )

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need sufficient permissions to use this (Manage Server, "
                "or Administrator for `clear`).", ephemeral=True
            )
        else:
            raise error


async def setup(bot):
    await bot.add_cog(CorpusCog(bot))
