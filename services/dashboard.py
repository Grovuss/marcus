"""
Password-protected admin dashboard, served from the same process as the
bot so it can see live guild/channel state and send messages directly.

  /                  - every server Marcus is in
  /g/{guild_id}      - that server's text channels + how much is logged
  /c/{channel_id}    - logged messages/GIFs for a channel, plus a send box

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
from urllib.parse import quote

import discord
from aiohttp import web

log = logging.getLogger("marcus.dashboard")

PAGE_SIZE = 100
MAX_MESSAGE_LEN = 2000

CSS = """
:root { --bg:#f6f6f8; --card:#fff; --fg:#1c1c22; --muted:#6b6b76; --line:#e2e2e8;
        --accent:#5865f2; --ok:#1f8a4c; --err:#c0392b; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16171b; --card:#1f2026; --fg:#e8e8ec; --muted:#9a9aa6; --line:#2e2f37;
          --accent:#7b86ff; --ok:#3fbf7a; --err:#ff6b5e; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:960px; margin:0 auto; padding:24px 16px 64px; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
h1 { font-size:22px; margin:0 0 4px; } h2 { font-size:16px; margin:28px 0 10px; }
.crumbs { color:var(--muted); font-size:13px; margin-bottom:16px; }
.muted { color:var(--muted); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; }
table { width:100%; border-collapse:collapse; }
th, td { text-align:left; padding:10px 14px; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); font-weight:600; }
tr:last-child td { border-bottom:none; }
td.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
td.msg { word-break:break-word; white-space:pre-wrap; }
.tag { display:inline-block; font-size:12px; padding:1px 8px; border-radius:99px;
       border:1px solid var(--line); color:var(--muted); margin-right:4px; }
.tag.on { color:var(--ok); border-color:var(--ok); }
form.send { padding:14px; display:flex; flex-direction:column; gap:10px; }
textarea { width:100%; min-height:90px; padding:10px; font:inherit; color:var(--fg);
           background:var(--bg); border:1px solid var(--line); border-radius:8px; resize:vertical; }
button { align-self:flex-end; background:var(--accent); color:#fff; border:none; border-radius:8px;
         padding:8px 18px; font:inherit; font-weight:600; cursor:pointer; }
