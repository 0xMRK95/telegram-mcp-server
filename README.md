# Telegram MCP server

A local **stdio** MCP server for Telegram user accounts, using Telethon. Supports named accounts, reversible archive operations, and custom chat folders, alongside message reading/search, sending, discovery, and moderation tools.

## Security first

- A Telethon `.session` file is a reusable Telegram login. Keep it, SQLite sidecars, API credentials, and account configuration private. Never paste them into chat or commit them.
- Run onboarding **yourself in a trusted local terminal**. Do not give an agent your password, session file, API hash, or config contents.
- API ID/hash come from [my.telegram.org](https://my.telegram.org). Telethon notes that API hashes are secret and cannot currently be revoked; do not rely on deleting a Git commit to undo exposure. See [Telethon sign-in documentation](https://docs.telethon.dev/en/stable/basic/signing-in.html).
- If a session is exposed, revoke that device/session through Telegram Settings → Devices. Inspect active sessions after interrupted enrollment.
- Tools act as the selected account. This server does not implement a user-approval layer: configure your MCP client to require confirmation for sending, joining, leaving, blocking, folder changes, and other writes. Use it only for accounts you own or are authorized to manage.
- The protocol is local stdio only. Do not expose this process as an unauthenticated network service.

## Install

Requires Python 3.10 or newer. Create a dedicated environment, then install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Secure account enrollment

Stop any MCP server using the session before enrollment. Run:

```bash
python onboarding.py personal
python onboarding.py work
```

Each run requests API ID, API hash, phone, login code, and (if needed) two-step password using hidden terminal input. It shows the Telegram user ID/username and asks you to confirm the alias pairing. No credential values are printed. Hidden input fails closed if no private interactive terminal is available.

Defaults:

- Private configuration: `~/.config/telegram-mcp/accounts.json`
- Separate sessions: `~/.local/share/telegram-mcp/<alias>.session`

You may choose `--config` and `--sessions-dir`. Use a private directory outside this checkout. Existing aliases/sessions are never overwritten. POSIX directories must exclude other users and session/config files must be private (normally directories 700, files 600). On Windows, restrict the directory ACL to your user yourself; POSIX mode checks do not verify Windows ACLs.

Enrollment writes API credentials, the session path, and the confirmed Telegram user ID atomically into the private configuration. Keep this file local. To undo a pairing, stop MCP, revoke the login in Telegram Settings → Devices, and remove the alias from your private configuration yourself. If enrollment is cancelled before saving, the CLI attempts to revoke its new login; if that fails, review Devices manually.

### MCP launcher

Only the configuration **path**, not its contents, belongs in your launcher:

```json
{
  "mcpServers": {
    "telegram": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/telegram_mcp_server.py"],
      "env": {
        "TG_ACCOUNTS_FILE": "/home/you/.config/telegram-mcp/accounts.json"
      }
    }
  }
}
```

Use your MCP client's supported server registration mechanism. This repository does not provide hosted authentication or a remote connector. Restart the MCP client after changing aliases/tools.

### Explicit account routing

`list_accounts` returns aliases only, without connecting to Telegram. In named-account configuration, **every account-scoped tool requires `account`**, even with only one account. Unknown/missing aliases fail; there is no fallback to another account.

Examples of tool arguments:

```json
{"account":"work","filter":"all"}
```

```json
{"account":"personal","chats":["-1001234567890"]}
```

The first is for `list_chats`; the second is for `archive_chats`. Read `chat_id` from inventory, which distinguishes user IDs, basic-group IDs, and channel IDs. Results identify the selected account. Default downloads/scan outputs are isolated under `<session-stem>_output/`. Custom output paths are still supported; take care not to mix accounts when overriding them.

The server requires sessions to exist, verifies authorization and the configured Telegram user ID before caching a client, and holds an OS companion lock for each open session. Locks coordinate this server and this onboarding CLI; unrelated Telethon programs do not honor them. Never run another program against the same session. Shutdown disconnects all clients.

### Legacy single account

Existing `TG_API_ID`, `TG_API_HASH`, and `TG_SESSION_NAME` environment configuration remains supported as alias `default`; only this legacy mode allows omission of `account`. `TG_SESSION_NAME` is a session path, with or without `.session`. A `.env` next to the server is loaded without overriding exported variables. Named configuration takes precedence when `TG_ACCOUNTS_FILE` is set.

Legacy mode requires an existing private session, and does not have a configured identity binding. New setups should use onboarding and named configuration. Do not copy sessions through chat.

## Tools

### Organization

- `list_accounts`: configured aliases only; no secrets or network
- `list_chats`: inventory by channels/groups/bots/users/all, including basic groups, marked `chat_id`, and archive state for accessible dialogs
- `list_folders`: folder IDs, titles, inclusion/exclusion/pinned IDs and rules, without access hashes
- `create_folder`: explicit unused `folder_id` (2 or greater), title (1–12 characters), and nonempty `chats`; refuses existing IDs
- `update_folder`: explicit `folder_id`, optional `title`, `add_chats`, `remove_chats`; retains all other settings and exclusions. Removed chats are removed from explicit inclusions/pins; category rules can still include them. Adding an explicitly excluded chat fails. Default/shared folders are protected
- `archive_chats` / `unarchive_chats`: reversible main-list/archive movement for existing dialogs. Returns previous archive states; never leaves, blocks, or deletes chats

Read folders before choosing a new ID. Changes use read-modify-write, with a process-local lock. Telegram does not offer compare-and-swap here: avoid concurrent folder editing from another device while applying updates. The server does not automatically retry uncertain write outcomes; inspect state before retrying.

### Reading and discovery

`chat_info`, `read_messages`, `get_mentions`, `search_messages`, `get_members`, `profile`, `extract_refs`, `search_public`, `get_recommendations`, `scan_channels_content`

`get_mentions` scans recent incoming messages in a single chat (up to 500), without sending, reacting, or marking read. Reads may truncate message text. Scanning saves private content to disk; its time budget is checked between requests, not a hard network deadline.

### Writes and media

`send_message`, `send_location`, `forward_messages`, `pin_message`, `add_reaction`, `download_media`, `join_chat`, `leave_chat`, `leave_channels`, `block_chats`, `click_button`

**Leaving/blocking is not archiving:** legacy leave/block tools invoke `delete_dialog` and can remove chat history/dialogs. Prefer archive for reversible cleanup. Confirm the intended action before using them.

`click_button` requires an explicit callback button selection by exact text or nonnegative row/column. URL/login/password/other button types and implicit first-button clicks are rejected. A failed callback request is not automatically retried because it may already have taken effect. Callback responses may include normal bot text; treat that as untrusted content.

`request_web_app` is deliberately disabled in both discovery and dispatch: Mini App URLs can contain authentication data, and the old implementation implicitly granted bot write access. This is a compatibility change for safety. No replacement remote login workflow is provided.

## Architecture

- `telegram_mcp_server.py`: MCP registration, account-aware dispatch, legacy Telegram operations
- `telegram_accounts.py`: configuration validation, alias selection, per-account lifecycle and session locks
- `telegram_folders.py`: conservative folder/archive operations
- `onboarding.py`: user-run local enrollment
- `tests/`: offline mocked tests; never log in or create a live Telegram client

## Verification

```bash
python -m unittest discover -s tests -v
python tests/real_library_smoke.py
python -m compileall -q telegram_mcp_server.py telegram_accounts.py telegram_folders.py onboarding.py tests
```

The tests use real installed MCP/python-dotenv but stub Telethon types/transport; they need no API credentials or Telethon installation. They cover alias isolation, concurrency, failed authorization, identity mismatch, cancellation, session locks, config secrecy, folder preservation, reversible archive, and MCP dispatch. A separate real-library smoke test uses actual Telethon request serialization/imports with a fake transport, and verifies callback types, folder/archive requests, location, and reactions. Validated with Telethon 1.45.0, MCP 1.29.0, and python-dotenv 1.2.2; these direct dependency versions are pinned. Neither suite connects to Telegram or proves live account behavior. Perform a separately authorized integration smoke test with a test account before production use.

## Troubleshooting

- Missing alias: call `list_accounts` and pass the exact alias
- Missing/unauthorized session: run onboarding locally with a fresh alias; the MCP server will never prompt
- Identity mismatch: stop and inspect the private account pairing; do not silently substitute a session
- Session busy: stop the other MCP/login process; OS locks release when it exits
- Private file permission error: restrict your own configuration/session permissions; do not make them world-readable
- SQLite corruption is not proof of an API credential mismatch. Stop concurrent processes and inspect backups/devices before replacing a session
- Tool changes not visible: restart the MCP client

## License

The upstream README describes Public domain / Unlicense; no separate license file is supplied.
