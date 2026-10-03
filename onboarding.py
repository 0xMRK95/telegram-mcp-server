#!/usr/bin/env python3
"""User-run, local interactive account enrollment. Never run through an agent/chat."""
import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile
import warnings

from telegram_accounts import AccountError, SessionLock, _account, private_file


DEFAULT_CONFIG = Path.home() / '.config' / 'telegram-mcp' / 'accounts.json'
DEFAULT_SESSIONS = Path.home() / '.local' / 'share' / 'telegram-mcp'


def secret(prompt, *, preserve_whitespace=False):
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise AccountError('Onboarding needs a private interactive terminal; do not pipe secrets or use chat')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            value = getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise AccountError('Hidden input is unavailable; stopping without reading a secret') from None
    if not value if preserve_whitespace else not value.strip():
        raise AccountError('A required value was empty')
    return value if preserve_whitespace else value.strip()


def read_config(path):
    if not path.exists():
        return {'accounts': {}}
    private_file(path)
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get('accounts'), dict):
            raise ValueError()
        return data
    except (ValueError, OSError):
        raise AccountError('Existing account configuration is invalid; it was not changed') from None


def write_config(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.accounts-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


async def enroll(alias, config, sessions):
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise AccountError('Run onboarding yourself in a private interactive terminal')
    config, sessions = config.expanduser().resolve(), sessions.expanduser().resolve()
    # Validate alias before deriving a path from it.
    _account(alias, {'api_id': 1, 'api_hash': 'validation-only', 'session': 'validation-only'}, sessions)
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'posix':
        private_file(config.parent)
        private_file(sessions)
    config_lock = SessionLock(config)
    config_lock.acquire()
    client, session_lock = None, None
    confirmed = False
    try:
        data = read_config(config)
        if alias in data['accounts']:
            raise AccountError('Alias already exists; choose a new name. Existing accounts are never replaced')
        session = sessions / (alias + '.session')
        if session.exists():
            raise AccountError('Session path already exists; choose a fresh alias or inspect the old session yourself')
        api_id_text = secret('Telegram API ID (hidden): ')
        if not api_id_text.isdecimal() or int(api_id_text) <= 0:
            raise AccountError('API ID must be a positive integer')
        api_hash = secret('Telegram API hash (hidden): ')
        phone = secret('Phone number with country code (hidden): ')
        session_lock = SessionLock(session)
        session_lock.acquire()
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
        client = TelegramClient(str(session), int(api_id_text), api_hash)
        await client.connect()
        sent = await client.send_code_request(phone)
        code = secret('Telegram login code (hidden): ')
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            await client.sign_in(password=secret('Telegram two-step password (hidden): ', preserve_whitespace=True))
        me = await client.get_me()
        if not me:
            raise AccountError('Login did not return an account identity')
        print(f'Alias {alias!r} will refer to Telegram user ID {me.id}, username @{me.username or "(none)"}.')
        if input('Save this account pairing? Type yes: ').strip() != 'yes':
            raise AccountError('Account pairing was not saved')
        private_file(session)
        data['accounts'][alias] = {'api_id': int(api_id_text), 'api_hash': api_hash,
                                   'session': str(session), 'expected_user_id': me.id}
        write_config(config, data)
        confirmed = True
        print('Saved privately. Set TG_ACCOUNTS_FILE to your accounts.json path in the MCP launcher.')
        print('Do not paste the configuration or session contents into chat. Restart MCP to load the alias.')
    finally:
        try:
            if client is not None:
                if not confirmed:
                    # Revoke this newly-created login rather than leave an untracked session.
                    try:
                        await client.log_out()
                    except Exception:
                        print('Enrollment was interrupted. Check Telegram Settings > Devices for an unfinished login.', file=sys.stderr)
                await client.disconnect()
        finally:
            if session_lock:
                session_lock.release()
            config_lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('alias', help='Non-secret nickname such as personal or work')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--sessions-dir', type=Path, default=DEFAULT_SESSIONS)
    args = parser.parse_args()
    if os.name == 'posix':
        os.umask(0o077)
    try:
        asyncio.run(enroll(args.alias, args.config, args.sessions_dir))
    except (KeyboardInterrupt, EOFError):
        print('Onboarding cancelled. No secrets were printed.', file=sys.stderr)
        return 1
    except AccountError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        # Never stringify Telegram errors: they may contain phone numbers or requests.
        print(f'Onboarding stopped ({type(error).__name__}). Check Telegram Settings > Devices before retrying.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
