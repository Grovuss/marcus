"""
Decides whether an incoming Discord message should be logged into the
text corpus, and cleans up message content before it's stored so Marcus
doesn't learn to ping people, paste raw URLs, or repeat command syntax.
"""
import re

import discord

MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>")
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
URL_RE = re.compile(r"https?://\S+")
COMMAND_PREFIXES = ("!", "?", ".", ";", "-", "$", "%", "~", "/")


def sanitize_content(content: str) -> str:
    """Strip mentions, custom emoji, and URLs; collapse whitespace."""
    text = MENTION_RE.sub("", content)
    text = CUSTOM_EMOJI_RE.sub("", text)
    text = URL_RE.sub("", text)
    text = " ".join(text.split())
    return text


def looks_like_command(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    return stripped.startswith(COMMAND_PREFIXES)


def is_loggable_text(message: discord.Message) -> tuple[bool, str]:
    """
    Returns (should_log, cleaned_text). should_log is False if there's
    nothing worth logging (empty, command-like, bot/system message, or
    an attachment-only message with no real text).
    """
    if message.author.bot:
        return False, ""
    if message.is_system():
        return False, ""
    if looks_like_command(message.content):
        return False, ""

    cleaned = sanitize_content(message.content)
    if not cleaned:
        return False, ""

    # A message that's just an attachment filename slipping through as
    # "content" (rare, but be safe) or otherwise too short to be useful.
    if len(cleaned) < 2:
        return False, ""

    return True, cleaned
