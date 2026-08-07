"""
/channel add/remove/list/enable/disable

Logging and responding are tracked as separate flags per channel.
`add` turns both on for a channel; `remove` turns both off (existing
logged data is kept - use /corpus clear to actually delete it).
`enable`/`disable` toggle just the response behavior, leaving logging
as-is, for channels where you want Marcus to keep learning quietly
without talking.
"""
import discord
from discord import app_commands
from discord.ext import commands


class ChannelCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    channel_group = app_commands.Group(
        name="channel",
        description="Configure which channels Marcus logs and responds in.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @channel_group.command(name="add", description="Register a channel: logging ON, responses ON.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.add_channel(channel.id, interaction.guild_id)
        await interaction.response.send_message(
            f"{channel.mention} added. Logging: **ON**, Responses: **ON**."
        )

    @channel_group.command(name="remove", description="Unregister a channel: logging OFF, responses OFF.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.remove_channel(channel.id)
        await interaction.response.send_message(
            f"{channel.mention} removed. Logging: **OFF**, Responses: **OFF**. "
            f"(Previously logged data was kept - use `/corpus clear` to delete it.)"
        )

    @channel_group.command(name="enable", description="Turn Marcus's responses ON in a channel (logging unaffected).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def enable(self, interaction: discord.Interaction, channel: discord.TextChannel):
        settings = await self.bot.db.get_channel_settings(channel.id)
        if not settings:
            await interaction.response.send_message(
                f"{channel.mention} isn't registered yet. Use `/channel add` first.",
                ephemeral=True,
            )
            return
        await self.bot.db.set_channel_responses_enabled(channel.id, True)
        await interaction.response.send_message(f"Responses enabled in {channel.mention}.")

    @channel_group.command(name="disable", description="Turn Marcus's responses OFF in a channel (logging unaffected).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable(self, interaction: discord.Interaction, channel: discord.TextChannel):
        settings = await self.bot.db.get_channel_settings(channel.id)
        if not settings:
            await interaction.response.send_message(
                f"{channel.mention} isn't registered yet. Use `/channel add` first.",
                ephemeral=True,
            )
            return
        await self.bot.db.set_channel_responses_enabled(channel.id, False)
        await interaction.response.send_message(f"Responses disabled in {channel.mention}.")

    @channel_group.command(name="list", description="List all configured channels and their status.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_channels(self, interaction: discord.Interaction):
        rows = await self.bot.db.list_channel_settings(interaction.guild_id)
        if not rows:
            await interaction.response.send_message(
                "No channels configured yet. Use `/channel add #channel` to get started.",
                ephemeral=True,
            )
            return

        guild_settings = await self.bot.db.get_guild_settings(interaction.guild_id)
        lines = []
        for row in rows:
            channel_obj = interaction.guild.get_channel(int(row["channel_id"]))
            name = channel_obj.mention if channel_obj else f"`{row['channel_id']}` (not found)"
            logging_state = "ON" if row["logging_enabled"] else "OFF"
            responses_state = "ON" if row["responses_enabled"] else "OFF"
            chance = row["response_chance"]
            chance_str = f"{chance:g}%" if chance is not None else f"{guild_settings['global_response_chance']:g}% (default)"
            lines.append(f"{name} - Logging: **{logging_state}**, Responses: **{responses_state}**, Chance: **{chance_str}**")

        embed = discord.Embed(title="Marcus - Configured Channels", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to use this.", ephemeral=True
            )
        else:
            raise error


async def setup(bot):
    await bot.add_cog(ChannelCog(bot))
