"""
/responsechance [channel] [value]

No args        -> show the server-wide default response chance.
channel only    -> show that channel's effective response chance.
channel + value -> set that channel's response chance (0-100).
"""
import discord
from discord import app_commands
from discord.ext import commands


class ResponseChanceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="responsechance", description="View or set Marcus's response chance.")
    @app_commands.describe(
        channel="Channel to view/set (omit to view the server-wide default)",
        value="New response chance as a percent, 0-100 (omit to just view)",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def responsechance(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel = None,
        value: app_commands.Range[float, 0, 100] = None,
    ):
        guild_settings = await self.bot.db.get_guild_settings(interaction.guild_id)

        # No channel given: view or set the server-wide default.
        if channel is None:
            if value is None:
                await interaction.response.send_message(
                    f"Server-wide default response chance: **{guild_settings['global_response_chance']:g}%**"
                )
            else:
                await self.bot.db.set_guild_setting(interaction.guild_id, "global_response_chance", float(value))
                await interaction.response.send_message(
                    f"Server-wide default response chance set to **{value:g}%**."
                )
            return

        # Channel given.
        channel_settings = await self.bot.db.get_channel_settings(channel.id)
        if not channel_settings:
            await interaction.response.send_message(
                f"{channel.mention} isn't configured yet. Use `/channel add` first.",
                ephemeral=True,
            )
            return

        if value is None:
            effective = channel_settings["response_chance"]
            if effective is None:
                await interaction.response.send_message(
                    f"{channel.mention} has no channel-specific override - using the "
                    f"server default of **{guild_settings['global_response_chance']:g}%**."
                )
            else:
                await interaction.response.send_message(
                    f"{channel.mention} response chance: **{effective:g}%**"
                )
            return

        await self.bot.db.set_channel_response_chance(channel.id, float(value))
        await interaction.response.send_message(
            f"{channel.mention} response chance set to **{value:g}%**."
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to use this.", ephemeral=True
            )
        else:
            raise error


async def setup(bot):
    await bot.add_cog(ResponseChanceCog(bot))
