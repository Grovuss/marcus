# Marcus

Marcus is a server-specific Discord bot that quietly logs messages from
channels you choose, builds a local text/GIF corpus out of them, and
occasionally blurts out a generated response by recombining things your
server has actually said. No external LLM or cloud AI API is used
anywhere — generation is pure local Markov chains + fragment
recombination over SQLite-stored data.

> Marcus is supposed to be stupid on purpose. The goal is "this server's
> collective brain somehow became sentient," not a polished chatbot.

---

## 1. Create the Discord application/bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application**, give it a name (e.g. "Marcus").
3. Go to the **Bot** tab → **Add Bot**.
4. Under **Privileged Gateway Intents**, enable:
   - **Message Content Intent** (required — see below)
5. Copy the bot **Token** (Bot tab → "Reset Token" if you haven't got one yet).
   You'll put this in `.env`. Never share or commit this token.
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Send Messages`, `Read Message History`,
     `View Channels`, `Embed Links`, `Use Slash Commands`
   - Open the generated URL and invite the bot to your test server.

### Why Message Content Intent is required

Marcus reads the actual text of messages in configured channels to build
its corpus and to run generation. Without the **Message Content Intent**
enabled (both in the Developer Portal *and* in the bot's `intents` in
code — already handled in `bot.py`), Discord will only give the bot
empty `message.content` strings, and Marcus can't do anything useful.

---

## 2. Install dependencies

Requires Python 3.11+.

```bash
cd marcus
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## 3. Create your `.env`

```bash
cp .env.example .env
```

Edit `.env` and set:

```
DISCORD_TOKEN=your-bot-token-here
```

`DEV_GUILD_ID` is optional — set it to your test server's ID while
developing so slash commands sync instantly to that one server instead
of waiting up to an hour for a global sync.

---

## 4. Run Marcus

```bash
python bot.py
```

On first run, Marcus creates `marcus.db` (SQLite) in the project
directory automatically — this is where all corpus data and
configuration live. `config.yaml` only provides the *initial* defaults
seeded for a server the first time Marcus sees it; after that, settings
changed via slash commands persist in SQLite and survive restarts.

---

## 5. Configuring channels

Marcus logs and responds **only** in channels you explicitly configure.
Nothing is logged by default.

```
/channel add #general        # enable logging AND responses in #general
/channel add #shitposting
/channel list                 # see logging/response status for every configured channel
/channel disable #general     # stop Marcus from responding in #general (keeps logging)
/channel enable #general      # turn responses back on
/channel remove #general      # disable both logging and responses (data is kept)
```

All `/channel` commands require the **Manage Server** permission.

---

## 6. Slash commands

### `/message` — testing & manual corpus control
| Command | Description |
|---|---|
| `/message send text:<text>` | Manually inject a line of text into the current channel's corpus, as if it had been logged normally. Useful for seeding a corpus before real chat activity exists. |
| `/message generate [channel]` | Generate a test response on demand (ephemeral, only you see it) without waiting for real chat traffic. |

### `/responsechance` — response probability (Manage Server required)
```
/responsechance                      # show the server-wide default
/responsechance #general             # show #general's effective chance
/responsechance #general 5           # set #general to 5%
```
Values are validated to the 0–100 range. If a channel has no
channel-specific override, it falls back to the server-wide default.

### `/channel` — enable/disable logging & responses (Manage Server required)
```
/channel add #general
/channel remove #general
/channel enable #general
/channel disable #general
/channel list
```
Logging and responding are separate flags, so you can have Marcus
quietly learn in a channel without ever posting there, or vice versa.

### `/corpus` — corpus stats & management
```
/corpus stats                        # server-wide stats (Manage Server)
/corpus stats #general                # channel-specific stats
/corpus clear channel:#general        # wipe one channel's data (Administrator)
/corpus clear user:@someone           # wipe everything logged from one user (Administrator)
/corpus clear all:True                # wipe the ENTIRE server corpus (Administrator)
```
`stats` shows messages logged, GIFs logged, channel count, unique
users, and oldest/newest logged message. `clear` requires
**Administrator** and exactly one of `channel` / `user` / `all` to
avoid accidental total wipes.

### `/marcus` — status/info (Manage Server required)
Shows total messages/GIFs learned, channel count, response chance,
cooldown, and generation settings at a glance.

---

## 7. How the corpus works

1. When a message arrives in a channel with **logging** enabled, Marcus:
   - Skips it entirely if it's from a bot (including itself), empty,
     command-like (starts with `!`, `/`, `.`, etc.), or a system message.
   - Strips user/role/channel mentions, custom emoji, and URLs from the
     text (so Marcus never learns to ping people or regurgitate links).
   - Stores the cleaned text, plus the Discord message ID, channel ID,
     author ID, and timestamp, in SQLite.
2. Author IDs are kept **only** so admins can delete a specific user's
   data later (`/corpus clear user:`). They are never passed into the
   generation system — generation only ever sees the text itself.
3. When Marcus decides to respond, it pulls the relevant corpus
   (preferring the current channel, falling back to the whole server if
   there isn't enough local data yet) and runs one of two generation
   methods:
   - **Markov chains**: builds a word-level n-gram model (order
     configurable, default 2) from the corpus and walks it randomly.
   - **Fragment recombination**: splits stored messages into "head" and
     "tail" fragments at natural-ish split points and stitches a head
     from one message to the tail of a completely different one — this
     tends to preserve more recognizable phrasing than pure Markov.
   - By default (`generation.mode: mixed`) Marcus randomly picks
     between the two methods each time it responds.
4. Generated text is checked against a short rolling history of Marcus's
   own recent responses per server, to avoid repeating itself back to
   back.

Nothing in the corpus is ever sent to an external API. It's 100% local
SQLite + local generation logic.

---

## 8. How GIF logging works

Marcus recognizes GIFs in three forms:
- Direct `.gif` file attachments
- Tenor/Giphy links (raw or unfurled Discord embeds), including gifv
  previews
- Any URL ending in `.gif` pasted directly into a message

If a message contains both text and a GIF (e.g. `"bro what are you
doing"` + a GIF), **both** are logged — the text goes into the text
corpus, and the GIF URL goes into a separate GIF table. Duplicate GIF
URLs within the same channel are not stored twice.

When Marcus decides to respond with a GIF, it's picked independently of
text generation — Marcus never tries to weave a GIF URL into generated
text. Responses can be text-only, GIF-only, or (occasionally) both.
GIF selection prefers GIFs logged in the current channel first, falling
back to the server-wide GIF pool if the channel doesn't have enough.

Configure this via the `gif` section of `config.yaml` for defaults, or
per-channel overrides (advanced — edit the `channel_settings` table
directly, or extend `/responsechance`-style commands if you want a
dedicated slash command for it).

---

## 9. Response behavior & anti-spam

- Each eligible message has a configurable **percent chance** of
  triggering a response (`/responsechance`), checked per message.
- A **cooldown** (default 30s, `guild_settings.cooldown_seconds`) is
  enforced per channel regardless of the probability roll, so a busy
  channel can't get spammed even at a high response chance.
- Generated responses are capped in length (`response.max_words`, plus
  a hard character cap) and have a configurable minimum word count so
  Marcus doesn't reply with fragments like "the server".
- Marcus never responds to itself or to other bots.
- Marcus avoids repeating its own recent responses verbatim.

---

## 10. Clearing / deleting corpus data

```
/corpus clear channel:#general   # everything logged in one channel
/corpus clear user:@someone      # everything logged by one user, server-wide
/corpus clear all:True           # the entire server corpus (messages + GIFs)
```

All three require **Administrator**. `/channel remove` disables logging
and responding in a channel but does **not** delete previously logged
data — use `/corpus clear` for actual deletion.

---

## Project structure

```
marcus/
├── bot.py                   # entry point: intents, event loop, on_message pipeline
├── config.py                 # loads config.yaml, provides seed defaults
├── config.yaml                # static default config (seeds new guilds only)
├── database.py                # all SQLite access (aiosqlite)
├── generator/
│   ├── markov.py               # word-level Markov chain generator
│   ├── fragments.py            # fragment split/recombination generator
│   └── generator.py            # picks between methods, cleans/truncates output
├── commands/
│   ├── message.py               # /message send, /message generate
│   ├── responsechance.py        # /responsechance
│   ├── channel.py                # /channel add/remove/enable/disable/list
│   ├── corpus.py                  # /corpus stats/clear
│   └── marcus.py                   # /marcus info
├── services/
│   ├── message_logger.py           # eligibility checks + text sanitization
│   ├── gif_logger.py                # GIF URL extraction
│   └── responder.py                  # probability/cooldown/generation orchestration
├── requirements.txt
├── .env.example
└── README.md
```

## Notes on permissions

All configuration slash commands (`/message`, `/responsechance`,
`/channel`, `/corpus`, `/marcus`) require at least **Manage Server**,
and destructive `/corpus clear` operations additionally require
**Administrator**. Regular server members can't change Marcus's
configuration, only admins can.
