"""
Finds GIF URLs in a Discord message, whatever form they arrive in:
Tenor/Giphy links pasted or unfurled as embeds, direct .gif attachments,
or Discord's own embedded gifv-style previews.
"""
import re

import discord

GIF_HOST_RE = re.compile(r"(tenor\.com|giphy\.com|media\.discordapp\.net.*\.gif|media\.tenor\.com)", re.I)
GIF_URL_RE = re.compile(r"https?://\S+\.gif(\?\S*)?", re.I)
TENOR_GIPHY_LINK_RE = re.compile(r"https?://(?:www\.)?(?:tenor\.com|giphy\.com)/\S+", re.I)


def extract_gif_urls(message: discord.Message) -> list[str]:
    urls: list[str] = []

    # 1. Direct .gif file attachments
    for attachment in message.attachments:
        filename = (attachment.filename or "").lower()
        content_type = (attachment.content_type or "").lower()
        if filename.endswith(".gif") or "gif" in content_type:
            urls.append(attachment.url)

    # 2. Rich embeds (Tenor/Giphy links unfurl into embeds, gifv previews, etc.)
    for embed in message.embeds:
        candidates = []
        if embed.url:
            candidates.append(embed.url)
        if embed.video and embed.video.url:
            candidates.append(embed.video.url)
        if embed.thumbnail and embed.thumbnail.url:
            candidates.append(embed.thumbnail.url)
        if getattr(embed, "type", None) == "gifv" and embed.url:
            candidates.append(embed.url)

        for url in candidates:
            if url and (GIF_URL_RE.search(url) or GIF_HOST_RE.search(url)):
                urls.append(url)

    # 3. Raw Tenor/Giphy links or .gif URLs pasted directly in the message text
    content = message.content or ""
    for match in TENOR_GIPHY_LINK_RE.findall(content):
        urls.append(match)
    for m in GIF_URL_RE.finditer(content):
        urls.append(m.group(0))

    # de-duplicate while preserving order
    seen = set()
    deduped = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped
