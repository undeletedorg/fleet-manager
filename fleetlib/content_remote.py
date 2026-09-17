"""Self-contained skills/instructions worker, transported over SSH stdin."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid

BEGIN = '<!-- fleet-management:begin -->'
END = '<!-- fleet-management:end -->'
LIMIT = 16 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_path(home, relative, allow_leaf_link=False):
    """Only allow home-relative targets with no symlink ancestors."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in ('.', '..') for p in parts):
        raise ValueError('unsafe relative path')
    current = home
    for index, part in enumerate(parts):
        current = current / part
        if current.is_symlink() and not (allow_leaf_link and index == len(parts) - 1):
            raise ValueError('symlink in destination path: ' + relative)
    return current


def snapshot(folder):
    """Follow an explicitly selected skill root, never nested links or special files."""
    if not folder.is_dir():
        raise ValueError('skill directory is missing or broken')
    files = {}
    total = 0
    for current, dirs, names in os.walk(folder, followlinks=False):
        for name in dirs:
            if (Path(current) / name).is_symlink():
                raise ValueError('nested symlink in skill')
        for name in sorted(names):
            path = Path(current) / name
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError('symlink or special file in skill')
            total += path.stat().st_size
            if total > LIMIT or len(files) >= 5000:
                raise ValueError('skill exceeds size/file limit')
            files[path.relative_to(folder).as_posix()] = {'sha256': digest(path.read_bytes()), 'executable': bool(path.stat().st_mode & 0o111)}
    return files


def tree_hash(files):
    return digest(json.dumps(files, sort_keys=True, separators=(',', ':')).encode())


def file_hash(path):
    if path.stat().st_size > LIMIT:
        raise ValueError('instruction file exceeds size limit')
    return digest(path.read_bytes())


def split_instruction(raw):
    text = raw.decode('utf-8')
    if BEGIN not in text and END not in text:
        return text, None, ''
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise ValueError('ambiguous instruction markers')
    start, finish = text.index(BEGIN), text.index(END)
    if start > finish:
        raise ValueError('reversed instruction markers')
    finish += len(END)
    return text[:start], text[start:finish], text[finish:]


def instruction_block(text):
    if BEGIN in text or END in text:
        raise ValueError('source contains reserved instruction markers')
    return BEGIN + '\n' + text.rstrip() + '\n' + END


