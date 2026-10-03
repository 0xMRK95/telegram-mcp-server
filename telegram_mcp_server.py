#!/usr/bin/env python3
"""Telegram MCP Server - exposes Telethon operations as MCP tools for Hermes Agent."""

import asyncio
import json
import os
import re
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from telethon import TelegramClient
from telethon.tl.types import ChannelForbidden, ChatForbidden, Channel, Chat, User
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest, RequestAppWebViewRequest
from telethon.tl.types import KeyboardButtonCallback, KeyboardButtonUrl, KeyboardButtonSwitchInline, InputMediaGeoPoint, GeoPoint, InputBotAppShortName, InputUser

def _required_env(name: str) -> str:
    """Read a required environment variable or exit with an actionable message."""
    value = os.environ.get(name)
    if not value:
        sys.exit(
            f"[telegram-mcp] Missing required environment variable: {name}\n"
            f"[telegram-mcp] Copy .env.example to .env, fill it in, and export the values."
        )
    return value


API_ID = int(_required_env("TG_API_ID"))
API_HASH = _required_env("TG_API_HASH")
# Session file NAME (no .session suffix). The auth key lives in the resulting
# .session SQLite database - that file must never be committed or shared.
SESSION_NAME = _required_env("TG_SESSION_NAME")

# Singleton client
_client: TelegramClient | None = None

async def get_client() -> TelegramClient:
    global _client
    if _client is None or not _client.is_connected():
        _client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
        await _client.connect()
        if not await _client.is_user_authorized():
            return None
    return _client

async def _resolve(client: TelegramClient, target: str):
    target = target.strip()
    if target.startswith('@'):
        target = target[1:]
    if target.lstrip('-').isdigit():
        return await client.get_entity(int(target))
    try:
        return await client.get_entity(target)
    except Exception:
        return await client.get_entity('@' + target)


def _message_mentions_account(message: Any, account_id: int, username: str | None) -> bool:
    """Return whether a received Telegram message explicitly mentions this account."""
    if bool(getattr(message, "mentioned", False)):
        return True

    for entity in getattr(message, "entities", None) or []:
        if getattr(entity, "user_id", None) == account_id:
            return True

    handle = (username or "").lstrip("@").strip()
    text = getattr(message, "raw_text", None) or getattr(message, "text", "") or ""
    if not handle or not text:
        return False

    return re.search(
        rf"(?<![A-Za-z0-9_])@{re.escape(handle)}(?![A-Za-z0-9_])",
        text,
        flags=re.IGNORECASE,
    ) is not None

def _entity_to_dict(entity) -> dict:
    result = {"id": entity.id, "type": type(entity).__name__}
    if isinstance(entity, Channel):
        result["title"] = entity.title
        result["username"] = getattr(entity, 'username', '') or ''
        result["members"] = getattr(entity, 'participants_count', 0) or 0
        result["is_channel"] = entity.broadcast
        result["is_group"] = entity.megagroup
        result["creator"] = getattr(entity, 'creator', False)
        result["admin"] = entity.admin_rights is not None
    elif isinstance(entity, User):
        result["first_name"] = getattr(entity, 'first_name', '') or ''
        result["last_name"] = getattr(entity, 'last_name', '') or ''
        result["username"] = getattr(entity, 'username', '') or ''
        result["bot"] = entity.bot
    return result

