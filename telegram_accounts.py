"""Account routing and isolated authenticated Telethon client lifecycles."""
import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat


class AccountError(ValueError):
    """Safe, user-facing configuration/authentication error (never a secret)."""


def private_file(path):
    if os.name == 'posix' and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise AccountError('Private configuration/session must not be accessible to other users; use chmod 600')


class SessionLock:
    """An OS-held companion lock, released automatically on process exit."""
    def __init__(self, session):
        self.path = Path(str(session) + '.mcp-lock')
        self.file = None

    def acquire(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.file = os.fdopen(fd, 'r+b')
        try:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                raise AccountError('Session locking is unsupported on this operating system')
        except BaseException:
            self.file.close()
            self.file = None
            raise AccountError('Session is already in use or cannot be locked; stop other MCP/login processes') from None

    def release(self):
        if self.file:
            self.file.close()
            self.file = None


@dataclass(frozen=True)
class Account:
    alias: str
    api_id: int
    api_hash: str
    session: Path
    expected_user_id: int | None = None

    @property
    def session_name(self):
        return str(self.session)[:-len('.session')]

    @property
    def output_dir(self):
        return self.session.parent / (self.session.stem + '_output')


def _account(alias, raw, base):
    if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,31}', alias):
        raise AccountError('Account aliases must be 1–32 letters, digits, underscores, or hyphens')
    if not isinstance(raw, dict):
        raise AccountError('Each account must be an object')
    api_id, api_hash, session = raw.get('api_id'), raw.get('api_hash'), raw.get('session')
    if type(api_id) is not int or api_id <= 0 or not isinstance(api_hash, str) or not api_hash.strip():
        raise AccountError('Each account requires a positive api_id and nonempty api_hash')
    if not isinstance(session, str) or not session.strip():
        raise AccountError('Each account requires a session path')
    path = Path(session).expanduser()
    if path.suffix != '.session':
        path = Path(str(path) + '.session')
    path = (base / path).resolve() if not path.is_absolute() else path.resolve()
    expected = raw.get('expected_user_id')
    if expected is not None and (type(expected) is not int or expected <= 0):
        raise AccountError('expected_user_id must be a positive integer')
    return Account(alias, api_id, api_hash, path, expected)


class AccountManager:
    def __init__(self, accounts, client_factory=None, require_alias=True):
        self.closing = False
        self.require_alias = require_alias
        self.accounts = {a.alias: a for a in accounts}
        if not self.accounts:
            raise AccountError('Configure TG_ACCOUNTS_FILE or legacy TG_API_ID, TG_API_HASH, TG_SESSION_NAME')
        paths = [a.session for a in accounts]
        if len(paths) != len(set(paths)):
            raise AccountError('Each alias must have a distinct session path')
        self.clients, self.file_locks = {}, {}
        self.locks = {alias: asyncio.Lock() for alias in self.accounts}
        self.client_factory = client_factory

    @classmethod
    def from_environment(cls):
        config = os.environ.get('TG_ACCOUNTS_FILE')
        if config:
            path = Path(config).expanduser().resolve()
            try:
                private_file(path)
                data = json.loads(path.read_text())
                raw = data['accounts']
                if not isinstance(raw, dict):
                    raise AccountError('accounts must be an object keyed by alias')
                accounts = [_account(alias, value, path.parent) for alias, value in raw.items()]
                if any(a.expected_user_id is None for a in accounts):
                    raise AccountError('Named account configuration requires expected_user_id; use onboarding.py')
            except AccountError:
                raise
            except (OSError, ValueError, KeyError, TypeError):
                raise AccountError('Cannot read a valid private TG_ACCOUNTS_FILE') from None
            return cls(accounts)
        try:
            raw = {'api_id': int(os.environ.get('TG_API_ID', '')), 'api_hash': os.environ.get('TG_API_HASH'),
                   'session': os.environ.get('TG_SESSION_NAME')}
        except ValueError:
            raise AccountError('Set TG_ACCOUNTS_FILE or legacy Telegram environment variables') from None
        return cls([_account('default', raw, Path.cwd())], require_alias=False)

    def select(self, alias=None):
        if alias is None:
            if self.require_alias or len(self.accounts) != 1:
                raise AccountError('An explicit account alias is required; call list_accounts')
            return next(iter(self.accounts.values()))
        if not isinstance(alias, str) or alias not in self.accounts:
            raise AccountError('Unknown account alias; call list_accounts')
        return self.accounts[alias]

    async def get_client(self, alias=None):
        account = self.select(alias)
        async with self.locks[account.alias]:
            if self.closing:
                raise AccountError('Account manager is shutting down')
            current = self.clients.get(account.alias)
            if current is not None and current.is_connected():
                return current
            if current is not None:
                await self._close_one(account.alias)
            if not account.session.is_file():
                raise AccountError('Session is missing; run onboarding.py locally first')
            private_file(account.session)
            lock = SessionLock(account.session)
            lock.acquire()
            client = None
            try:
                factory = self.client_factory
                if factory is None:
                    from telethon import TelegramClient
                    factory = TelegramClient
                client = factory(account.session_name, account.api_id, account.api_hash)
                await client.connect()
                if not await client.is_user_authorized():
                    raise AccountError('Account is not authenticated; run onboarding.py locally')
                user = await client.get_me()
                if not user or (account.expected_user_id is not None and user.id != account.expected_user_id):
                    raise AccountError('Session identity does not match the configured account; refusing access')
                self.clients[account.alias] = client
                self.file_locks[account.alias] = lock
                return client
            except BaseException:
                try:
                    if client is not None:
                        await asyncio.shield(client.disconnect())
                finally:
                    lock.release()
                raise

    async def _close_one(self, alias):
        client = self.clients.pop(alias, None)
        lock = self.file_locks.pop(alias, None)
        try:
            if client is not None:
                await client.disconnect()
        finally:
            if lock:
                lock.release()

    async def close(self):
        # Wait for in-flight initialization and stop future acquisitions.
        self.closing = True
        async def close_alias(alias):
            async with self.locks[alias]:
                await self._close_one(alias)
        await asyncio.gather(*(close_alias(alias) for alias in self.accounts), return_exceptions=True)
