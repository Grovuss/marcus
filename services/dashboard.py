"""
Password-protected admin dashboard, served from the same process as the
bot so it can see live guild/channel state and send messages directly.

  /                  - every server Marcus is in, plus search over everything
  /search?q=         - saved messages matching q across every server
  /g/{guild_id}      - that server's text channels + how much is logged
  /c/{channel_id}    - one channel: send/queue a message, add to memory,
                       browse/search/delete what's saved there
  /filter            - words/phrases/links Marcus must never remember

Uses aiohttp, which discord.py already depends on. Disabled entirely
unless DASHBOARD_PASSWORD is set. Login is HTTP Basic auth (any
username, that password).
"""
import base64
import hashlib
import hmac
import html
import logging
import os
import re
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit

import discord
from aiohttp import web

from services.gif_logger import GIF_URL_RE, TENOR_GIPHY_LINK_RE
from services.message_logger import sanitize_content

log = logging.getLogger("marcus.dashboard")

PAGE_SIZE = 100
MAX_MESSAGE_LEN = 2000
MAX_FILTER_LEN = 200

TENOR_ID_RE = re.compile(r"tenor\.com/(?:[a-z-]+/)?view/\S*?-(\d+)(?:[/?#]|$)", re.I)
GIPHY_ID_RE = re.compile(r"giphy\.com/gifs/(?:\S*-)?([A-Za-z0-9]+)(?:[/?#]|$)", re.I)
VIDEO_RE = re.compile(r"\.(mp4|webm)(\?|$)", re.I)
MEDIA_GIF_RE = re.compile(r"https?://media\d*\.(?:tenor|giphy)\.com/\S+", re.I)

CSS = """
:root { --bg:#f6f6f8; --card:#fff; --fg:#1c1c22; --muted:#6b6b76; --line:#e2e2e8;
        --accent:#5865f2; --ok:#1f8a4c; --err:#c0392b; --mark:#ffe58a; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16171b; --card:#1f2026; --fg:#e8e8ec; --muted:#9a9aa6; --line:#2e2f37;
          --accent:#7b86ff; --ok:#3fbf7a; --err:#ff6b5e; --mark:#6b5a12; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:960px; margin:0 auto; padding:24px 16px 64px; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:16px; margin:28px 0 10px; }
mark { background:var(--mark); color:inherit; border-radius:3px; padding:0 1px; }
nav { display:flex; gap:16px; margin-bottom:20px; font-weight:600; }
.crumbs { color:var(--muted); font-size:13px; margin-bottom:16px; }
.muted { color:var(--muted); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; }
.pad { padding:14px; }
table { width:100%; border-collapse:collapse; }
th, td { text-align:left; padding:10px 14px; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); font-weight:600; }
tr:last-child td { border-bottom:none; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.tag { display:inline-block; font-size:12px; padding:1px 8px; border-radius:99px;
       border:1px solid var(--line); color:var(--muted); margin-right:4px; font-weight:400; }
.tag.on { color:var(--ok); border-color:var(--ok); }
form.stack { display:flex; flex-direction:column; gap:10px; }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.row.end { justify-content:flex-end; }
textarea, input[type=text], input[type=search] { width:100%; padding:9px 10px; font:inherit; color:var(--fg);
       background:var(--bg); border:1px solid var(--line); border-radius:8px; }
textarea { min-height:80px; resize:vertical; }
.row input[type=search], .row input[type=text] { flex:1; min-width:180px; width:auto; }
button { background:var(--accent); color:#fff; border:1px solid var(--accent); border-radius:8px;
         padding:8px 16px; font:inherit; font-weight:600; cursor:pointer; }
button.ghost { background:transparent; color:var(--accent); }
button.x { background:transparent; color:var(--muted); border:1px solid var(--line); padding:0 8px;
           font-size:12px; font-weight:400; line-height:22px; }
button.x:hover { color:var(--err); border-color:var(--err); }
form.inline { display:inline; margin:0; }
.flash { padding:10px 14px; border-radius:8px; margin-bottom:16px; border:1px solid; }
.flash.ok { color:var(--ok); border-color:var(--ok); }
.flash.err { color:var(--err); border-color:var(--err); }
.pager { display:flex; justify-content:space-between; margin-top:12px; }
.entry { padding:12px 14px; border-bottom:1px solid var(--line); }
.entry:last-child { border-bottom:none; }
.meta { font-size:13px; color:var(--muted); margin-bottom:4px; }
.meta b { color:var(--fg); }
.item { display:flex; gap:10px; align-items:flex-start; margin-top:4px; }
.item .body { flex:1; white-space:pre-wrap; word-break:break-word; }
.gif { flex:1; }
.gif img, .gif video { max-width:260px; max-height:220px; border-radius:6px; display:block; }
.gif iframe { width:260px; height:200px; border:0; border-radius:6px; display:block; }
.gif a { font-size:12px; word-break:break-all; }
.queue li { margin:6px 0; }
label.check { display:flex; gap:6px; align-items:center; font-size:14px; }
"""


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def highlight(text: str, query: str | None) -> str:
    """HTML-escape text, wrapping case-insensitive matches of query in <mark>."""
    if not query:
        return esc(text)
    parts = re.split(f"({re.escape(query)})", text, flags=re.IGNORECASE)
    return "".join(f"<mark>{esc(p)}</mark>" if i % 2 else esc(p) for i, p in enumerate(parts))


