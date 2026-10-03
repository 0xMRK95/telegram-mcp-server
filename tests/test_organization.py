"""Offline tests: Telethon is stubbed; no session, credentials, or network used."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types as pytypes
import unittest
from unittest.mock import AsyncMock, patch


class Record:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class DialogFilter:
    def __init__(self, id, title: 'TypeTextWithEntities', pinned_peers, include_peers, exclude_peers, **kwargs):
        self.id, self.title = id, title
        self.pinned_peers, self.include_peers, self.exclude_peers = pinned_peers, include_peers, exclude_peers
        self.__dict__.update(kwargs)


def peer_id(peer):
    return peer.id


# Replace only the external Telegram transport and types, using keyword request fields.
modules = {}
for name in ('telethon', 'telethon.utils', 'telethon.tl', 'telethon.tl.types',
             'telethon.tl.functions', 'telethon.tl.functions.messages', 'telethon.tl.functions.folders'):
    modules[name] = pytypes.ModuleType(name)
class NoLiveClient:
    def __init__(self, *a, **kw):
        raise AssertionError('No live client')
modules['telethon'].TelegramClient = NoLiveClient
modules['telethon'].utils = modules['telethon.utils']
modules['telethon.utils'].get_peer_id = peer_id
modules['telethon.tl'].types = modules['telethon.tl.types']
for name in ('ChannelForbidden', 'ChatForbidden', 'Channel', 'Chat', 'User', 'KeyboardButtonCallback',
             'KeyboardButtonUrl', 'KeyboardButtonSwitchInline', 'InputMediaGeoPoint', 'GeoPoint',
             'InputBotAppShortName', 'InputUser', 'TextWithEntities', 'InputFolderPeer'):
    setattr(modules['telethon.tl.types'], name, type(name, (Record,), {}))
modules['telethon.tl.types'].DialogFilter = DialogFilter
for name in ('GetDialogFiltersRequest', 'UpdateDialogFilterRequest', 'GetBotCallbackAnswerRequest', 'RequestAppWebViewRequest'):
    setattr(modules['telethon.tl.functions.messages'], name, type(name, (Record,), {}))
modules['telethon.tl.functions.folders'].EditPeerFoldersRequest = type('EditPeerFoldersRequest', (Record,), {})
with patch.dict(sys.modules, modules), patch.dict(os.environ, {'TG_API_ID': '1', 'TG_API_HASH': 'offline-test', 'TG_SESSION_NAME': '/nonexistent/offline'}), patch('dotenv.load_dotenv'):
    folders = importlib.import_module('telegram_folders')
    server = importlib.import_module('telegram_mcp_server')


class Client:
    def __init__(self, filters=(), dialogs=()):
        self.filters, self.dialogs = list(filters), list(dialogs)
        self.writes = []

    async def __call__(self, request):
        if type(request).__name__ == 'GetDialogFiltersRequest':
            return Record(filters=self.filters)
        self.writes.append(request)
        return True

    async def get_input_entity(self, target):
        return Record(id=int(target))

    async def iter_dialogs(self):
        for d in self.dialogs:
            yield d


def make_folder(**kwargs):
    values = dict(id=2, title=Record(text='Original'), pinned_peers=[Record(id=10)],
                  include_peers=[Record(id=10), Record(id=11)], exclude_peers=[Record(id=90)],
                  groups=True, exclude_archived=True, color=3, emoticon='⭐', title_noanimate=True)
    values.update(kwargs)
    return DialogFilter(**values)


class OrganizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mode_patch = patch.object(server, 'MODE', 'general')
        self.mode_patch.start()
        self.addCleanup(self.mode_patch.stop)

    async def test_organization_policy_blocks_forged_writes_before_network(self):
        blocked = ('send_message', 'send_location', 'forward_messages', 'pin_message',
                   'add_reaction', 'join_chat', 'leave_chat', 'leave_channels', 'block_chats',
                   'click_button', 'request_web_app', 'update_folder', 'archive_chats',
                   'unarchive_chats', 'download_media', 'unknown_tool')
        with patch.object(server, 'MODE', 'organization'), patch.object(server, 'get_client', AsyncMock()) as get_client:
            tools = await server.list_tools()
            self.assertEqual({t.name for t in tools}, server.ORGANIZATION_TOOLS)
            for name in blocked:
                result = json.loads((await server.call_tool(name, {'account': 'default', 'folder_id': 2}))[0].text)
                self.assertIn('blocked', result['error'])
            get_client.assert_not_called()

    async def test_create_rechecks_occupied_id_without_write(self):
        client = Client()
        responses = iter([[], [make_folder(id=3)]])
        async def changed_filters(request):
            if type(request).__name__ == 'GetDialogFiltersRequest':
                return next(responses)
            raise AssertionError('Must not mutate an occupied ID')
        with patch.object(Client, '__call__', side_effect=changed_filters):
            with self.assertRaises(ValueError):
                await folders.manage_folders(client, 'create_folder', {'folder_id': 3, 'title': 'Work', 'chats': ['12']})

    async def test_list_and_create(self):
        client = Client([make_folder()])
        result = await folders.manage_folders(client, 'list_folders', {})
        self.assertEqual(result['folders'][0]['include_peers'], [10, 11])
        self.assertFalse(client.writes)
        result = await folders.manage_folders(client, 'create_folder', {'folder_id': 3, 'title': 'Work', 'chats': ['12', '12']})
        self.assertEqual(result['folder']['include_peers'], [12])
        self.assertEqual(client.writes[0].filter.title.text, 'Work')

    async def test_create_cannot_overwrite_or_use_reserved_id(self):
        client = Client([make_folder()])
        for id in (0, 1, True, '3', 2):
            with self.assertRaises(ValueError):
                await folders.manage_folders(client, 'create_folder', {'folder_id': id, 'title': 'Work', 'chats': ['12']})
        self.assertFalse(client.writes)

    async def test_update_preserves_settings_and_original_object(self):
        original = make_folder()
        client = Client([original])
        result = await folders.manage_folders(client, 'update_folder', {'folder_id': 2, 'add_chats': ['12'], 'remove_chats': ['10']})
        updated = client.writes[0].filter
        self.assertEqual([p.id for p in updated.include_peers], [11, 12])
        self.assertEqual(updated.pinned_peers, [])
        for attr in ('groups', 'exclude_archived', 'color', 'emoticon', 'title_noanimate'):
            self.assertEqual(getattr(updated, attr), getattr(original, attr))
        self.assertEqual([p.id for p in original.include_peers], [10, 11])
        self.assertEqual([p.id for p in updated.exclude_peers], [90])
        self.assertEqual(result['before']['include_peers'], [10, 11])

    async def test_invalid_updates_never_write(self):
        client = Client([make_folder()])
        for args in ({'add_chats': ['90']}, {'add_chats': ['12'], 'remove_chats': ['12']},
                     {'title': ''}, {'title': 'x'*13}, {'add_chats': [4]}, {}):
            with self.assertRaises(ValueError):
                await folders.manage_folders(client, 'update_folder', {'folder_id': 2, **args})
        self.assertFalse(client.writes)

    async def test_protect_missing_and_shared_folders(self):
        client = Client([Record(id=3)])
        for id in (2, 3):
            with self.assertRaises(ValueError):
                await folders.manage_folders(client, 'update_folder', {'folder_id': id, 'title': 'Work'})
        self.assertFalse(client.writes)

    async def test_archive_unarchive_and_idempotence(self):
        client = Client(dialogs=[Record(entity=Record(id=-1001), archived=False), Record(entity=Record(id=2), archived=True)])
        result = await folders.set_archived(client, ['-1001', '2'], True)
        self.assertEqual(result['changed_ids'], [-1001])
        self.assertEqual(client.writes[0].folder_peers[0].folder_id, 1)
        self.assertEqual(result['previous'], [{'id': -1001, 'archived': False}, {'id': 2, 'archived': True}])
        await folders.set_archived(client, ['2'], True)
        self.assertEqual(len(client.writes), 1)
        await folders.set_archived(client, ['2'], False)
        self.assertEqual(client.writes[-1].folder_peers[0].folder_id, 0)

    async def test_invalid_archive_batch_never_writes(self):
        client = Client(dialogs=[Record(entity=Record(id=1), archived=False)])
        for chats in ([], ['1', '2'], [None], ['1']*101):
            with self.assertRaises(ValueError):
                await folders.set_archived(client, chats, True)
        self.assertFalse(client.writes)

    async def test_transport_error_propagates_without_retry(self):
        client = Client([make_folder()])
        async def request(req):
            if type(req).__name__ == 'GetDialogFiltersRequest':
                return client.filters  # Also support older Telethon vector responses.
            client.writes.append(req)
            raise TimeoutError('unknown server outcome')
        with patch.object(Client, '__call__', side_effect=request):
            with self.assertRaises(TimeoutError):
                await folders.manage_folders(client, 'update_folder', {'folder_id': 2, 'title': 'Work'})
        self.assertEqual(len(client.writes), 1)

    async def test_server_registration_dispatch_and_basic_group(self):
        tools = await server.list_tools()
        self.assertEqual(len(tools), 28)
        client = Client(dialogs=[Record(entity=server.Chat(id=-3, title='Small group', participants_count=2), archived=True)])
        with patch.object(server, 'get_client', AsyncMock(return_value=client)):
            result = json.loads((await server.call_tool('list_chats', {'filter': 'groups'}))[0].text)
            self.assertEqual(result['groups'][0]['chat_id'], -3)
            self.assertTrue(result['groups'][0]['archived'])
            result = json.loads((await server.call_tool('archive_chats', {'chats': ['-3']}))[0].text)
            self.assertTrue(result['success'])
            result = json.loads((await server.call_tool('create_folder', {'folder_id': 1}))[0].text)
            self.assertIn('error', result)

    async def test_named_dispatch_requires_alias_and_miniapp_stays_disabled(self):
        from telegram_accounts import Account, AccountManager
        manager = AccountManager([Account('work', 1, 'fake', Path('/nonexistent/work.session'), 123)])
        with patch.object(server, 'accounts', manager), patch.object(server, 'get_client', AsyncMock()) as get_client:
            tools = await server.list_tools()
            self.assertNotIn('request_web_app', [t.name for t in tools])
            for tool in tools:
                if tool.name != 'list_accounts':
                    self.assertIn('account', tool.inputSchema['required'])
            for name, args in (('profile', {}), ('profile', {'account': 'typo'}), ('request_web_app', {'account': 'work'})):
                result = json.loads((await server.call_tool(name, args))[0].text)
                self.assertIn('error', result)
            result = json.loads((await server.call_tool('list_accounts', {}))[0].text)
            self.assertEqual(result, {'accounts': ['work']})
            get_client.assert_not_called()

    async def test_callback_rejects_url_and_never_retries_timeout(self):
        url_button = server.tl_types.KeyboardButtonUrl(text='Login', url='https://example.invalid/secret-token')
        self.assertIsNone(server.callback_data(url_button))
        button = server.tl_types.KeyboardButtonCallback(text='Confirm', data=b'yes')
        msg = Record(reply_markup=Record(rows=[Record(buttons=[button])]))
        msg.click = AsyncMock()
        client = Client()
        client.get_entity = AsyncMock(return_value=Record(id=1))
        client.get_messages = AsyncMock(return_value=msg)
        async def fail(request):
            raise TimeoutError('secret-token')
        with patch.object(Client, '__call__', side_effect=fail), patch.object(server, 'get_client', AsyncMock(return_value=client)):
            result = json.loads((await server.call_tool('click_button', {'chat': '1', 'message_id': 2, 'button_text': 'Confirm'}))[0].text)
        self.assertEqual(result['error'], 'TimeoutError')
        self.assertNotIn('secret-token', str(result))
        msg.click.assert_not_called()

    async def test_older_string_title(self):
        class OldFilter:
            def __init__(self, title: str):
                pass
        with patch.object(folders.types, 'DialogFilter', OldFilter):
            self.assertEqual(folders._title('Work'), 'Work')

    async def test_dotenv_path_and_environment_precedence(self):
        # Verify only the load statement, avoiding module import/session creation.
        import ast
        source = ast.parse(Path(server.__file__).read_text())
        expression = next(n for n in source.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                          and isinstance(n.value.func, ast.Name) and n.value.func.id == 'load_dotenv')
        from dotenv import load_dotenv
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'TG_API_ID': '123'}):
            Path(tmp, '.env').write_text('TG_API_ID=999\nFOLDER_TEST_VALUE=loaded\n')
            with patch.dict(os.environ):
                exec(compile(ast.Module(body=[expression], type_ignores=[]), '<config-test>', 'exec'),
                     {'load_dotenv': load_dotenv, 'Path': Path, '__file__': str(Path(tmp, 'server.py'))})
                self.assertEqual(os.environ['TG_API_ID'], '123')
                self.assertEqual(os.environ['FOLDER_TEST_VALUE'], 'loaded')


if __name__ == '__main__':
    unittest.main()