def atomic_bytes(path, raw, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def audit_agent(home, adapter, kind):
    observed = []
    roots = adapter['audit_skills'] if kind == 'skills' else adapter['audit_instructions']
    for root in roots:
        path = home / root
        if kind == 'skills':
            if not path.is_dir():
                continue
            for item in sorted(path.iterdir()):
                if item.name.startswith('.'):
                    continue  # Bundled/system skill sets stay agent-owned.
                if not (item / 'SKILL.md').exists() and not item.is_symlink():
                    continue
                row = {'path': root + '/' + item.name, 'name': item.name, 'symlink': item.is_symlink()}
                try:
                    row['sha256'] = tree_hash(snapshot(item))
                    row['status'] = 'observed'
                except (OSError, ValueError) as exc:
                    row.update(status='unreadable', error=str(exc))
                observed.append(row)
        else:
            files = sorted(path.rglob('*.md')) if path.is_dir() else [path] if path.is_file() else []
            for file in files:
                observed.append({'path': file.relative_to(home).as_posix(), 'sha256': file_hash(file), 'symlink': file.is_symlink(), 'status': 'observed'})
    return observed


def validate_bundle(bundle):
    """Validate decoded paths and hashes before any mutation."""
    total = 0
    for skill in bundle.values():
        files = {}
        if 'SKILL.md' not in skill['files']:
            raise ValueError('SKILL.md is missing from bundle')
        for relative, item in skill['files'].items():
            if not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
                raise ValueError('unsafe bundled file path')
            raw = base64.b64decode(item['content'], validate=True)
            total += len(raw)
            if total > LIMIT:
                raise ValueError('bundle exceeds size limit')
            if digest(raw) != item['sha256']:
                raise ValueError('bundle checksum mismatch')
            files[relative] = {'sha256': item['sha256'], 'executable': item['executable']}
        if tree_hash(files) != skill['sha256']:
            raise ValueError('skill checksum mismatch')


def plan_skill(home, relative, skill, previous, adopt):
    target = safe_path(home, relative, allow_leaf_link=True)
    exists = target.exists() or target.is_symlink()
    actual = tree_hash(snapshot(target)) if exists else None
    wanted = skill['sha256']
    result = {'path': relative, 'before': actual, 'desired': wanted, 'status': 'missing'}
    if exists:
        if previous and not target.is_symlink():
            if actual == wanted:
                result['status'] = 'in_sync'
            elif actual == previous.get('sha256'):
                result['status'] = 'outdated'
            else:
                result['status'] = 'conflict'
                result['error'] = 'managed skill was edited on this host'
        elif adopt:
            result['status'] = 'adopt'
        else:
            result['status'] = 'external_identical' if actual == wanted else 'conflict'
            if result['status'] == 'conflict':
                result['error'] = 'unmanaged skill differs; review before using --adopt'
    return result


def plan_instruction(home, relative, text, previous):
    target = safe_path(home, relative)
    raw = target.read_bytes() if target.exists() else b''
    if len(raw) > LIMIT:
        raise ValueError('instruction file exceeds size limit')
    prefix, block, suffix = split_instruction(raw)
    wanted = instruction_block(text)
    actual = digest(block.encode()) if block else None
    result = {'path': relative, 'before': actual, 'desired': digest(wanted.encode()),
              'file_before': digest(raw), 'status': 'missing'}
    if block:
        if actual == result['desired']:
            result['status'] = 'in_sync' if previous else 'external_identical'
        elif previous and actual == previous.get('sha256'):
            result['status'] = 'outdated'
        else:
            result.update(status='conflict', error='managed instruction block was edited or is not owned by fleet')
    elif previous:
        result.update(status='conflict', error='previously managed instruction block was removed')
    if block:
        new = prefix + wanted + suffix
    else:
        new = prefix + ('\n\n' if prefix and not prefix.endswith('\n\n') else '') + wanted + '\n'
    return result, new.encode()


def execute_content(config, home=None):
    home = Path(home) if home is not None else Path.home()
    kind, action = config['kind'], config['action']
    configured = any(spec['selected_skills'] if kind == 'skills' else spec['instruction_text'].strip() for spec in config['agents'].values())
    mutate = action == 'sync' and not config['dry_run'] and (configured or config.get('prune', False))
    validate_bundle(config.get('bundle', {}))
    state_dir = safe_path(home, '.local/state/fleet-management/content')
    state_file = safe_path(home, '.local/state/fleet-management/content/state.json')
    lock = None
    if mutate:
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = safe_path(home, '.local/state/fleet-management/content/sync.lock')
        lock = lock_path.open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return {'status': 'busy', 'agents': {}, 'error': 'another content sync is running'}
    try:
        state = json.loads(state_file.read_text()) if state_file.exists() else {'schema_version': 1, 'entries': {}}
        if state.get('schema_version') != 1 or not isinstance(state.get('entries'), dict):
            raise ValueError('invalid ownership state')
        result = {'status': 'ok', 'agents': {}}
        plans = []
        for agent, spec in config['agents'].items():
            row = {'status': 'ok', 'observed': audit_agent(home, spec, kind), 'items': [],
                   'configured': bool(spec['selected_skills'] if kind == 'skills' else spec['instruction_text'].strip())}
            result['agents'][agent] = row
            if kind == 'skills':
                for name in spec['selected_skills']:
                    relative = spec['skills_dir'] + '/' + name
                    item = plan_skill(home, relative, config['bundle'][name], state['entries'].get(relative), config.get('adopt', False))
                    item['name'] = name
                    row['items'].append(item)
                    plans.append((item, 'skill', config['bundle'][name]))
            else:
                text = spec['instruction_text']
                if text.strip():
                    relative = spec['instruction_file']
                    item, raw = plan_instruction(home, relative, text, state['entries'].get(relative))
                    override = home / str(Path(relative).with_name('AGENTS.override.md'))
                    if agent == 'codex' and override.is_file() and override.stat().st_size:
                        item.update(status='conflict', error='AGENTS.override.md shadows the managed global instructions')
                    item['name'] = 'global-instructions'
                    row['items'].append(item)
                    plans.append((item, 'instruction', raw))
            selected = {item['path'] for item in row['items']}
            root = spec['skills_dir'] + '/' if kind == 'skills' else spec['instruction_file']
            row['retained_managed'] = [path for path in state['entries'] if (path.startswith(root) if kind == 'skills' else path == root) and path not in selected]
            if row['retained_managed'] and config.get('prune'):
                for relative in row['retained_managed']:
                    target = safe_path(home, relative)
                    item = {'name': Path(relative).name, 'path': relative, 'desired': None, 'status': 'remove'}
                    if kind == 'skills':
                        actual = tree_hash(snapshot(target)) if target.exists() else None
                        payload = None
                    else:
                        raw = target.read_bytes() if target.exists() else b''
                        prefix, block, suffix = split_instruction(raw)
                        actual = digest(block.encode()) if block else None
                        payload = (prefix + suffix).encode()
                        item['file_before'] = digest(raw)
                    item['before'] = actual
                    if actual is not None and actual != state['entries'][relative]['sha256']:
                        item.update(status='conflict', error='locally edited content will not be pruned')
                    row['items'].append(item)
                    plans.append((item, 'remove-' + kind, payload))
                row['retained_managed'] = []
            if row['retained_managed'] or any(item['status'] == 'unreadable' for item in row['observed']):
                row['status'] = 'drift'
        # Preflight all selected agents on this host before the first content write.
        conflicts = any(item['status'] == 'conflict' for item, _, _ in plans)
        run_id = uuid.uuid4().hex
        backup_root = safe_path(home, '.local/state/fleet-management/content/backups/' + run_id)
        for item, item_kind, payload in plans:
            if mutate and not conflicts and item['status'] in {'missing', 'outdated', 'adopt', 'remove'}:
                target = safe_path(home, item['path'], allow_leaf_link=item_kind == 'skill')
                # Detect changes since the preflight, including edits outside instruction blocks.
                if item_kind in {'skill', 'remove-skills'}:
                    actual = tree_hash(snapshot(target)) if target.exists() or target.is_symlink() else None
                else:
                    actual = digest(target.read_bytes()) if target.exists() else digest(b'')
                expected = item['before'] if item_kind in {'skill', 'remove-skills'} else item['file_before']
                if actual != expected:
                    item.update(status='conflict', error='destination changed during sync')
                    conflicts = True
                    continue
                backup = backup_root / item['path']
                target.parent.mkdir(parents=True, exist_ok=True)
                if item_kind.startswith('remove-'):
                    if target.exists():
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        if item_kind == 'remove-skills':
                            target.rename(backup)
                        else:
                            mode = stat.S_IMODE(target.stat().st_mode)
                            atomic_bytes(backup, target.read_bytes(), mode)
                            atomic_bytes(target, payload, mode)
                        item['backup'] = str(backup.relative_to(home))
                    state['entries'].pop(item['path'])
                    atomic_bytes(state_file, (json.dumps(state, indent=2) + '\n').encode())
                    item.update(status='removed', after=None)
                    continue
                if item_kind == 'skill':
                    stage = Path(tempfile.mkdtemp(prefix='.fleet-stage-', dir=target.parent))
                    try:
                        for relative, data in payload['files'].items():
                            file = stage / relative
                            file.parent.mkdir(parents=True, exist_ok=True)
                            file.write_bytes(base64.b64decode(data['content']))
                            file.chmod(0o755 if data['executable'] else 0o644)
                        if tree_hash(snapshot(stage)) != item['desired']:
                            raise ValueError('staging verification failed')
                        if target.exists() or target.is_symlink():
                            backup.parent.mkdir(parents=True, exist_ok=True)
                            target.rename(backup)  # Move a symlink itself, never its target.
                            item['backup'] = str(backup.relative_to(home))
                        try:
                            stage.rename(target)
                        except OSError:
                            if backup.exists() or backup.is_symlink():
                                backup.rename(target)
                            raise
                    finally:
                        if stage.exists():
                            shutil.rmtree(stage)
                    verified = tree_hash(snapshot(target))
                else:
                    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
                    if target.exists():
                        atomic_bytes(backup, target.read_bytes(), mode)
                        item['backup'] = str(backup.relative_to(home))
                    atomic_bytes(target, payload, mode)
                    _, block, _ = split_instruction(target.read_bytes())
                    verified = digest(block.encode())
                if verified != item['desired']:
                    raise ValueError('post-write verification failed')
                state['entries'][item['path']] = {'sha256': verified, 'kind': item_kind}
                atomic_bytes(state_file, (json.dumps(state, indent=2) + '\n').encode())
                item.update(status='synced', after=verified)
            elif action == 'sync' and config['dry_run'] and item['status'] in {'missing', 'outdated', 'adopt', 'remove'}:
                item['operation'] = item['status']
                item['status'] = 'planned'
        for row in result['agents'].values():
            statuses = {item['status'] for item in row['items']}
            if 'conflict' in statuses:
                row['status'] = 'conflict'
            elif statuses - {'in_sync', 'external_identical', 'synced', 'planned', 'removed'}:
                row['status'] = 'drift'
            if row['status'] != 'ok':
                result['status'] = 'failed'
        if conflicts:
            result['error'] = 'conflicts detected; remaining writes on this host were skipped'
        return result
    finally:
        if lock:
            lock.close()
