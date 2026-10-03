# telegram-mcp-server

An [MCP](https://modelcontextprotocol.io) server that exposes [Telethon](https://docs.telethon.dev/) Telegram operations as tools, so an agent (Claude Code, Hermes, Codex, …) can read and act on a Telegram account over stdio.

Exposes 23 tools: dialog/channel listing, message read & search, member listing, media download, inline-keyboard clicking, Web App URL retrieval, channel discovery, and bulk join/leave/block.

---

## ⚠️ Security — read this first

**A Telethon `.session` file is a complete login for your Telegram account.**
It is an SQLite database whose `sessions` table holds the auth key. Anyone who obtains it can log in as you — no password, no 2FA prompt.

Therefore:

| Never commit | Why |
|---|---|
| `*.session` | Contains the auth key. This is the account. |
| `*.session-journal` | Write-ahead log for the above. |
| `.env` | Contains `TG_API_ID` / `TG_API_HASH`. |
| `scan_results.json`, `downloads/` | Output can contain private messages, contacts, and personal data. |

`.gitignore` in this repo covers all of these. **Before your first commit, run `git status --ignored` and confirm nothing sensitive is staged.**

If you ever push a secret by accident, deleting the file in a later commit does **not** remove it from history — treat it as compromised and rotate the credential immediately.

### Rotating a leaked `api_hash`

An `api_id`/`api_hash` pair identifies an app you registered at my.telegram.org. If the hash leaks, someone else can use your app's API quota and identity. Go to **my.telegram.org → API Development Tools → App configuration → terminate app**, then create a new one and update `.env`.

---

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create your `.env`

```bash
cp .env.example .env
```

Get `TG_API_ID` and `TG_API_HASH` from <https://my.telegram.org> → *API Development Tools*.

Set `TG_SESSION_NAME` to the path where the session should live (**no `.session` suffix**). Telethon appends the suffix itself.

### 3. First run — interactive login

The MCP server itself won't prompt. Do the one-time auth separately:

```python
# login.py  — run once, then delete it
import asyncio
from telethon import TelegramClient
from dotenv import load_dotenv
import os

load_dotenv()

async def main():
    client = TelegramClient(
        os.environ["TG_SESSION_NAME"],
        int(os.environ["TG_API_ID"]),
        os.environ["TG_API_HASH"],
    )
    await client.start()          # prompts for phone + code (and 2FA password)
    me = await client.get_me()
    print(f"logged in as {me.first_name} (@{me.username})")
    await client.disconnect()

asyncio.run(main())
```

```bash
pip install python-dotenv
python login.py
```

After this, `TG_SESSION_NAME.session` exists and the MCP server can use it non-interactively.

### 4. Run the server

```bash
python telegram_mcp_server.py
```

It speaks MCP over stdio, so it normally isn't run by hand — it's launched by the MCP client. Quick smoke test:

```bash
python -c "
import asyncio, os
from dotenv import load_dotenv
load_dotenv()
os.environ.setdefault('TG_API_ID','1')
" # see Troubleshooting for the offline import check
```

---

## Register with an MCP client

### Claude Code / Claude Desktop

```json
{
  "mcpServers": {
    "telegram": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/telegram_mcp_server.py"],
      "env": {
        "TG_API_ID": "<your-api-id>",
        "TG_API_HASH": "<your-32-char-api-hash>",
        "TG_SESSION_NAME": "/absolute/path/to/session/my_account"
      }
    }
  }
}
```

If you use `python-dotenv`, the server also picks up a `.env` sitting next to it.

### Hermes

```yaml
mcp_servers:
  telegram:
    command: /absolute/path/to/.venv/bin/python
    args:
      - /absolute/path/to/telegram_mcp_server.py
    timeout: 120
```

---

## Tools

### Reading

| Tool | Purpose |
|---|---|
| `list_chats` | List dialogs, filtered by `channels` / `groups` / `bots` / `users` / `all` |
| `chat_info` | Detail for one chat or user by `@username` or numeric ID |
| `read_messages` | Recent messages from a chat (`limit`, `offset`) |
| `get_mentions` | Read-only: messages in a chat that explicitly @mention you. Never sends, reacts, or marks read. |
| `search_messages` | Full-text message search inside one chat |
| `get_members` | List group participants |
| `profile` | The authenticated account's own profile |
| `extract_refs` | Pull `@mentions`, `t.me/` links, invite links, and forward-source channel IDs out of a chat |

### Discovery

| Tool | Purpose |
|---|---|
| `search_public` | Telegram's global directory search by keyword |
| `get_recommendations` | Telegram's "similar channels" graph for a seed channel |
| `scan_channels_content` | Bounded batch scan of many channels; writes JSON to disk, returns a compact summary. `offset` / `max_channels` / `timeout_seconds` keep it from hanging. |

### Writing

| Tool | Purpose |
|---|---|
| `send_message` | Send text |
| `send_location` | Send a geo-point |
| `forward_messages` | Forward messages between chats |
| `pin_message` / `add_reaction` | Pin/unpin, react |
| `download_media` | Download a photo/video/file |
| `join_chat` / `leave_chat` | Join or leave one channel/group |
| `leave_channels` / `block_chats` | Bulk leave / block by list of IDs |
| `click_button` | Press an inline-keyboard button; returns the bot's callback answer, edited message, and available buttons |
| `request_web_app` | Resolve a bot's Web App / Mini App URL by short name |

### Bot callback responses

`click_button` handles all three Telegram bot response patterns, because they behave differently:

1. **New message** → returned in `bot_response`
2. **In-place edit** → returned in `edited_message` / `edited_buttons`
3. **Popup alert** (`BotCallbackAnswer`) → returned in `callback_answer`. This text never appears as a message; if both `callback_answer` and `edited_message` are empty, the bot replied with a transient alert only.

If a click fails, `available_buttons` lists the exact labels — retry with one of those in `button_text`.

---

## Ethical boundaries

This server can message any account the authenticated user can reach. It does not, and should not, be used to impersonate a human to people who believe they are talking to one. Read-only and analysis use — listing chats, summarizing content, finding bots, mapping channel networks — is fine.

---

## Troubleshooting

**`Missing required environment variable: TG_API_ID`** — the server exits immediately by design rather than silently falling back to a default. Create `.env` or pass `env` in the MCP client config.

**`sqlite3.DatabaseError: database disk image is malformed`** — the session file was created with a different `(api_id, api_hash)` pair than the one currently configured. Telegram binds sessions to credentials. Authenticate fresh with the new pair instead of reusing the old file.

**`Not authorized. Re-login required.`** — no valid session at `TG_SESSION_NAME`. Re-run the login step.

**`401 Unauthorized` / `EOFError` on a headless host** — there's no interactive stdin for Telethon to prompt on. Do the one-time login locally or via a pty, then copy the resulting `.session` file to the server. Verify with `file session.session` (should report SQLite 3.x).

**MCP tools don't appear after editing the server** — the client caches tool definitions at startup. Restart the MCP client (and the Hermes gateway, if configured there) for changes to take effect.

---

## License

Public domain / Unlicense, matching upstream yt-dlp-style contributions. Add your preferred license.