"""Credential-free, network-free tests of account isolation and onboarding safety."""
import asyncio
import contextlib
import getpass
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from telegram_accounts import Account, AccountError, AccountManager, SessionLock
import onboarding


class FakeClient:
    made = []
    authorized = True
    user_id = 123
    fail_connect = False
    fail_disconnect = False

    def __init__(self, session, api_id, api_hash):
        self.session = session
        self.connected = False
        self.disconnected = False
        self.__class__.made.append(self)

    def is_connected(self):
        return self.connected

    async def connect(self):
        await asyncio.sleep(0)
        if self.fail_connect:
            raise asyncio.CancelledError()
        self.connected = True

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return type('User', (), {'id': self.user_id})()

    async def disconnect(self):
        self.connected = False
        self.disconnected = True
        if self.fail_disconnect:
            raise RuntimeError('sensitive exception text')


class AccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        FakeClient.made = []
        FakeClient.authorized = True
        FakeClient.fail_connect = False
        FakeClient.fail_disconnect = False
        self.entries = []
        for alias in ('work', 'personal'):
            session = self.root / (alias + '.session')
            session.touch(mode=0o600)
            self.entries.append(Account(alias, 1, 'fake-hash', session, 123))
        self.manager = AccountManager(self.entries, FakeClient)

    async def asyncTearDown(self):
        await self.manager.close()
        self.tmp.cleanup()

    async def test_alias_required_and_no_fallback(self):
        for alias in (None, 'typo', ''):
            with self.assertRaises(AccountError):
                await self.manager.get_client(alias)
        self.assertFalse(FakeClient.made)

    async def test_concurrent_initialization_and_separate_clients(self):
        a, b = await asyncio.gather(self.manager.get_client('work'), self.manager.get_client('work'))
        c = await self.manager.get_client('personal')
        self.assertIs(a, b)
        self.assertIsNot(a, c)
        self.assertEqual(len(FakeClient.made), 2)
        self.assertNotEqual(a.session, c.session)
        self.assertNotEqual(self.entries[0].output_dir, self.entries[1].output_dir)

    async def test_unauthorized_not_cached_and_lock_released(self):
        FakeClient.authorized = False
        for _ in range(2):
            with self.assertRaises(AccountError):
                await self.manager.get_client('work')
        self.assertFalse(self.manager.clients)
        self.assertTrue(all(c.disconnected for c in FakeClient.made))
        FakeClient.authorized = True
        await self.manager.get_client('work')

    async def test_wrong_identity_fails_closed(self):
        with patch.object(FakeClient, 'user_id', 456):
            with self.assertRaises(AccountError):
                await self.manager.get_client('personal')
        self.assertFalse(self.manager.clients)
        self.assertTrue(FakeClient.made[0].disconnected)

    async def test_cancelled_connect_disconnects_and_unlocks(self):
        FakeClient.fail_connect = True
        with self.assertRaises(asyncio.CancelledError):
            await self.manager.get_client('work')
        self.assertTrue(FakeClient.made[0].disconnected)
        self.assertFalse(self.manager.clients)
        lock = SessionLock(self.entries[0].session)
        lock.acquire()
        lock.release()

    async def test_missing_session_never_creates_client(self):
        self.entries[0].session.unlink()
        with self.assertRaises(AccountError):
            await self.manager.get_client('work')
        self.assertFalse(FakeClient.made)

    async def test_existing_process_lock_blocks(self):
        lock = SessionLock(self.entries[0].session)
        lock.acquire()
        try:
            with self.assertRaises(AccountError):
                await self.manager.get_client('work')
        finally:
            lock.release()
        self.assertFalse(FakeClient.made)

    async def test_close_attempts_all_clients_after_error(self):
        await self.manager.get_client('work')
        await self.manager.get_client('personal')
        FakeClient.fail_disconnect = True
        await self.manager.close()
        self.assertTrue(all(c.disconnected for c in FakeClient.made))
        self.assertFalse(self.manager.file_locks)

    async def test_duplicate_session_rejected(self):
        with self.assertRaises(AccountError):
            AccountManager([self.entries[0], Account('other', 1, 'fake', self.entries[0].session, 123)])

    async def test_close_waits_for_inflight_initialization(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_connect(client):
            entered.set()
            await release.wait()
            client.connected = True
        with patch.object(FakeClient, 'connect', slow_connect):
            opening = asyncio.create_task(self.manager.get_client('work'))
            await entered.wait()
            closing = asyncio.create_task(self.manager.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            release.set()
            await asyncio.gather(opening, closing)
        self.assertFalse(self.manager.clients)
        self.assertTrue(FakeClient.made[0].disconnected)
        with self.assertRaises(AccountError):
            await self.manager.get_client('work')

    async def test_api_credentials_reuse_environment_without_prompt_or_output(self):
        with patch.dict(os.environ, {'TG_API_ID': '42', 'TG_API_HASH': 'fake-vault-hash'}), \
             patch.object(onboarding, 'secret') as prompt, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(onboarding.api_credentials(), (42, 'fake-vault-hash'))
            prompt.assert_not_called()
            self.assertEqual(output.getvalue(), '')

    async def test_password_whitespace_preserved(self):
        with patch.object(sys.stdin, 'isatty', return_value=True), patch.object(sys.stderr, 'isatty', return_value=True), \
             patch.object(getpass, 'getpass', return_value='  password  '):
            self.assertEqual(onboarding.secret('Password: ', preserve_whitespace=True), '  password  ')

    async def test_private_named_config_identity_required(self):
        path = self.root / 'accounts.json'
        config = {'accounts': {'work': {'api_id': 1, 'api_hash': 'fake', 'session': 'work.session', 'expected_user_id': 123}}}
        path.write_text(json.dumps(config))
        path.chmod(0o600)
        with patch.dict(os.environ, {'TG_ACCOUNTS_FILE': str(path)}):
            manager = AccountManager.from_environment()
            self.assertEqual(manager.select('work').alias, 'work')
            self.assertEqual(manager.select('work').session, self.entries[0].session)
            del config['accounts']['work']['expected_user_id']
            path.write_text(json.dumps(config))
            with self.assertRaises(AccountError):
                AccountManager.from_environment()

    async def test_onboarding_rejects_non_tty_before_files(self):
        config = self.root / 'new' / 'accounts.json'
        with patch.object(sys.stdin, 'isatty', return_value=False):
            with self.assertRaises(AccountError):
                await onboarding.enroll('work', config, self.root / 'new-sessions')
        self.assertFalse(config.parent.exists())

    async def test_hidden_input_does_not_fall_back(self):
        with patch.object(sys.stdin, 'isatty', return_value=True), patch.object(sys.stderr, 'isatty', return_value=True), \
             patch.object(getpass, 'getpass', side_effect=getpass.GetPassWarning()):
            with self.assertRaises(AccountError):
                onboarding.secret('Test: ')

    async def test_atomic_config_and_cancelled_main(self):
        path = self.root / 'accounts.json'
        onboarding.write_config(path, {'accounts': {}})
        self.assertEqual(json.loads(path.read_text()), {'accounts': {}})
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with patch.object(sys, 'argv', ['onboarding.py', 'work']), \
             patch.object(onboarding, 'enroll', AsyncMock(side_effect=KeyboardInterrupt)), \
             patch.object(asyncio, 'run', side_effect=lambda coro: (coro.close(), (_ for _ in ()).throw(KeyboardInterrupt()))), \
             contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(onboarding.main(), 1)
        self.assertNotIn('fake-hash', output.getvalue())
