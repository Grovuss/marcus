"""
/marcus - a general info/debug command showing corpus size, channel
count, and the current effective configuration at a glance.
"""
import discord
from discord import app_commands
from discord.ext import commands


class MarcusInfoCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="marcus", description="Show Marcus's status and configuration for this server.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    async def marcus_info(self, interaction: discord.Interaction):
        guild_settings = await self.bot.db.get_guild_settings(interaction.guild_id)
        stats = await self.bot.db.get_stats(interaction.guild_id)

        embed = discord.Embed(
            title=f"🧠 {self.bot.bot_name}",
            description="Server-specific brainrot chatbot, powered entirely by your own corpus.",
            color=discord.Color.purple(),
        )
        embed.add_field(name="Messages learned", value=f"{stats['message_count']:,}")
        embed.add_field(name="GIFs learned", value=f"{stats['gif_count']:,}")
        embed.add_field(name="Channels contributing", value=f"{stats['channel_count']:,}")
        embed.add_field(name="Response chance (default)", value=f"{guild_settings['global_response_chance']:g}%")
        embed.add_field(name="Cooldown", value=f"{guild_settings['cooldown_seconds']} seconds")
        embed.add_field(name="Generation mode", value=guild_settings["generation_mode"])
        embed.add_field(name="Markov order", value=str(guild_settings["markov_order"]))
        embed.add_field(name="GIF responses", value=("enabled" if guild_settings["gif_enabled"] else "disabled") +
                         f" ({guild_settings['gif_response_chance']:g}% of responses)")
        embed.set_footer(text="Use /channel list, /corpus stats, and /responsechance for more detail.")

        await interaction.response.send_message(embed=embed)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to use this.", ephemeral=True
            )
        else:
            raise error


async def setup(bot):
    await bot.add_cog(MarcusInfoCog(bot))