def gif_html(url: str) -> str:
    """Render a saved GIF URL as something visible in the browser."""
    safe = esc(url)
    if m := TENOR_ID_RE.search(url):
        media = f"<iframe src='https://tenor.com/embed/{m.group(1)}' loading='lazy' allowfullscreen></iframe>"
    elif (m := GIPHY_ID_RE.search(url)) and "media" not in urlsplit(url).netloc:
        media = f"<img src='https://media.giphy.com/media/{esc(m.group(1))}/giphy.gif' loading='lazy' alt=''>"
    elif VIDEO_RE.search(url):
        media = f"<video src='{safe}' autoplay loop muted playsinline></video>"
    else:
        media = f"<img src='{safe}' loading='lazy' alt=''>"
    return f"{media}<a href='{safe}' target='_blank' rel='noopener noreferrer'>{safe}</a>"


def looks_like_gif_link(text: str) -> bool:
    text = text.strip()
    if not text or any(c.isspace() for c in text):
        return False
    return bool(TENOR_GIPHY_LINK_RE.fullmatch(text) or GIF_URL_RE.fullmatch(text) or MEDIA_GIF_RE.fullmatch(text))


def page(title: str, body: str, request: web.Request | None = None) -> web.Response:
    flash = ""
    if request is not None:
        if request.query.get("ok"):
            flash = f"<div class='flash ok'>{esc(request.query['ok'])}</div>"
        elif request.query.get("error"):
            flash = f"<div class='flash err'>{esc(request.query['error'])}</div>"
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)} · Marcus</title><style>{CSS}</style></head>"
        "<body><main><nav><a href='/'>Servers</a><a href='/search'>Search</a>"
        f"<a href='/filter'>Memory filter</a></nav>{flash}{body}</main></body></html>"
    )
    return web.Response(text=doc, content_type="text/html")


def redirect(back: str, **params) -> web.HTTPSeeOther:
    """Redirect to a local URL, replacing any previous ok/error flash with the given one."""
    parts = urlsplit(back or "/")
    path = parts.path if parts.path.startswith("/") and not parts.path.startswith("//") else "/"
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("ok", "error")]
    query += [(k, v) for k, v in params.items() if v]
    return web.HTTPSeeOther(path + ("?" + urlencode(query) if query else ""))