.flash { padding:10px 14px; border-radius:8px; margin-bottom:16px; border:1px solid; }
.flash.ok { color:var(--ok); border-color:var(--ok); }
.flash.err { color:var(--err); border-color:var(--err); }
.pager { display:flex; justify-content:space-between; margin-top:12px; }
.gifs { display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:10px; padding:14px; }
.gifs a { display:block; font-size:12px; word-break:break-all; }
.gifs img { width:100%; border-radius:6px; display:block; margin-bottom:4px; }
"""


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def page(title: str, body: str) -> web.Response:
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)} · Marcus</title><style>{CSS}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )
    return web.Response(text=doc, content_type="text/html")


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
        app.router.add_get("/g/{guild_id}", self.guild_view)
        app.router.add_get("/c/{channel_id}", self.channel_view)
        app.router.add_post("/c/{channel_id}/send", self.send_message)
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
        body = f"<h1>Servers</h1><div class='crumbs'>Logged in as {me} · {len(self.bot.guilds)} servers</div>{table}"
        return page("Servers", body)

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
        return page(guild.name, body)

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

        try:
            page_no = max(1, int(request.query.get("page", "1")))
        except ValueError:
            page_no = 1
        offset = (page_no - 1) * PAGE_SIZE

        messages = await self.bot.db.get_channel_messages(channel_id, limit=PAGE_SIZE + 1, offset=offset)
        has_more = len(messages) > PAGE_SIZE
        messages = messages[:PAGE_SIZE]
        gifs = await self.bot.db.get_gifs(channel_id=channel_id, limit=60)

        name = f"#{channel.name}" if channel else f"unknown channel {channel_id}"
        crumbs = "<a href='/'>Servers</a>"
        if guild:
            crumbs += f" / <a href='/g/{guild.id}'>{esc(guild.name)}</a>"
        crumbs += f" / {esc(name)}"

        flash = ""
        if request.query.get("sent"):
            flash = "<div class='flash ok'>Message sent.</div>"
        elif request.query.get("error"):
            flash = f"<div class='flash err'>{esc(request.query['error'])}</div>"

        # Send box
        if isinstance(channel, discord.abc.Messageable) and guild:
            if channel.permissions_for(guild.me).send_messages:
                send = (
                    f"<form class='send card' method='post' action='/c/{channel_id}/send'>"
                    f"<input type='hidden' name='csrf' value='{self.csrf_token}'>"
                    f"<textarea name='content' maxlength='{MAX_MESSAGE_LEN}' required "
                    f"placeholder='Say something as Marcus in {esc(name)}…'></textarea>"
                    "<button type='submit'>Send as Marcus</button></form>"
                )
            else:
                send = "<div class='card muted' style='padding:14px'>Marcus doesn't have permission to send messages here.</div>"
        else:
            send = "<div class='card muted' style='padding:14px'>Marcus can't see this channel anymore, so it can't send here.</div>"

        # Logged messages
        rows = []
        for m in messages:
            author = await self._author_name(guild, m["author_id"])
            ts = (m["timestamp"] or "")[:16].replace("T", " ")
            manual = " <span class='tag'>manual</span>" if str(m["message_id"]).startswith("manual-") else ""
            rows.append(
                f"<tr><td class='num muted'>{esc(ts)}</td>"
                f"<td>{esc(author)}{manual}</td><td class='msg'>{esc(m['content'])}</td></tr>"
            )
        msg_table = (
            "<div class='card'><table><tr><th>Logged (UTC)</th><th>Author</th><th>Message</th></tr>"
            + ("".join(rows) or "<tr><td colspan='3' class='muted'>Nothing logged here.</td></tr>")
            + "</table></div>"
        )
        pager = "<div class='pager'>"
        pager += f"<a href='?page={page_no - 1}'>← Newer</a>" if page_no > 1 else "<span></span>"
        pager += f"<a href='?page={page_no + 1}'>Older →</a>" if has_more else "<span></span>"
        pager += "</div>"

        gif_html = ""
        if gifs:
            items = "".join(
                f"<a href='{esc(u)}' target='_blank' rel='noopener noreferrer'>"
                f"<img src='{esc(u)}' loading='lazy' alt=''>{esc(u)}</a>"
                for u in gifs
            )
            gif_html = f"<h2>Saved GIFs (latest {len(gifs)})</h2><div class='card gifs'>{items}</div>"

        body = (
            f"<div class='crumbs'>{crumbs}</div><h1>{esc(name)}</h1>"
            f"<div class='crumbs'>{channel_id}</div>{flash}"
            f"<h2>Send a message</h2>{send}"
            f"<h2>Saved messages</h2>{msg_table}{pager}{gif_html}"
        )
        return page(name, body)

    async def send_message(self, request: web.Request):
        channel_id = self._int_param(request, "channel_id")
        form = await request.post()
        back = f"/c/{channel_id}"

        if not hmac.compare_digest(str(form.get("csrf", "")), self.csrf_token):
            raise web.HTTPForbidden(text="Bad form token - reload the page and try again.")

        content = str(form.get("content", "")).strip()
        channel = self.bot.get_channel(channel_id)
        error = None
        if not content:
            error = "Message was empty."
        elif len(content) > MAX_MESSAGE_LEN:
            error = f"Discord messages max out at {MAX_MESSAGE_LEN} characters."
        elif not isinstance(channel, discord.abc.Messageable) or not getattr(channel, "guild", None):
            error = "Marcus can't see that channel."
        else:
            try:
                await channel.send(
                    content,
                    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
                )
                log.info("Dashboard sent message to #%s (%s)", channel.name, channel_id)
            except discord.HTTPException as e:
                error = f"Discord rejected it: {e.text or e}"

        if error:
            raise web.HTTPSeeOther(f"{back}?error={quote(error)}")
        raise web.HTTPSeeOther(f"{back}?sent=1")


def create_dashboard(bot) -> Dashboard | None:
    password = os.getenv("DASHBOARD_PASSWORD")
    if not password:
        log.info("DASHBOARD_PASSWORD not set; dashboard disabled.")
        return None
    port = int(os.getenv("DASHBOARD_PORT") or os.getenv("PORT") or 8080)
    return Dashboard(bot, password, port=port)
