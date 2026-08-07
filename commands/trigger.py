"""
/trigger [channel] - forces Marcus to post a real, public generated
response right now, instead of waiting on the random response-chance
roll. Open to any member (this is a fun/toy command, not admin config),
but still respects the channel's cooldown so it can't be used to spam.
"""
import math

import discord
from discord import app_commands
from discord.ext import commands


class TriggerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="trigger", description="Force Marcus to post a generated response right now.")
    @app_commands.describe(channel="Which channel to post in (defaults to this channel)")
    @app_commands.guild_only()
    async def trigger(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        target = channel or interaction.channel

        channel_settings = await self.bot.db.get_channel_settings(target.id)
        if not channel_settings or not channel_settings["responses_enabled"]:
            await interaction.response.send_message(
                f"Marcus isn't set up to respond in {target.mention}. "
                f"An admin can enable it with `/channel add` or `/channel enable`.",
                ephemeral=True,
            )
            return

        guild_settings = await self.bot.db.get_guild_settings(interaction.guild_id)
        remaining = self.bot.responder.seconds_remaining_on_cooldown(target.id, guild_settings["cooldown_seconds"])
        if remaining > 0:
            await interaction.response.send_message(
                f"Marcus is on cooldown in {target.mention} for another {math.ceil(remaining)}s.",
                ephemeral=True,
            )
            return

        result = await self.bot.responder.generate_response(interaction.guild_id, target.id)
        if not result:
            await interaction.response.send_message(
                f"Couldn't generate anything from {target.mention}'s corpus - it's probably too small yet.",
                ephemeral=True,
            )
            return

        # Acknowledge the interaction quietly, then post the real response
        # as a normal channel message so it looks like an organic Marcus post.
        await interaction.response.send_message("triggered.", ephemeral=True)
        await self.bot._send_response(target, result)


async def setup(bot):
    await bot.add_cog(TriggerCog(bot))