class Dashboard:
    def __init__(self, bot, password: str, host: str = "0.0.0.0", port: int = 8080):
        self.bot = bot
        self.password = password
        self.host = host
        self.port = port
        self.csrf_token = hmac.new(password.encode(), b"marcus-dashboard-csrf", hashlib.sha256).hexdigest()
        self._runner: web.AppRunner | None = None
        self._user_names: dict[str, str] = {}

        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get("/", self.index)
        app.router.add_get("/search", self.search_view)
        app.router.add_get("/g/{guild_id}", self.guild_view)
        app.router.add_get("/c/{channel_id}", self.channel_view)
        app.router.add_get("/filter", self.filter_view)
        app.router.add_post("/c/{channel_id}/send", self.send_message)
        app.router.add_post("/c/{channel_id}/memory", self.add_memory)
        app.router.add_post("/queue/{queue_id}/delete", self.delete_queued)
        app.router.add_post("/item/delete", self.delete_item)
        app.router.add_post("/filter/add", self.add_filter)
        app.router.add_post("/filter/{filter_id}/delete", self.delete_filter)
        self.app = app

    async def start(self):
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        log.info("Dashboard listening on http://%s:%d", self.host, self.port)

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        if not self._authorized(request.headers.get("Authorization", "")):
            return web.Response(
                status=401,
                text="Login required.",
                headers={"WWW-Authenticate": 'Basic realm="Marcus dashboard", charset="UTF-8"'},
            )
        if request.method == "POST":
            form = await request.post()
            if not hmac.compare_digest(str(form.get("csrf", "")), self.csrf_token):
                raise web.HTTPForbidden(text="Bad form token - reload the page and try again.")
        return await handler(request)

    def _authorized(self, header: str) -> bool:
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        _, _, supplied = decoded.partition(":")
        return hmac.compare_digest(supplied.encode(), self.password.encode())

    def _form(self, action: str, inner: str, cls: str = "inline", back: str | None = None) -> str:
        back_field = f"<input type='hidden' name='back' value='{esc(back)}'>" if back else ""
        return (
            f"<form class='{cls}' method='post' action='{esc(action)}'>"
            f"<input type='hidden' name='csrf' value='{self.csrf_token}'>{back_field}{inner}</form>"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _author_name(self, guild: discord.Guild | None, author_id: str) -> str:
        if author_id in self._user_names:
            return self._user_names[author_id]
        name = author_id
        try:
            uid = int(author_id)
            member = guild.get_member(uid) if guild else None
            user = member or self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            name = getattr(user, "display_name", None) or user.name
        except (ValueError, discord.HTTPException):
            pass
        self._user_names[author_id] = name
        return name

    @staticmethod
    def _int_param(request: web.Request, key: str) -> int:
        try:
            return int(request.match_info[key])
        except ValueError:
            raise web.HTTPNotFound()

    @staticmethod
    def _page_no(request: web.Request) -> int:
        try:
            return max(1, int(request.query.get("page", "1")))
        except ValueError:
            return 1

    @staticmethod
    def _sendable(channel) -> bool:
        guild = getattr(channel, "guild", None)
        return (
            isinstance(channel, discord.abc.Messageable)
            and guild is not None
            and channel.permissions_for(guild.me).send_messages
        )

    def _search_box(self, action: str, query: str, placeholder: str) -> str:
        return (
            f"<form method='get' action='{esc(action)}' class='row'>"
            f"<input type='search' name='q' value='{esc(query)}' placeholder='{esc(placeholder)}'>"
            "<button type='submit'>Search</button>"
            + (f"<a href='{esc(action)}'>Clear</a>" if query else "")
            + "</form>"
        )

    async def _timeline_html(self, request: web.Request, channel_id: int | None, query: str,
                             show_channel: bool) -> str:
        page_no = self._page_no(request)
        entries = await self.bot.db.get_timeline(
            channel_id=channel_id, query=query or None, limit=PAGE_SIZE + 1, offset=(page_no - 1) * PAGE_SIZE
        )
        has_more = len(entries) > PAGE_SIZE
        entries = entries[:PAGE_SIZE]
        back = request.path_qs

        blocks = []
        for e in entries:
            channel = self.bot.get_channel(int(e["channel_id"]))
            guild = getattr(channel, "guild", None) or self.bot.get_guild(int(e["guild_id"]))
            author = await self._author_name(guild, e["author_id"])
            ts = (e["timestamp"] or "")[:16].replace("T", " ")
            mid = str(e["message_id"])
            source = ""
            if mid.startswith("dash-"):
                source = " <span class='tag'>added from dashboard</span>"
            elif mid.startswith("manual-"):
                source = " <span class='tag'>/message send</span>"
            where = ""
            if show_channel:
                cname = f"#{channel.name}" if channel else f"channel {e['channel_id']}"
                gname = guild.name if guild else f"server {e['guild_id']}"
                where = f" · <a href='/c/{e['channel_id']}'>{esc(cname)}</a> in {esc(gname)}"

            items = []
            for t in e["texts"]:
                items.append(
                    "<div class='item'>"
                    f"<div class='body'>{highlight(t['content'], query)}</div>"
                    + self._form("/item/delete", f"<input type='hidden' name='kind' value='message'>"
                                 f"<input type='hidden' name='id' value='{t['id']}'>"
                                 "<button class='x' title='Forget this message'>forget</button>", back=back)
                    + "</div>"
                )
            for g in e["gifs"]:
                items.append(
                    f"<div class='item'><div class='gif'>{gif_html(g['url'])}</div>"
                    + self._form("/item/delete", f"<input type='hidden' name='kind' value='gif'>"
                                 f"<input type='hidden' name='id' value='{g['id']}'>"
                                 "<button class='x' title='Forget this GIF'>forget</button>", back=back)
                    + "</div>"
                )
            blocks.append(
                f"<div class='entry'><div class='meta'><b>{esc(author)}</b> · {esc(ts)} UTC{where}{source}</div>"
                + "".join(items) + "</div>"
            )

        empty = "No saved messages match that search." if query else "Nothing saved here yet."
        listing = f"<div class='card'>{''.join(blocks) or f'<div class=pad><span class=muted>{empty}</span></div>'}</div>"

        def page_link(n):
            params = {"page": n}
            if query:
                params["q"] = query
            return f"{request.path}?{urlencode(params)}"

        pager = "<div class='pager'>"
        pager += f"<a href='{esc(page_link(page_no - 1))}'>← Newer</a>" if page_no > 1 else "<span></span>"
        pager += f"<a href='{esc(page_link(page_no + 1))}'>Older →</a>" if has_more else "<span></span>"
        pager += "</div>"
        return listing + pager

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    async def index(self, request: web.Request):
        rows = []
        for guild in sorted(self.bot.guilds, key=lambda g: g.name.lower()):
            stats = await self.bot.db.get_stats(guild.id)
            logged = sum(1 for c in await self.bot.db.list_channel_settings(guild.id) if c["logging_enabled"])
            rows.append(
                f"<tr><td><a href='/g/{guild.id}'>{esc(guild.name)}</a>"
                f"<div class='muted'>{guild.id}</div></td>"
                f"<td class='num'>{guild.member_count or '—'}</td>"
                f"<td class='num'>{logged}</td>"
                f"<td class='num'>{stats['message_count']}</td>"
                f"<td class='num'>{stats['gif_count']}</td></tr>"
            )
        table = (
            "<div class='card'><table><tr><th>Server</th><th class='num'>Members</th>"
            "<th class='num'>Logged channels</th><th class='num'>Messages</th><th class='num'>GIFs</th></tr>"
            + ("".join(rows) or "<tr><td colspan='5' class='muted'>Marcus isn't in any servers.</td></tr>")
            + "</table></div>"
        )
        me = esc(self.bot.user) if self.bot.user else "Marcus"
        body = (
            f"<h1>Servers</h1><div class='crumbs'>Logged in as {me} · {len(self.bot.guilds)} servers</div>"
            + self._search_box("/search", "", "Search saved messages in every server…")
            + f"<h2>Servers</h2>{table}"
        )
        return page("Servers", body, request)

    async def search_view(self, request: web.Request):
        query = request.query.get("q", "").strip()
        body = "<h1>Search</h1><div class='crumbs'>Every server and channel</div>"
        body += self._search_box("/search", query, "Search saved messages in every server…")
        if query:
            body += f"<h2>Results for “{esc(query)}”</h2>"
            body += await self._timeline_html(request, None, query, show_channel=True)
        return page("Search", body, request)

    async def guild_view(self, request: web.Request):
        guild = self.bot.get_guild(self._int_param(request, "guild_id"))
        if guild is None:
            raise web.HTTPNotFound(text="Marcus isn't in that server.")

        settings = {c["channel_id"]: c for c in await self.bot.db.list_channel_settings(guild.id)}
        counts = await self.bot.db.count_by_channel(guild.id)

        rows = []
        seen = set()
        for channel in guild.text_channels:
            cid = str(channel.id)
            seen.add(cid)
            rows.append(self._channel_row(cid, f"#{channel.name}", channel.category, settings.get(cid), counts.get(cid)))
        # Channels that have logged data but no longer exist (deleted, or Marcus lost access).
        for cid in sorted(set(counts) - seen):
            rows.append(self._channel_row(cid, f"unknown channel {cid}", None, settings.get(cid), counts.get(cid)))

        table = (
            "<div class='card'><table><tr><th>Channel</th><th>Status</th>"
            "<th class='num'>Messages</th><th class='num'>GIFs</th></tr>" + "".join(rows) + "</table></div>"
        )
        body = (
            f"<div class='crumbs'><a href='/'>Servers</a> / {esc(guild.name)}</div>"
            f"<h1>{esc(guild.name)}</h1><div class='crumbs'>{guild.id}</div>{table}"
        )
        return page(guild.name, body, request)

    @staticmethod
    def _channel_row(cid, label, category, setting, count) -> str:
        tags = ""
        if setting and setting["logging_enabled"]:
            tags += "<span class='tag on'>logging</span>"
        if setting and setting["responses_enabled"]:
            tags += "<span class='tag on'>responding</span>"
        if not tags:
            tags = "<span class='tag'>off</span>"
        count = count or {"messages": 0, "gifs": 0}
        cat = f"<div class='muted'>{esc(category.name)}</div>" if category else ""
        return (
            f"<tr><td><a href='/c/{cid}'>{esc(label)}</a>{cat}</td><td>{tags}</td>"
            f"<td class='num'>{count['messages']}</td><td class='num'>{count['gifs']}</td></tr>"
        )

    async def channel_view(self, request: web.Request):
        channel_id = self._int_param(request, "channel_id")
        channel = self.bot.get_channel(channel_id)
        guild = getattr(channel, "guild", None)
        query = request.query.get("q", "").strip()

        name = f"#{channel.name}" if channel else f"unknown channel {channel_id}"
        crumbs = "<a href='/'>Servers</a>"
        if guild:
            crumbs += f" / <a href='/g/{guild.id}'>{esc(guild.name)}</a>"
        crumbs += f" / {esc(name)}"

        # Send / queue
        if self._sendable(channel):
            send = self._form(
                f"/c/{channel_id}/send",
                f"<textarea name='content' maxlength='{MAX_MESSAGE_LEN}' required "
                f"placeholder='Say something as Marcus in {esc(name)}…'></textarea>"
                "<div class='row end'>"
                "<button class='ghost' name='action' value='queue' "
                "title='Marcus posts this the next time someone talks in this channel'>Queue as next message</button>"
                "<button name='action' value='send'>Send now</button></div>",
                cls="stack card pad",
            )
        elif channel is not None and guild is not None:
            send = "<div class='card muted pad'>Marcus doesn't have permission to send messages here.</div>"
        else:
            send = "<div class='card muted pad'>Marcus can't see this channel anymore, so it can't send here.</div>"

        queue = await self.bot.db.list_queue(channel_id)
        queue_html = ""
        if queue:
            lis = "".join(
                f"<li class='row'><span style='flex:1'>{esc(q['content'])}</span>"
                + self._form(f"/queue/{q['id']}/delete", "<button class='x'>remove</button>", back=request.path_qs)
                + "</li>"
                for q in queue
            )
            queue_html = (
                "<h2>Queued</h2><div class='card pad'><div class='muted' style='font-size:13px'>"
                "Marcus posts these, oldest first, each time someone sends a message here "
                "(or someone uses /trigger).</div>"
                f"<ol class='queue'>{lis}</ol></div>"
            )

        # Add to memory
        memory = ""
        if guild is not None:
            memory = "<h2>Add to memory</h2>" + self._form(
                f"/c/{channel_id}/memory",
                "<input type='text' name='content' required maxlength='2000' "
                "placeholder='A phrase, or a Tenor/Giphy/.gif link'>"
                "<button>Remember</button>",
                cls="row card pad",
            )

        body = (
            f"<div class='crumbs'>{crumbs}</div><h1>{esc(name)}</h1>"
            f"<div class='crumbs'>{channel_id}</div>"
            f"<h2>Send a message</h2>{send}{queue_html}{memory}"
            "<h2>Saved messages</h2>"
            + self._search_box(f"/c/{channel_id}", query, f"Search {name}…")
            + "<div style='height:10px'></div>"
            + await self._timeline_html(request, channel_id, query, show_channel=False)
        )
        return page(name, body, request)

    async def filter_view(self, request: web.Request):
        entries = await self.bot.db.list_filter()
        rows = "".join(
            f"<tr><td>{esc(f['pattern'])}</td><td class='muted'>{esc((f['created_at'] or '')[:10])}</td>"
            f"<td class='num'>" + self._form(f"/filter/{f['id']}/delete", "<button class='x'>remove</button>")
            + "</td></tr>"
            for f in entries
        )
        table = (
            "<div class='card'><table><tr><th>Word / phrase / link</th><th>Added</th><th></th></tr>"
            + (rows or "<tr><td colspan='3' class='muted'>The filter is empty.</td></tr>")
            + "</table></div>"
        )
        form = self._form(
            "/filter/add",
            "<div class='row'><input type='text' name='pattern' required "
            f"maxlength='{MAX_FILTER_LEN}' placeholder='Word, phrase, or GIF link'>"
            "<button>Add to filter</button></div>"
            "<label class='check'><input type='checkbox' name='purge' value='1' checked>"
            "Also forget everything already saved that matches</label>",
            cls="stack card pad",
        )
        body = (
            "<h1>Memory filter</h1><div class='crumbs'>Applies to every server. A saved message or GIF "
            "containing any of these (whole words, any capitalization) is never remembered, and is "
            "never used in a response.</div>"
            f"{form}<h2>Filtered ({len(entries)})</h2>{table}"
        )
        return page("Memory filter", body, request)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    async def send_message(self, request: web.Request):
        channel_id = self._int_param(request, "channel_id")
        form = await request.post()
        back = f"/c/{channel_id}"
        content = str(form.get("content", "")).strip()
        action = form.get("action", "send")
        channel = self.bot.get_channel(channel_id)

        if not content:
            raise redirect(back, error="Message was empty.")
        if len(content) > MAX_MESSAGE_LEN:
            raise redirect(back, error=f"Discord messages max out at {MAX_MESSAGE_LEN} characters.")
        if not self._sendable(channel):
            raise redirect(back, error="Marcus can't send messages in that channel.")

        if action == "queue":
            await self.bot.db.queue_message(channel_id, channel.guild.id, content)
            raise redirect(back, ok="Queued. Marcus will post it the next time someone talks here.")

        try:
            await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            )
            log.info("Dashboard sent message to #%s (%s)", channel.name, channel_id)
        except discord.HTTPException as e:
            raise redirect(back, error=f"Discord rejected it: {e.text or e}")
        raise redirect(back, ok="Message sent.")

    async def add_memory(self, request: web.Request):
        channel_id = self._int_param(request, "channel_id")
        form = await request.post()
        back = f"/c/{channel_id}"
        raw = str(form.get("content", "")).strip()
        channel = self.bot.get_channel(channel_id)
        guild = getattr(channel, "guild", None)
        if guild is None:
            raise redirect(back, error="Marcus can't see that channel.")
        if self.bot.db.is_filtered(raw):
            raise redirect(back, error="That matches the memory filter, so it wasn't saved.")

        fields = dict(message_id=f"dash-{uuid.uuid4().hex}", channel_id=channel_id,
                      guild_id=guild.id, author_id="dashboard")
        if looks_like_gif_link(raw):
            if not await self.bot.db.log_gif(url=raw, **fields):
                raise redirect(back, error="That GIF is already saved in this channel.")
            raise redirect(back, ok="GIF added to this channel's memory.")

        cleaned = sanitize_content(raw)
        if len(cleaned) < 2:
            raise redirect(back, error="Nothing usable left after cleanup (links, mentions and custom emoji are stripped).")
        await self.bot.db.log_message(content=cleaned, **fields)
        raise redirect(back, ok="Added to this channel's memory.")

    async def delete_queued(self, request: web.Request):
        form = await request.post()
        await self.bot.db.remove_queued(self._int_param(request, "queue_id"))
        raise redirect(str(form.get("back", "/")), ok="Removed from the queue.")

    async def delete_item(self, request: web.Request):
        form = await request.post()
        back = str(form.get("back", "/"))
        try:
            row_id = int(form.get("id", ""))
        except ValueError:
            raise redirect(back, error="Bad item id.")
        if form.get("kind") == "gif":
            await self.bot.db.delete_gif_row(row_id)
            raise redirect(back, ok="GIF forgotten.")
        await self.bot.db.delete_message_row(row_id)
        raise redirect(back, ok="Message forgotten.")

    async def add_filter(self, request: web.Request):
        form = await request.post()
        pattern = " ".join(str(form.get("pattern", "")).split())
        if not pattern:
            raise redirect("/filter", error="Enter a word, phrase, or link.")
        if len(pattern) > MAX_FILTER_LEN:
            raise redirect("/filter", error=f"Keep filter entries under {MAX_FILTER_LEN} characters.")
        added = await self.bot.db.add_filter(pattern)
        msg = f"Added “{pattern}” to the filter." if added else f"“{pattern}” was already in the filter."
        if form.get("purge"):
            removed = await self.bot.db.purge_filtered()
            msg += f" Forgot {removed} saved item{'s' if removed != 1 else ''} that matched."
        raise redirect("/filter", ok=msg)

    async def delete_filter(self, request: web.Request):
        await self.bot.db.remove_filter(self._int_param(request, "filter_id"))
        raise redirect("/filter", ok="Removed from the filter.")


def create_dashboard(bot) -> Dashboard | None:
    password = os.getenv("DASHBOARD_PASSWORD")
    if not password:
        log.info("DASHBOARD_PASSWORD not set; dashboard disabled.")
        return None
    port = int(os.getenv("DASHBOARD_PORT") or os.getenv("PORT") or 8080)
    return Dashboard(bot, password, port=port)
