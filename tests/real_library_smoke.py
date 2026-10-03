"""Run separately with real installed dependencies; fake transport, no live client."""
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(TG_API_ID='1', TG_API_HASH='offline-test', TG_SESSION_NAME='/nonexistent/offline')
os.environ.pop('TG_ACCOUNTS_FILE', None)
from telethon import TelegramClient, utils
from telethon.tl import types
from telethon.tl.functions.messages import GetDialogFiltersRequest, UpdateDialogFilterRequest, SendReactionRequest
from telethon.tl.functions.folders import EditPeerFoldersRequest
import telegram_folders as folders
with patch('dotenv.load_dotenv'):
    import telegram_mcp_server as server


class Transport:
    def __init__(self):
        self.requests = []
        self.folder = types.DialogFilter(id=2, title=types.TextWithEntities(text='Work', entities=[]),
            pinned_peers=[], include_peers=[types.InputPeerUser(123, 0)], exclude_peers=[], groups=True)

    async def __call__(self, request):
        # This exercises real Telethon serialization without any network connection.
        assert bytes(request)
        if isinstance(request, GetDialogFiltersRequest):
            return SimpleNamespace(filters=[self.folder])
        self.requests.append(request)
        return True

    async def get_input_entity(self, target):
        return types.InputPeerUser(int(target), 0)

    async def get_entity(self, target):
        return types.InputPeerUser(int(target), 0)

    async def iter_dialogs(self):
        yield SimpleNamespace(entity=types.PeerUser(123), archived=False)

    async def send_message(self, entity, **kwargs):
        assert bytes(kwargs['file'])
        return SimpleNamespace(id=7)


async def main():
    with patch.object(TelegramClient, '__init__', side_effect=AssertionError('Live clients prohibited')):
        assert server.callback_data(types.KeyboardInlineButton(text='OK', type=types.InlineButtonTypeCallback(data=b'yes'))) == b'yes'
        assert server.callback_data(types.KeyboardInlineButton(text='Login', type=types.InlineButtonTypeUrl(url='https://example.invalid'))) is None
        assert server.callback_data(types.KeyboardInlineButton(text='Password', type=types.InlineButtonTypeCallback(data=b'yes', requires_password=True))) is None
        client = Transport()
        result = await folders.manage_folders(client, 'create_folder', {'folder_id': 3, 'title': 'Personal', 'chats': ['456']})
        assert result['folder']['include_peers'] == [456]
        await folders.manage_folders(client, 'update_folder', {'folder_id': 2, 'add_chats': ['456']})
        assert client.requests[-1].filter.groups
        await folders.set_archived(client, ['123'], True)
        assert isinstance(client.requests[-1], EditPeerFoldersRequest)
        assert client.requests[-1].folder_peers[0].folder_id == 1
        assert utils.get_peer_id(types.InputPeerChannel(42, 0)) == -1000000000042
        with patch.object(server, 'get_client', AsyncMock(return_value=client)):
            tools = await server.list_tools()
            assert len(tools) == 28
            result = await server.call_tool('add_reaction', {'chat': '123', 'message_id': 1, 'emoji': '👍'})
            assert json.loads(result[0].text)['success']
            assert isinstance(client.requests[-1], SendReactionRequest)
            result = await server.call_tool('send_location', {'chat': '123', 'latitude': 10.0, 'longitude': 20.0})
            assert json.loads(result[0].text)['success']
    print('Real-library serialization/import smoke passed; no Telegram connection')
    print({p: importlib.metadata.version(p) for p in ('telethon', 'mcp', 'python-dotenv')})


asyncio.run(main())
