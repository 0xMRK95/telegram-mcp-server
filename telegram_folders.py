"""Conservative folder and archive operations; never delete dialogs or messages."""
import asyncio
import copy
import inspect

from telethon import utils
from telethon.tl import types
from telethon.tl.functions.folders import EditPeerFoldersRequest
from telethon.tl.functions.messages import GetDialogFiltersRequest, UpdateDialogFilterRequest

_folder_lock = asyncio.Lock()


def _folder_id(value):
    if type(value) is not int or not 2 <= value <= 2147483647:
        raise ValueError("folder_id must be an integer >= 2 (0 and 1 are reserved)")
    return value


def _title(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 12:
        raise ValueError("title must contain 1–12 characters and not be blank")
    # Older Telethon layers use str; newer layers use TextWithEntities.
    annotation = inspect.signature(types.DialogFilter).parameters['title'].annotation
    return value if annotation in (str, 'str') else types.TextWithEntities(text=value, entities=[])


def _targets(value, *, allow_empty=False):
    if not isinstance(value, list) or (not value and not allow_empty) or len(value) > 100:
        raise ValueError("chats must be a list of 1–100 usernames or marked chat IDs")
    if any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError("each chat must be a nonempty string")
    return value


async def _peers(client, targets):
    peers = {}
    for target in targets:
        target = target.strip()
        entity = await client.get_input_entity(int(target) if target.lstrip('-').isdigit() else target)
        # Marked IDs keep users, basic groups, and channels unambiguous.
        peers[utils.get_peer_id(entity)] = entity
    return peers


async def _filters(client):
    result = await client(GetDialogFiltersRequest())
    return list(getattr(result, 'filters', result))


def _describe(folder):
    title = getattr(folder, 'title', '')
    result = {'id': getattr(folder, 'id', 0), 'title': getattr(title, 'text', title),
              'type': type(folder).__name__, 'editable': type(folder) is types.DialogFilter}
    for field in ('include_peers', 'exclude_peers', 'pinned_peers'):
        result[field] = [utils.get_peer_id(p) for p in getattr(folder, field, [])]
    for field in ('contacts', 'non_contacts', 'groups', 'broadcasts', 'bots',
                  'exclude_muted', 'exclude_read', 'exclude_archived'):
        result[field] = bool(getattr(folder, field, False))
    return result


async def manage_folders(client, name, arguments):
    """Updates preserve all fields except the explicitly requested title/membership."""
    if name == 'list_folders':
        return {'folders': [_describe(f) for f in await _filters(client)]}
    folder_id = _folder_id(arguments.get('folder_id'))
    async with _folder_lock:
        folders = await _filters(client)
        existing = next((f for f in folders if getattr(f, 'id', 0) == folder_id), None)
        if name == 'create_folder':
            if existing is not None:
                raise ValueError('folder_id already exists; use update_folder explicitly')
            title = _title(arguments.get('title'))
            peers = await _peers(client, _targets(arguments.get('chats')))
            folder = types.DialogFilter(id=folder_id, title=title, pinned_peers=[],
                                        include_peers=list(peers.values()), exclude_peers=[])
            before = None
        elif name == 'update_folder':
            if existing is None:
                raise ValueError('folder_id does not exist')
            if type(existing) is not types.DialogFilter:
                raise ValueError('Only ordinary custom folders can be updated; shared/default folders are protected')
            if not any(k in arguments for k in ('title', 'add_chats', 'remove_chats')):
                raise ValueError('Provide title, add_chats, or remove_chats')
            folder = copy.deepcopy(existing)
            before = _describe(existing)
            if 'title' in arguments:
                folder.title = _title(arguments['title'])
            additions = await _peers(client, _targets(arguments.get('add_chats', []), allow_empty=True))
            removals = await _peers(client, _targets(arguments.get('remove_chats', []), allow_empty=True))
            if additions.keys() & removals.keys():
                raise ValueError('A chat cannot be both added and removed')
            excluded = {utils.get_peer_id(p) for p in folder.exclude_peers}
            if additions.keys() & excluded:
                raise ValueError('A requested chat is explicitly excluded; exclusions were preserved')
            included = {utils.get_peer_id(p): p for p in folder.include_peers}
            included.update(additions)
            folder.include_peers = [p for key, p in included.items() if key not in removals]
            folder.pinned_peers = [p for p in folder.pinned_peers if utils.get_peer_id(p) not in removals]
        else:
            raise ValueError('Unknown folder operation')
        result = await client(UpdateDialogFilterRequest(id=folder_id, filter=folder))
        if not result:
            raise RuntimeError('Telegram did not confirm the folder update')
        return {'success': True, 'before': before, 'folder': _describe(folder)}


async def set_archived(client, chats, archived):
    targets = _targets(chats)
    peers = await _peers(client, targets)  # Resolve all before any mutation.
    previous = {}
    async for dialog in client.iter_dialogs():
        key = utils.get_peer_id(dialog.entity)
        if key in peers:
            previous[key] = bool(getattr(dialog, 'archived', False))
    if peers.keys() - previous.keys():
        raise ValueError('Every target must be an existing dialog; no chats were changed')
    changed = {key: peer for key, peer in peers.items() if previous[key] != archived}
    if changed:
        await client(EditPeerFoldersRequest(folder_peers=[
            types.InputFolderPeer(peer=peer, folder_id=1 if archived else 0)
            for peer in changed.values()
        ]))
    return {'success': True, 'archived': archived, 'changed_ids': list(changed),
            'previous': [{'id': key, 'archived': value} for key, value in previous.items()]}


def organization_tools(tool_type):
    folder_id = {"type": "integer", "minimum": 2, "maximum": 2147483647}
    chats = {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 100}
    title = {"type": "string", "minLength": 1, "maxLength": 12}

    def tool(name, description, properties, required=()):
        return tool_type(name=name, description=description, inputSchema={
            "type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False})

    result = [
        tool('list_folders', 'List folders, rules, and marked chat IDs. Read-only.', {}),
        tool('create_folder', 'Create a folder at an explicit unused ID. Never overwrite.',
             {'folder_id': folder_id, 'title': title, 'chats': {**chats, 'minItems': 1}},
             ('folder_id', 'title', 'chats')),
        tool('update_folder', 'Update ordinary folder by ID, preserving rules and exclusions. Remove only explicit inclusions/pins; category rules may still include removed chats.',
             {'folder_id': folder_id, 'title': title, 'add_chats': chats, 'remove_chats': chats}, ('folder_id',)),
    ]
    for name, description in (
        ('archive_chats', 'Archive existing chats reversibly. Never leave, block, or delete.'),
        ('unarchive_chats', 'Return archived chats to the main list. Never delete.'),
    ):
        result.append(tool(name, description, {'chats': {**chats, 'minItems': 1}}, ('chats',)))
    return result