server = Server("telegram")

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_chats",
            description="List all Telegram chats (channels, groups, bots, users). Filter by type: channels, groups, bots, users, or all.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "enum": ["all", "channels", "groups", "bots", "users"], "description": "Type of chats to list"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="chat_info",
            description="Get detailed info about a Telegram chat or user by username or ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Username (with or without @) or numeric ID"},
                },
                "required": ["target"],
            },
        ),
        types.Tool(
            name="read_messages",
            description="Read recent messages from a Telegram chat.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat": {"type": "string", "description": "Chat username or ID"},
                    "limit": {"type": "integer", "description": "Number of messages to fetch (default 20)"},
                    "offset": {"type": "integer", "description": "Number of messages to skip (default 0)"},
                },
                "required": ["chat"],
            },
        ),
        types.Tool(
            name="get_mentions",
            description="Read-only: return recent incoming messages in one chat that explicitly mention the authenticated account. Never sends, reacts, or marks messages read.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat": {"type": "string", "description": "Chat username or ID"},
                    "limit": {"type": "integer", "description": "Number of recent messages to inspect (default 100, max 500)"},
                },
                "required": ["chat"],
            },
        ),
        types.Tool(
            name="send_message",
            description="Send a text message to a Telegram chat.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat": {"type": "string", "description": "Chat username or ID"},
                    "text": {"type": "string", "description": "Message text to send"},
                },
                "required": ["chat", "text"],
            },
        ),
        types.Tool(
            name="send_location",
            description="Send a GPS location to a Telegram chat. Useful for bots that require location input.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat": {"type": "string", "description": "Chat username or ID"},
                    "latitude": {"type": "number", "description": "Latitude (e.g. 35.6892 for Tehran)"},
                    "longitude": {"type": "number", "description": "Longitude (e.g. 51.3890 for Tehran)"},
                },
                "required": ["chat", "latitude", "longitude"],
            },
        ),
        types.Tool(
            name="search_messages",
            description="Search for messages in a Telegram chat by keyword.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat": {"type": "string", "description": "Chat username or ID"},
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (default 20)"},
                },
                "required": ["chat", "query"],
            },
        ),
        types.Tool(
            name="join_chat",
            description="Join a Telegram channel or group by username or invite link.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Username or invite link"},
                },
                "required": ["target"],
            },
        ),
        types.Tool(
            name="leave_chat",
            description="Leave a Telegram channel or group.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Chat username or ID"},
                },
                "required": ["target"],
            },
        ),
        types.Tool(
            name="get_members",
            description="List members of a Telegram group (limited to what's accessible).",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat": {"type": "string", "description": "Group username or ID"},
                    "limit": {"type": "integer", "description": "Max members to fetch (default 50)"},
                },
                "required": ["chat"],
            },
        ),
        types.Tool(
            name="download_media",
            description="Download media (photo, video, file) from a Telegram message.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat": {"type": "string", "description": "Chat username or ID"},
                    "message_id": {"type": "integer", "description": "ID of the message containing media"},
                    "output_dir": {"type": "string", "description": "Directory to save the file (default: <session dir>/downloads)"},
                },
                "required": ["chat", "message_id"],
            },
        ),
        types.Tool(
            name="pin_message",
            description="Pin or unpin a message in a Telegram chat.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat": {"type": "string", "description": "Chat username or ID"},
                    "message_id": {"type": "integer", "description": "Message ID to pin/unpin"},
                    "action": {"type": "string", "enum": ["pin", "unpin"], "description": "Pin or unpin (default: pin)"},
                },
                "required": ["chat", "message_id"],
            },
        ),
        types.Tool(
            name="add_reaction",
            description="Add an emoji reaction to a Telegram message.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat": {"type": "string", "description": "Chat username or ID"},
                    "message_id": {"type": "integer", "description": "Message ID"},
                    "emoji": {"type": "string", "description": "Emoji reaction (e.g. 👍, ❤️, 🔥)"},
                },
                "required": ["chat", "message_id", "emoji"],
            },
        ),
        types.Tool(
            name="forward_messages",
            description="Forward messages from one chat to another.",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_chat": {"type": "string", "description": "Source chat username or ID"},
                    "to_chat": {"type": "string", "description": "Destination chat username or ID"},
                    "message_ids": {"type": "string", "description": "Comma-separated message IDs"},
                },
                "required": ["from_chat", "to_chat", "message_ids"],
            },
        ),
        types.Tool(
            name="profile",
            description="Get your Telegram profile info.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        types.Tool(
            name="scan_channels_content",
            description="Scan channels: read last N messages from each channel. Saves full results to a JSON file and returns a compact summary. Use offset/limit_channels to scan in safe batches. Timeout-bounded to prevent MCP restart loops.",
            inputSchema={
                "type": "object",
                "properties": {
                    "messages_per_channel": {"type": "integer", "description": "Number of recent messages to read per channel (default 3)"},
                    "limit": {"type": "integer", "description": "Legacy: maps to messages_per_channel if messages_per_channel not set. Otherwise max channels to scan per call (default 25)"},
                    "filter": {"type": "string", "enum": ["channels", "groups", "all"], "description": "Type of chats to scan (default: channels)"},
                    "offset": {"type": "integer", "description": "Skip first N matching channels (for batched scanning). Default 0"},
                    "max_channels": {"type": "integer", "description": "Max channels to scan per call (default 25). Prevents timeout on large accounts."},
                    "timeout_seconds": {"type": "integer", "description": "Hard timeout in seconds. Stops scanning and saves partial results if exceeded. Default 55."},
                    "save_to": {"type": "string", "description": "File path to save full JSON results. Default: <session dir>/scan_results.json"},
                    "max_text_length": {"type": "integer", "description": "Max chars per message text (default 200). Truncates long messages for compact classification."},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="leave_channels",
            description="Leave multiple Telegram channels at once by providing a list of IDs or usernames.",
            inputSchema={
                "type": "object",
                "properties": {
                    "targets": {"type": "array", "items": {"type": "string"}, "description": "List of channel IDs (as strings) or usernames to leave"},
                },
                "required": ["targets"],
            },
        ),
        types.Tool(
            name="block_chats",
            description="Block multiple Telegram users/bots and remove them from the chat list. Blocking stops them from messaging you. Provide a list of IDs or usernames.",
            inputSchema={
                "type": "object",
                "properties": {
                    "targets": {"type": "array", "items": {"type": "string"}, "description": "List of user/bot IDs (as strings) or usernames to block"},
                },
                "required": ["targets"],
            },
        ),
        types.Tool(
            name="search_public",
            description="Search Telegram's global public directory by keyword (contacts.Search). Returns public channels/groups and users whose title or username matches. Useful for discovering new channels to join.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword (title or @username substring)"},
                    "limit": {"type": "integer", "description": "Max results (default 50, server caps lower)"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_recommendations",
            description="Get Telegram's 'similar channels' recommendation graph for a given channel (channels.GetChannelRecommendations). Finds RELATED channels regardless of their title/username — great for discovering channels that keyword search can't reach.",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "Seed channel username or ID"},
                },
                "required": ["channel"],
            },
        ),
        types.Tool(
            name="extract_refs",
            description="Read a chat's recent messages and extract every channel reference it contains: @usernames mentioned, t.me / joinchat invite links, and forwarded-from channels. Surfaces channels that advertise/forward each other even when their own names are codenames or junk.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat": {"type": "string", "description": "Chat username or ID to scan"},
                    "limit": {"type": "integer", "description": "How many recent messages to scan (default 100)"},
                },
                "required": ["chat"],
            },
        ),
        types.Tool(
            name="click_button",
            description="Click an inline keyboard button on a Telegram message. Provide chat, message_id, and button text (or row/col indices). Returns the result message from the bot.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chat": {"type": "string", "description": "Chat username or ID"},
                    "message_id": {"type": "integer", "description": "Message ID containing the inline button"},
                    "button_text": {"type": "string", "description": "Text of the button to click (fuzzy match). Alternative to row/col."},
                    "row": {"type": "integer", "description": "Row index (0-based) of the button"},
                    "col": {"type": "integer", "description": "Column index (0-based) of the button"},
                },
                "required": ["chat", "message_id"],
            },
        ),
        types.Tool(
            name="request_web_app",
            description="Request a bot's Web App/Minor App URL. Provide bot username. Returns the full web app URL if found.",
            inputSchema={
                "type": "object",
                "properties": {
                    "bot": {"type": "string", "description": "Bot username (with or without @)"},
                    "short_name": {"type": "string", "description": "App short name (default: app) — try: app, webapp, main, web"},
                    "platform": {"type": "string", "description": "Platform: android, ios, web (default: android)"},
                },
                "required": ["bot"],
            },
        ),
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    client = await get_client()
    if client is None:
        return [types.TextContent(type="text", text=json.dumps({"error": "Not authenticated. Re-login required."}))]

    try:
        if name == "list_chats":
            filter_type = arguments.get("filter", "all")
            channels, groups, bots, users = [], [], [], []
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if isinstance(entity, (ChannelForbidden, ChatForbidden)):
                    if filter_type in ("channels", "all"):
                        channels.append({"id": entity.id, "title": getattr(entity, 'title', 'Unknown'), "username": "", "members": 0, "type": "channel"})
                    continue
                if isinstance(entity, Channel):
                    info = {"id": entity.id, "title": entity.title, "username": getattr(entity, 'username', '') or '', "members": getattr(entity, 'participants_count', 0) or 0}
                    if entity.broadcast:
                        info["type"] = "channel"
                        channels.append(info)
                    else:
                        info["type"] = "group"
                        info["role"] = "creator" if getattr(entity, 'creator', False) else ("admin" if entity.admin_rights else "member")
                        groups.append(info)
                elif isinstance(entity, User):
                    if entity.bot:
                        bots.append({"id": entity.id, "name": getattr(entity, 'first_name', ''), "username": getattr(entity, 'username', '') or '', "type": "bot"})
                    else:
                        users.append({"id": entity.id, "name": getattr(entity, 'first_name', '') or '', "username": getattr(entity, 'username', '') or '', "type": "user"})
            result = {}
            if filter_type in ("channels", "all"): result["channels"] = channels
            if filter_type in ("groups", "all"): result["groups"] = groups
            if filter_type in ("bots", "all"): result["bots"] = bots
            if filter_type in ("users", "all"): result["users"] = users
            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        elif name == "chat_info":
            target = arguments["target"]
            entity = await _resolve(client, target)
            return [types.TextContent(type="text", text=json.dumps(_entity_to_dict(entity), ensure_ascii=False, indent=2))]

        elif name == "read_messages":
            target = arguments["chat"]
            limit = arguments.get("limit", 20)
            offset = arguments.get("offset", 0)
            entity = await _resolve(client, target)
            msgs = await client.get_messages(entity, limit=limit + offset)
            messages = []
            for msg in msgs[offset:]:
                msg_info = {"id": msg.id, "date": str(msg.date), "text": (msg.text or "")[:500]}
                if msg.sender:
                    msg_info["from"] = getattr(msg.sender, 'first_name', '') or getattr(msg.sender, 'title', '')
                if msg.media:
                    msg_info["has_media"] = True
                    msg_info["media_type"] = type(msg.media).__name__
                messages.append(msg_info)
            return [types.TextContent(type="text", text=json.dumps({"chat": target, "count": len(messages), "messages": messages}, ensure_ascii=False, indent=2))]

        elif name == "get_mentions":
            target = arguments["chat"]
            limit = max(1, min(int(arguments.get("limit", 100)), 500))
            entity = await _resolve(client, target)
            account = await client.get_me()
            msgs = await client.get_messages(entity, limit=limit)
            mentions = []
            for msg in msgs:
                if not msg or bool(getattr(msg, "out", False)):
                    continue
                if not _message_mentions_account(msg, account.id, getattr(account, "username", "")):
                    continue

                msg_info = {
                    "id": msg.id,
                    "date": str(msg.date),
                    "text": (msg.text or "")[:500],
                }
                if msg.sender:
                    msg_info["from"] = getattr(msg.sender, "first_name", "") or getattr(msg.sender, "title", "")
                if msg.media:
                    msg_info["has_media"] = True
                    msg_info["media_type"] = type(msg.media).__name__
                mentions.append(msg_info)

            return [types.TextContent(type="text", text=json.dumps({
                "chat": target,
                "scanned": len(msgs),
                "count": len(mentions),
                "mentions": mentions,
            }, ensure_ascii=False, indent=2))]

        elif name == "send_message":
            target = arguments["chat"]
            text = arguments["text"]
            entity = await _resolve(client, target)
            result = await client.send_message(entity, text)
            return [types.TextContent(type="text", text=json.dumps({"success": True, "message_id": result.id, "chat": target}, ensure_ascii=False))]

        elif name == "send_location":
            target = arguments["chat"]
            latitude = float(arguments["latitude"])
            longitude = float(arguments["longitude"])
            entity = await _resolve(client, target)
            from telethon.tl.types import InputMediaGeoPoint, GeoPoint as TGeoPoint
            geo_point = InputMediaGeoPoint(geo_point=TGeoPoint(lat=latitude, long=longitude, access_hash=0))
            result = await client.send_message(entity, file=geo_point)
            return [types.TextContent(type="text", text=json.dumps({"success": True, "message_id": result.id, "chat": target, "lat": latitude, "lon": longitude}, ensure_ascii=False))]

        elif name == "search_messages":
            target = arguments["chat"]
            query = arguments["query"]
            limit = arguments.get("limit", 20)
            entity = await _resolve(client, target)
            msgs = await client.get_messages(entity, limit=limit, search=query)
            messages = []
            for msg in msgs:
                msg_info = {"id": msg.id, "date": str(msg.date), "text": (msg.text or "")[:300]}
                if msg.sender:
                    msg_info["from"] = getattr(msg.sender, 'first_name', '') or getattr(msg.sender, 'title', '')
                messages.append(msg_info)
            return [types.TextContent(type="text", text=json.dumps({"chat": target, "query": query, "count": len(messages), "messages": messages}, ensure_ascii=False, indent=2))]

        elif name == "join_chat":
            from telethon.tl import functions as tl_functions
            target = arguments["target"].strip()
            if target.startswith('@'):
                target = target[1:]
            try:
                entity = await _resolve(client, target)
                from telethon.tl.functions.channels import JoinChannelRequest
                result = await client(JoinChannelRequest(entity))
                title = ""
                for update in getattr(result, 'updates', []):
                    if hasattr(update, 'message') and hasattr(update.message, 'chat'):
                        title = getattr(update.message.chat, 'title', '')
                return [types.TextContent(type="text", text=json.dumps({"success": True, "title": title, "id": entity.id, "type": "joined_channel"}, ensure_ascii=False))]
            except Exception as e:
                try:
                    result = await client(functions.channels.JoinChannelRequest(target))
                    return [types.TextContent(type="text", text=json.dumps({"success": True, "id": 0, "type": "joined_channel"}, ensure_ascii=False))]
                except:
                    return [types.TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]

        elif name == "leave_chat":
            target = arguments["target"]
            entity = await _resolve(client, target)
            await client.delete_dialog(entity)
            return [types.TextContent(type="text", text=json.dumps({"success": True, "left": target}, ensure_ascii=False))]

        elif name == "get_members":
            target = arguments["chat"]
            limit = arguments.get("limit", 50)
            entity = await _resolve(client, target)
            members = []
            async for user in client.iter_participants(entity, limit=limit):
                members.append({"id": user.id, "name": getattr(user, 'first_name', ''), "username": getattr(user, 'username', '') or '', "bot": user.bot})
            return [types.TextContent(type="text", text=json.dumps({"chat": target, "count": len(members), "members": members}, ensure_ascii=False, indent=2))]

        elif name == "download_media":
            target = arguments["chat"]
            msg_id = arguments["message_id"]
            output_dir = arguments.get("output_dir") or os.path.join(
                os.path.dirname(SESSION_NAME) or ".", "downloads"
            )
            os.makedirs(output_dir, exist_ok=True)
            entity = await _resolve(client, target)
            msg = await client.get_messages(entity, ids=msg_id)
            if msg and msg.media:
                path = await client.download_media(msg, file=output_dir)
                return [types.TextContent(type="text", text=json.dumps({"success": True, "path": path}, ensure_ascii=False))]
            return [types.TextContent(type="text", text=json.dumps({"error": "No media in message"}))]

        elif name == "pin_message":
            target = arguments["chat"]
            msg_id = arguments["message_id"]
            action = arguments.get("action", "pin")
            entity = await _resolve(client, target)
            if action == "unpin":
                await client.unpin_message(entity, msg_id)
            else:
                await client.pin_message(entity, msg_id)
            return [types.TextContent(type="text", text=json.dumps({"success": True, "action": action, "message_id": msg_id}, ensure_ascii=False))]

        elif name == "add_reaction":
            target = arguments["chat"]
            msg_id = arguments["message_id"]
            emoji = arguments["emoji"]
            entity = await _resolve(client, target)
            await client.send_reaction(entity, msg_id, emoji)
            return [types.TextContent(type="text", text=json.dumps({"success": True, "reaction": emoji}, ensure_ascii=False))]

        elif name == "forward_messages":
            from_chat = arguments["from_chat"]
            to_chat = arguments["to_chat"]
            msg_ids = [int(x) for x in arguments["message_ids"].split(",")]
            from_entity = await _resolve(client, from_chat)
            to_entity = await _resolve(client, to_chat)
            await client.forward_messages(to_entity, msg_ids, from_entity)
            return [types.TextContent(type="text", text=json.dumps({"success": True, "forwarded": len(msg_ids), "from": from_chat, "to": to_chat}, ensure_ascii=False))]

        elif name == "profile":
            me = await client.get_me()
            info = {"id": me.id, "first_name": me.first_name, "last_name": me.last_name or '', "username": me.username or '', "phone": me.phone or ''}
            return [types.TextContent(type="text", text=json.dumps(info, ensure_ascii=False, indent=2))]

        elif name == "scan_channels_content":
            # Bounded scan: timeout + max_channels prevent MCP restart loops
            messages_per_channel = arguments.get("messages_per_channel") or arguments.get("limit", 3)
            max_channels = arguments.get("max_channels", 25)
            filter_type = arguments.get("filter", "channels")
            offset = arguments.get("offset", 0)
            timeout_seconds = arguments.get("timeout_seconds", 55)
            default_save = os.path.join(os.path.dirname(SESSION_NAME) or ".", "scan_results.json")
            save_to = arguments.get("save_to") or default_save
            max_text_length = arguments.get("max_text_length", 200)
            import time
            start_time = time.time()
            results = []
            scanned = 0
            skipped = 0
            timed_out = False
            channel_index = 0
            async for dialog in client.iter_dialogs():
                channel_index += 1
                entity = dialog.entity
                if isinstance(entity, (ChannelForbidden, ChatForbidden)):
                    continue
                if isinstance(entity, Channel):
                    is_channel = entity.broadcast
                    is_group = entity.megagroup
                    if filter_type == "channels" and not is_channel:
                        continue
                    if filter_type == "groups" and not is_group:
                        continue
                elif isinstance(entity, User):
                    if filter_type != "all":
                        continue
                else:
                    continue
                # Apply offset: skip first N channels
                if skipped < offset:
                    skipped += 1
                    continue
                # Check max_channels limit
                if scanned >= max_channels:
                    break
                # Check timeout: stop if we're close to the limit
                elapsed = time.time() - start_time
                if elapsed > timeout_seconds - 5:
                    timed_out = True
                    break
                try:
                    msgs = await client.get_messages(entity, limit=messages_per_channel)
                    msg_texts = []
                    for msg in msgs:
                        if msg and msg.text:
                            msg_texts.append(msg.text[:max_text_length])
                    results.append({
                        "id": entity.id,
                        "title": getattr(entity, 'title', '') or getattr(entity, 'first_name', ''),
                        "username": getattr(entity, 'username', '') or '',
                        "type": "channel" if (isinstance(entity, Channel) and entity.broadcast) else ("group" if isinstance(entity, Channel) else "user"),
                        "members": getattr(entity, 'participants_count', 0) or 0,
                        "messages": msg_texts,
                    })
                    scanned += 1
                except Exception as e:
                    results.append({
                        "id": entity.id,
                        "title": getattr(entity, 'title', '') or getattr(entity, 'first_name', ''),
                        "username": getattr(entity, 'username', '') or '',
                        "type": "error",
                        "members": 0,
                        "messages": [],
                        "error": str(e),
                    })
                    scanned += 1
            # Save full results to file
            save_data = {"total": len(results), "scanned": scanned, "offset": offset + skipped, "channels": results}
            os.makedirs(os.path.dirname(save_to) or ".", exist_ok=True)
            with open(save_to, "w", encoding="utf-8") as f:
                json.dump(save_data, f, ensure_ascii=False, indent=2)
            file_size_kb = round(os.path.getsize(save_to) / 1024, 1)
            elapsed_total = round(time.time() - start_time, 1)
            # Return compact summary only
            summary = {
                "total_in_file": len(results),
                "scanned": scanned,
                "offset_used": offset,
                "next_offset": offset + scanned,
                "timed_out": timed_out,
                "elapsed_seconds": elapsed_total,
                "saved_to": save_to,
                "file_size_kb": file_size_kb,
            }
            return [types.TextContent(type="text", text=json.dumps(summary, ensure_ascii=False))]

        elif name == "leave_channels":
            targets = arguments["targets"]
            left = []
            failed = []
            _dialog_cache = None  # lazy {entity.id: entity} of current dialogs
            for target in targets:
                try:
                    try:
                        entity = await _resolve(client, target)
                    except Exception:
                        # Private / no-username channels can't be resolved by bare id.
                        # Fall back to matching them against the user's current dialogs.
                        if _dialog_cache is None:
                            _dialog_cache = {}
                            async for d in client.iter_dialogs():
                                _dialog_cache[d.entity.id] = d.entity
                        key = int(target) if str(target).lstrip('-').isdigit() else None
                        entity = _dialog_cache.get(key) or _dialog_cache.get(abs(key)) if key is not None else None
                        if entity is None:
                            raise
                    await client.delete_dialog(entity)
                    title = getattr(entity, 'title', '') or getattr(entity, 'first_name', '')
                    left.append({"id": target, "title": title})
                except Exception as e:
                    failed.append({"id": target, "error": str(e)})
            return [types.TextContent(type="text", text=json.dumps({"left": left, "failed": failed, "left_count": len(left), "failed_count": len(failed)}, ensure_ascii=False))]

        elif name == "block_chats":
            from telethon.tl.functions.contacts import BlockRequest
            targets = arguments["targets"]
            blocked = []
            failed = []
            _dialog_cache = None  # lazy {entity.id: entity} of current dialogs
            for target in targets:
                try:
                    try:
                        entity = await _resolve(client, target)
                    except Exception:
                        if _dialog_cache is None:
                            _dialog_cache = {}
                            async for d in client.iter_dialogs():
                                _dialog_cache[d.entity.id] = d.entity
                        key = int(target) if str(target).lstrip('-').isdigit() else None
                        entity = _dialog_cache.get(key) or _dialog_cache.get(abs(key)) if key is not None else None
                        if entity is None:
                            raise
                    # Block stops the bot/user from messaging; then drop the dialog so it
                    # disappears from the chat list.
                    await client(BlockRequest(id=entity))
                    try:
                        await client.delete_dialog(entity)
                    except Exception:
                        pass
                    title = getattr(entity, 'first_name', '') or getattr(entity, 'title', '')
                    blocked.append({"id": target, "title": title})
                except Exception as e:
                    failed.append({"id": target, "error": str(e)})
            return [types.TextContent(type="text", text=json.dumps({"blocked": blocked, "failed": failed, "blocked_count": len(blocked), "failed_count": len(failed)}, ensure_ascii=False))]

        elif name == "search_public":
            from telethon.tl.functions.contacts import SearchRequest
            query = arguments["query"]
            limit = int(arguments.get("limit", 50))
            res = await client(SearchRequest(q=query, limit=limit))
            channels = []
            for ch in res.chats:
                if isinstance(ch, Channel):
                    channels.append({
                        "id": ch.id,
                        "title": ch.title,
                        "username": getattr(ch, 'username', '') or '',
                        "members": getattr(ch, 'participants_count', 0) or 0,
                        "is_group": bool(ch.megagroup),
                        "is_channel": bool(ch.broadcast),
                        "verified": bool(getattr(ch, 'verified', False)),
                        "scam": bool(getattr(ch, 'scam', False)),
                        "fake": bool(getattr(ch, 'fake', False)),
                    })
            users = []
            for u in res.users:
                if isinstance(u, User):
                    users.append({"id": u.id, "name": getattr(u, 'first_name', '') or '', "username": getattr(u, 'username', '') or '', "bot": bool(u.bot)})
            return [types.TextContent(type="text", text=json.dumps({"query": query, "channels": channels, "users": users, "channel_count": len(channels)}, ensure_ascii=False))]

        elif name == "get_recommendations":
            from telethon.tl.functions.channels import GetChannelRecommendationsRequest
            target = arguments["channel"]
            # resolve from dialog cache first to avoid ResolveUsername when possible
            try:
                entity = await _resolve(client, target)
            except Exception:
                cache = {}
                async for d in client.iter_dialogs():
                    cache[d.entity.id] = d.entity
                    if getattr(d.entity, 'username', ''):
                        cache[d.entity.username.lower()] = d.entity
                key = int(target) if str(target).lstrip('-').isdigit() else str(target).lstrip('@').lower()
                entity = cache.get(key)
                if entity is None:
                    raise
            res = await client(GetChannelRecommendationsRequest(channel=entity))
            channels = []
            for ch in getattr(res, 'chats', []):
                if isinstance(ch, Channel):
                    channels.append({
                        "id": ch.id, "title": ch.title,
                        "username": getattr(ch, 'username', '') or '',
                        "members": getattr(ch, 'participants_count', 0) or 0,
                        "scam": bool(getattr(ch, 'scam', False)), "fake": bool(getattr(ch, 'fake', False)),
                    })
            return [types.TextContent(type="text", text=json.dumps({"seed": target, "recommendations": channels, "count": len(channels)}, ensure_ascii=False))]

        elif name == "extract_refs":
            import re
            target = arguments["chat"]
            limit = int(arguments.get("limit", 100))
            try:
                entity = await _resolve(client, target)
            except Exception:
                cache = {}
                async for d in client.iter_dialogs():
                    cache[d.entity.id] = d.entity
                    if getattr(d.entity, 'username', ''):
                        cache[d.entity.username.lower()] = d.entity
                key = int(target) if str(target).lstrip('-').isdigit() else str(target).lstrip('@').lower()
                entity = cache.get(key)
                if entity is None:
                    raise
            mentions = {}
            invites = {}
            forwards = {}
            async for msg in client.iter_messages(entity, limit=limit):
                t = msg.text or ""
                for m in re.findall(r'@([A-Za-z][A-Za-z0-9_]{3,31})', t):
                    mentions[m.lower()] = mentions.get(m.lower(), 0) + 1
                for inv in re.findall(r't\.me/(\+[\w-]+|joinchat/[\w-]+)', t):
                    invites[inv] = invites.get(inv, 0) + 1
                for pub in re.findall(r't\.me/([A-Za-z][A-Za-z0-9_]{3,31})', t):
                    mentions[pub.lower()] = mentions.get(pub.lower(), 0) + 1
                if msg.fwd_from and getattr(msg.fwd_from, 'from_id', None) is not None:
                    fid = msg.fwd_from.from_id
                    cid = getattr(fid, 'channel_id', None)
                    if cid:
                        forwards[cid] = forwards.get(cid, 0) + 1
            return [types.TextContent(type="text", text=json.dumps({
                "chat": target,
                "mentions": sorted(mentions.items(), key=lambda x: -x[1]),
                "invite_links": sorted(invites.items(), key=lambda x: -x[1]),
                "forward_channel_ids": sorted(forwards.items(), key=lambda x: -x[1]),
            }, ensure_ascii=False))]

        elif name == "click_button":
            chat_target = arguments["chat"]
            message_id = arguments["message_id"]
            button_text = arguments.get("button_text", "")
            row_idx = arguments.get("row")
            col_idx = arguments.get("col")

            entity = await _resolve(client, chat_target)
            msg = await client.get_messages(entity, ids=message_id)
            if not msg:
                return [types.TextContent(type="text", text=json.dumps({"error": f"Message {message_id} not found"}))]
            if isinstance(msg, list):
                msg = msg[0]

            if not msg.reply_markup or not hasattr(msg.reply_markup, 'rows'):
                return [types.TextContent(type="text", text=json.dumps({"error": "No inline keyboard on this message", "message_id": message_id}))]

            # Collect available buttons info
            buttons_info = []
            for r_idx, row in enumerate(msg.reply_markup.rows):
                row_buttons = [btn.text for btn in row.buttons]
                buttons_info.append(row_buttons)

            # --- Primary method: Use low-level GetBotCallbackAnswerRequest ---
            button = None
            callback_msg = None
            callback_alert = None
            used_low_level = False

            # Find the button by text or by row/col
            if button_text:
                # Exact match first
                for row in msg.reply_markup.rows:
                    for btn in row.buttons:
                        if btn.text == button_text:
                            button = btn
                            break
                    if button:
                        break
                # Fuzzy match if no exact match
                if not button:
                    for row in msg.reply_markup.rows:
                        for btn in row.buttons:
                            if button_text in btn.text:
                                button = btn
                                break
                        if button:
                            break
            elif row_idx is not None and col_idx is not None:
                # Index-based selection
                if row_idx < len(msg.reply_markup.rows) and col_idx < len(msg.reply_markup.rows[row_idx].buttons):
                    button = msg.reply_markup.rows[row_idx].buttons[col_idx]
            elif row_idx is not None:
                if row_idx < len(msg.reply_markup.rows) and len(msg.reply_markup.rows[row_idx].buttons) > 0:
                    button = msg.reply_markup.rows[row_idx].buttons[0]

            if button and hasattr(button, 'data'):
                try:
                    result = await client(GetBotCallbackAnswerRequest(
                        peer=entity,
                        msg_id=message_id,
                        data=button.data if button.data else b''
                    ))
                    callback_msg = result.message
                    callback_alert = result.alert
                    used_low_level = True
                except Exception as e:
                    # Low-level API failed, will fall back to msg.click()
                    callback_msg = None
                    callback_alert = None
                    used_low_level = False

            # --- Fallback: Use Telethon's built-in msg.click() ---
            if not used_low_level:
                callback_answer_obj = None
                try:
                    if button_text:
                        callback_answer_obj = await msg.click(text=button_text)
                    elif row_idx is not None and col_idx is not None:
                        callback_answer_obj = await msg.click(row=row_idx, col=col_idx)
                    elif row_idx is not None:
                        callback_answer_obj = await msg.click(row=row_idx, col=0)
                    else:
                        # Default: click first button
                        callback_answer_obj = await msg.click(0)
                except Exception as e:
                    return [types.TextContent(type="text", text=json.dumps({"error": f"Click failed: {str(e)}", "available_buttons": buttons_info}))]

                # Extract callback answer from msg.click() return value
                if callback_answer_obj:
                    callback_msg = getattr(callback_answer_obj, 'message', None) or str(callback_answer_obj)
                    callback_alert = getattr(callback_answer_obj, 'alert', None)

            # Small delay to let the bot respond (edit message, send new message, etc.)
            await asyncio.sleep(2.5)

            # Read the latest messages to see bot's response
            recent = await client.get_messages(entity, limit=10)
            response_texts = []
            for m in recent:
                if m.id > message_id and m.text:
                    response_texts.append({"id": m.id, "text": m.text[:500]})

            # Check if the clicked message was EDITED (common for callback bots)
            edited_msg = await client.get_messages(entity, ids=message_id)
            edited_text = None
            edited_buttons = None
            if edited_msg and isinstance(edited_msg, list):
                edited_msg = edited_msg[0] if edited_msg else None
            # Handle the case where get_messages returns a Message object directly
            if edited_msg and hasattr(edited_msg, 'text'):
                if edited_msg.text and edited_msg.text not in [r.get("text", "") for r in response_texts]:
                    edited_text = edited_msg.text[:500]
                if hasattr(edited_msg, 'reply_markup') and edited_msg.reply_markup and hasattr(edited_msg.reply_markup, 'rows'):
                    edited_buttons = [[btn.text for btn in row.buttons] for row in edited_msg.reply_markup.rows]

            clicked_label = button_text or (f"row={row_idx},col={col_idx}" if row_idx is not None else "first button")
            result = {
                "clicked_button": clicked_label,
                "message_id": message_id,
                "chat": chat_target,
                "bot_response": response_texts,
                "available_buttons": buttons_info
            }
            # Include callback answer (popup/alert text from the bot)
            if callback_msg:
                result["callback_answer"] = callback_msg
            if callback_alert is not None:
                result["callback_alert"] = callback_alert
            result["method"] = "GetBotCallbackAnswerRequest" if used_low_level else "msg.click()"
            if edited_text:
                result["edited_message"] = edited_text
            if edited_buttons:
                result["edited_buttons"] = edited_buttons

            return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "request_web_app":
            bot_target = arguments["bot"]
            short_name = arguments.get("short_name", "app")
            platform = arguments.get("platform", "android")
            
            names_to_try = [short_name] if short_name != "app" else ["app", "webapp", "main", "web", "dordor", "bot", "index"]
            
            bot_entity = await _resolve(client, bot_target)
            input_user = InputUser(user_id=bot_entity.id, access_hash=bot_entity.access_hash)
            results = []
            found_url = None
            
            for name in names_to_try:
                try:
                    result = await client(RequestAppWebViewRequest(
                        peer=bot_entity,
                        app=InputBotAppShortName(bot_id=input_user, short_name=name),
                        platform=platform,
                        write_allowed=True
                    ))
                    results.append({"short_name": name, "url": result.url})
                    found_url = result.url
                    break
                except Exception as e:
                    err = str(e)
                    if "BOT_APP_INVALID" in err or "APP_SHORTNAME" in err or "not found" in err.lower():
                        results.append({"short_name": name, "error": "not_found"})
                    else:
                        results.append({"short_name": name, "error": f"{type(e).__name__}: {err[:200]}"})
            
            return [types.TextContent(type="text", text=json.dumps({
                "bot": bot_target,
                "bot_id": bot_entity.id,
                "has_main_app": getattr(bot_entity, 'bot_has_main_app', None),
                "found_url": found_url,
                "tried": results,
                "platform": platform
            }, ensure_ascii=False, indent=2))]

        else:
            return [types.TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    except Exception as e:
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())