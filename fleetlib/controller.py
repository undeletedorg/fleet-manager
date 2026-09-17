"""Inventory validation, SSH transport, reporting, and command-line entry point."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent.parent


PLACEHOLDER = re.compile(r'\$\{([A-Z][A-Z0-9_]*)\}')


def load_env(path=None):
    """Read KEY=value lines from .env. Real environment variables win."""
    values = {}
    path = path or ROOT / '.env'
    try:
        text = path.read_text()
    except OSError:
        return values
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ValueError('.env line %d is not KEY=value' % number)
        key, _, value = line.partition('=')
        key = key.strip()
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
            raise ValueError('.env line %d: invalid key %r' % (number, key))
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        values[key] = value
    return values


def expand(data, values):
    """Substitute ${VAR} in every inventory string, so identifying values such as
    the management domain and SSH user live in .env instead of the inventory."""
    missing = set()

    def resolve(match):
        name = match.group(1)
        if name in os.environ:
            return os.environ[name]
        if name in values:
            return values[name]
        missing.add(name)
        return match.group(0)

    def walk(node):
        if isinstance(node, str):
            return PLACEHOLDER.sub(resolve, node)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        return node

    result = walk(data)
    if missing:
        raise ValueError('undefined in .env or the environment: ' + ', '.join(sorted(missing))
                         + ' (copy .env.example to .env and fill it in)')
    return result


def load_inventory(path, env_path=None):
    data = expand(json.loads(path.read_text()), load_env(env_path))
    def require(condition, message):
        if not condition:
            raise ValueError(message)
    require(isinstance(data, dict), 'inventory must be an object')
    require(data.get('schema_version') == 1, 'inventory schema_version must be 1')
    require(isinstance(data.get('tools'), dict), 'tools must be an object')
    require(isinstance(data.get('hosts'), list) and data['hosts'], 'hosts must be a nonempty list')
    require(isinstance(data.get('path_dirs'), list) and data['path_dirs'], 'path_dirs must be a nonempty list')
    require(all(isinstance(p, str) and p.startswith(('~/', '/')) for p in data['path_dirs']), 'PATH directories must be absolute or start with ~/')
    require(isinstance(data.get('ssh', {}), dict), 'ssh must be an object')
    timeout = data.get('ssh', {}).get('connect_timeout', 8)
    require(type(timeout) is int and 1 <= timeout <= 120, 'SSH connect_timeout must be 1..120')
    names = set()
    def validate_tool(name, spec):
        require(re.fullmatch(r'[a-z][a-z0-9_-]*', name), 'invalid tool name')
        require(isinstance(spec, dict), name + ': tool must be an object')
        require(isinstance(spec.get('installation'), str), name + ': installation note required')
        require(isinstance(spec.get('paths'), list) and bool(spec['paths']), name + ': paths required')
        require(all(isinstance(p, str) and p.startswith(('~/', '/')) for p in spec['paths']), name + ': paths must be absolute or start with ~/')
        for field in ['version_args', 'update_args']:
            value = spec.get(field)
            require((field == 'update_args' and value is None) or (isinstance(value, list) and bool(value) and all(isinstance(v, str) and '\x00' not in v for v in value)), name + ': invalid ' + field)
    for name, spec in data['tools'].items():
        validate_tool(name, spec)
    for host in data['hosts']:
        require(isinstance(host, dict), 'host must be an object')
        for key in ['name', 'hostname', 'user']:
            require(isinstance(host.get(key), str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', host[key]), 'invalid host ' + key)
        require(host['name'] not in names, 'duplicate host name: ' + host['name'])
        names.add(host['name'])
        require(isinstance(host.get('groups'), list) and all(isinstance(g, str) for g in host['groups']), 'invalid groups')
        require(isinstance(host.get('tools'), list) and all(isinstance(t, str) for t in host['tools']) and len(host['tools']) == len(set(host['tools'])) and all(t in data['tools'] for t in host['tools']), 'unknown or duplicate host tool')
        require(isinstance(host.get('tool_overrides', {}), dict), 'tool_overrides must be an object')
        for name, override in host.get('tool_overrides', {}).items():
            require(name in host['tools'], 'override tool must be assigned to host')
            require(isinstance(override, dict), 'tool override must be an object')
            validate_tool(name, dict(data['tools'][name], **override))
    updaters = data.get('service_updaters', {})
    require(isinstance(updaters, dict), 'service_updaters must be an object')
    for name, spec in updaters.items():
        validate_tool(name, spec)
        require(spec.get('update_args'), name + ': service updater requires update_args')
    service_names = set()
    require(isinstance(data.get('services', []), list), 'services must be a list')
    for service in data.get('services', []):
        require(isinstance(service, dict), 'service must be an object')
        require(service.get('host') in names, 'service refers to unknown host')
        require(isinstance(service.get('name'), str) and re.fullmatch(r'[a-z][a-z0-9_-]*', service['name']), 'invalid service name')
        require(service['name'] not in service_names, 'duplicate service name')
        service_names.add(service['name'])
        require(isinstance(service.get('runbook'), str) and service['runbook'].startswith('docs/'), 'service requires a docs/ runbook path')
        require(service.get('kind') in ['compose', 'systemd_user'], 'unsupported service kind')
        if 'updater' in service:
            require(service['updater'] in updaters, service['name'] + ': unknown service updater')
            require(service['kind'] == 'systemd_user', service['name'] + ': only systemd_user services can be updated')
        fields = ['compose_file', 'url'] if service['kind'] == 'compose' else ['unit']
        for key in fields:
            require(isinstance(service.get(key), str) and bool(service[key]) and not service[key].startswith('-'), 'service requires ' + key)
        if service['kind'] == 'compose':
            require(service['compose_file'].startswith('/') and service['url'].startswith(('http://', 'https://')), 'invalid compose file or URL')
    return data


def select_hosts(data, group, names):
    known = {h['name'] for h in data['hosts']}
    if names and set(names) - known:
        raise ValueError('unknown host(s): ' + ', '.join(sorted(set(names) - known)))
    selected = [h for h in data['hosts'] if (group == 'all' or group in h['groups']) and (not names or h['name'] in names)]
    if not selected:
        raise ValueError('no hosts match the selection')
    if names and set(names) - {h['name'] for h in selected}:
        raise ValueError('some requested hosts are outside the selected group; use --group all')
    return selected


def ssh_failure(stderr):
    message = stderr.lower()
    if 'host key verification failed' in message or 'host identification has changed' in message or 'no matching host key' in message:
        return 'host_key_untrusted', 'SSH host key is missing, changed, or unsupported; verify it through a trusted channel.'
    if 'permission denied' in message:
        return 'authentication_failed', 'SSH key authentication failed.'
    return 'unreachable', 'SSH connection failed; check DNS, network access, and SSH configuration.'


def query_host(host, data, action, dry_run, timeout, selected_tools):
    tools = {}
    if action not in ('services', 'service-update'):
        for name in host['tools']:
            if not selected_tools or name in selected_tools:
                tools[name] = dict(data['tools'][name], **host.get('tool_overrides', {}).get(name, {}))
    services = []
    if action in ('services', 'service-update'):
        services = [dict(s) for s in data.get('services', []) if s['host'] == host['name']]
        if action == 'service-update':
            services = [s for s in services if s.get('updater')]
            for spec in services:
                updater = data['service_updaters'][spec['updater']]
                spec['updater_spec'] = dict(updater, name=spec['updater'])
    config = {'path_dirs': data['path_dirs'], 'tools': tools, 'services': services,
              'action': action, 'dry_run': dry_run, 'timeout': timeout}
    source = (ROOT / 'fleetlib' / 'remote.py').read_text()
    # The JSON is a Python string literal on stdin, never interpolated into a shell command.
    source += '\nprint(MARKER + json.dumps(execute(json.loads(' + repr(json.dumps(config)) + '))))\n'
    connect = data.get('ssh', {}).get('connect_timeout', 8)
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ConnectTimeout=' + str(connect), '-o', 'ConnectionAttempts=1',
               '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
               host['user'] + '@' + host['hostname'], 'python3 -']
    per_service = (timeout + 3 * min(timeout, 30) + 5) if action == 'service-update' else (4 * min(timeout, 30) + 5)
    budget = connect + 20 + len(tools) * (timeout + 2 * min(timeout, 30)) + len(services) * per_service
    base = {'name': host['name'], 'hostname': host['hostname'], 'user': host['user'], 'tools': {}, 'services': {}}
    try:
        proc = subprocess.run(command, input=source, text=True, capture_output=True, timeout=budget)
    except subprocess.TimeoutExpired:
        return dict(base, status='transport_timeout', error='SSH session exceeded its time budget; any remote updater may still finish within its command timeout. Recheck before retrying.')
    except OSError:
        return dict(base, status='transport_error', error='Unable to launch the local SSH client.')
    if proc.returncode == 255:
        status, error = ssh_failure(proc.stderr)
        return dict(base, status=status, error=error)
    if proc.returncode:
        return dict(base, status='remote_error', error='Remote worker failed; verify python3 is installed and usable.', exit_code=proc.returncode)
    lines = [line[len('FLEET_RESULT='):] for line in proc.stdout.splitlines() if line.startswith('FLEET_RESULT=')]
    try:
        payload = json.loads(lines[-1])
        if payload['status'] not in {'ok', 'failed', 'busy'} or not isinstance(payload['tools'], dict) or not isinstance(payload['services'], dict):
            raise ValueError('invalid payload')
        return dict(base, **payload)
    except (ValueError, IndexError, KeyError, TypeError):
        return dict(base, status='remote_error', error='Remote worker returned an invalid report.')


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def cell(value):
    return str(value if value is not None else '—').replace('|', '\\|').replace('\n', ' ')


def report_markdown(report):
    lines = ['# Fleet report', '', 'Checked: ' + report['checked_at'], '',
             'Operation: ' + report['action'] + (' (dry run)' if report['dry_run'] else ''), '',
             '| Host | Item | Status | Before | After | Executable |', '| --- | --- | --- | --- | --- | --- |']
    for host in report['hosts']:
        if host['status'] not in {'ok', 'failed'} or not (host['tools'] or host['services']):
            lines.append('| ' + ' | '.join(map(cell, [host['name'], 'SSH/worker', host['status'], None, None, None])) + ' |')
        for name, result in host['tools'].items():
            lines.append('| ' + ' | '.join(map(cell, [host['name'], name, result['status'], result.get('before'), result.get('after'), result.get('path')])) + ' |')
        for name, result in host['services'].items():
            lines.append('| ' + ' | '.join(map(cell, [host['name'], name, result['status'], result.get('before'), result.get('after'), result.get('path')])) + ' |')
        if host.get('error'):
            lines.extend(['', host['name'] + ': ' + cell(host['error']), ''])
    lines.extend(['', 'Installed versions are observations, not a claim that a release is the latest available.',
                  'Detailed exit codes, service checks, and update commands are in the matching JSON report.', ''])
    return '\n'.join(lines)


def render_docs(data, log_dir):
    lines = ['# Devices', '', 'Generated from [inventory.json](inventory.json). Edit the inventory, then run `./fleet render`.', '',
             '| Name | SSH destination | Groups | Expected tools | Services |', '| --- | --- | --- | --- | --- |']
    for h in data['hosts']:
        services = ', '.join('[' + s['name'] + '](' + s['runbook'] + ')' for s in data.get('services', []) if s['host'] == h['name'])
        lines.append('| ' + ' | '.join([cell(h['name']), cell(h['user'] + '@' + h['hostname']), cell(', '.join(h['groups'])), cell(', '.join(h['tools'])), services or '—']) + ' |')
    lines.extend(['', '## Host notes', ''])
    for h in data['hosts']:
        if h.get('notes'):
            lines.append('- **' + h['name'] + ':** ' + h['notes'])
    lines.extend(['', 'See [CLI operations and observations](coding-agents-cli.md) and the [operations guide](README.md).', ''])
    atomic_write(ROOT / 'devices.md', '\n'.join(lines))
    # Preserve latest observation for each tool when checks target only part of the fleet.
    observations = {}
    latest_hosts = {}
    for path in sorted(log_dir.glob('run-*.json')):
        report = json.loads(path.read_text())
        if report['action'] == 'services':
            continue
        for host in report['hosts']:
            latest_hosts[host['name']] = (report['checked_at'], host['status'])
            for name, result in host['tools'].items():
                observations[(host['name'], name)] = (report['checked_at'], result)
    lines = ['# Coding CLI operations', '', 'Generated from [inventory.json](inventory.json) and dated JSON run logs. Run `./fleet check` to refresh observations.', '',
             '## Usage', '', '```sh', './fleet check --group coding', './fleet update --group coding --dry-run', './fleet update --group coding', './fleet service-update --group coding', '```', '',
             '`check` reads installed versions; it does not query release servers or update tools.',
             '`update` invokes the configured self-updaters and verifies the final versions. Git is check-only.', '',
             '## Installation methods', '', '| Tool | Installation | Update arguments | Preferred paths |', '| --- | --- | --- | --- |']
    for name, spec in data['tools'].items():
        lines.append('| ' + ' | '.join(map(cell, [name, spec['installation'], shlex.join([name] + spec['update_args']) if spec.get('update_args') else 'OS maintenance only', ', '.join(spec['paths'])])) + ' |')
    updaters = data.get('service_updaters', {})
    updatable = [s for s in data.get('services', []) if s.get('updater')]
    if updatable:
        lines.extend(['', '## Background service updates', '',
                      'Run `./fleet service-update --group coding` (add `--dry-run` to print the commands first).',
                      'The updater is refused if the remote worker is inside the unit\'s own cgroup, and the',
                      'service must be healthy again afterwards or the update is reported as failed.', '',
                      '| Service | Host | Unit | Update command | Launcher |', '| --- | --- | --- | --- | --- |'])
        for service in updatable:
            spec = updaters[service['updater']]
            command = shlex.join([service['updater']] + spec['update_args'])
            lines.append('| ' + ' | '.join(map(cell, [service['name'], service['host'], service['unit'],
                                                      command, ', '.join(spec['paths'])])) + ' |')
        for name, spec in updaters.items():
            lines.extend(['', '**' + name + ':** ' + cell(spec['installation'])])
    lines.extend(['', '## Latest observations', '', 'Each row retains its own observation time. A failed SSH attempt does not refresh old versions.', '',
                  '| Host | Tool | Observed at (UTC) | Status | Version | Executable |', '| --- | --- | --- | --- | --- | --- |'])
    for host in data['hosts']:
        for name in host['tools']:
            timestamp, result = observations.get((host['name'], name), ('never', {'status': 'not_checked'}))
            lines.append('| ' + ' | '.join(map(cell, [host['name'], name, timestamp, result['status'], result.get('after'), result.get('path')])) + ' |')
    lines.extend(['', '## Latest host attempts', '', '| Host | Attempted at (UTC) | Status |', '| --- | --- | --- |'])
    for host in data['hosts']:
        timestamp, status = latest_hosts.get(host['name'], ('never', 'not_checked'))
        lines.append('| ' + ' | '.join(map(cell, [host['name'], timestamp, status])) + ' |')
    lines.extend(['', 'See [logs](logs/) for before/after versions, commands, and failures; see [CLI troubleshooting](docs/cli-operations.md) for repair guidance.', ''])
    atomic_write(ROOT / 'coding-agents-cli.md', '\n'.join(lines))


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] in {'skills', 'instructions'}:
        from fleetlib.content import main as content_main
        return content_main(arguments)
    parser = argparse.ArgumentParser(description='Check and update the fleet using an explicit inventory and SSH keys.')
    parser.epilog = 'Content management: fleet skills --help or fleet instructions --help'
    parser.add_argument('action', choices=['check', 'update', 'services', 'service-update', 'render', 'validate'])
    parser.add_argument('--inventory', type=Path, default=ROOT / 'inventory.json')
    parser.add_argument('--group', default=None, help='default: coding for tools, all for services')
    parser.add_argument('--host', action='append', dest='hosts', help='inventory short name; repeatable')
    parser.add_argument('--tool', action='append', dest='tools', help='restrict tools; repeatable')
    parser.add_argument('--dry-run', action='store_true', help='discover tools and print update commands without executing them')
    parser.add_argument('--timeout', type=positive, default=180, help='seconds per updater; version checks capped at 30')
    parser.add_argument('--jobs', type=positive, default=3, help='maximum simultaneous host sessions')
    parser.add_argument('--log-dir', type=Path, default=ROOT / 'logs')
    parser.add_argument('--no-render', action='store_true', help='do not regenerate Markdown inventory documents')
    args = parser.parse_args(argv)
    try:
        data = load_inventory(args.inventory)
        if args.dry_run and args.action not in ('update', 'service-update'):
            raise ValueError('--dry-run is only supported for update and service-update')
        if args.tools and args.action not in {'check', 'update'}:
            raise ValueError('--tool is only supported for check and update')
        if args.action == 'validate':
            print('Inventory is valid: %d hosts, %d tools, %d services.' % (len(data['hosts']), len(data['tools']), len(data.get('services', []))))
            return 0
        if args.action == 'render':
            with (ROOT / '.fleet.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                render_docs(data, args.log_dir)
            print('Generated devices.md and coding-agents-cli.md.')
            return 0
        group = args.group or ('all' if args.action in ('services', 'service-update') else 'coding')
        hosts = select_hosts(data, group, args.hosts)
        if args.tools and set(args.tools) - {t for h in hosts for t in h['tools']}:
            raise ValueError('requested tool is not assigned to any selected host')
        if args.action in ('services', 'service-update'):
            services = data.get('services', [])
            if args.action == 'service-update':
                services = [s for s in services if s.get('updater')]
            service_hosts = {s['host'] for s in services}
            hosts = [h for h in hosts if h['name'] in service_hosts]
            if not hosts:
                raise ValueError('no updatable services assigned to selected hosts'
                                 if args.action == 'service-update' else
                                 'no services assigned to selected hosts')
        report = {'schema_version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(timespec='microseconds'),
                  'action': args.action, 'dry_run': args.dry_run, 'hosts': []}
        with ThreadPoolExecutor(max_workers=min(args.jobs, len(hosts))) as pool:
            futures = {pool.submit(query_host, h, data, args.action, args.dry_run, args.timeout, args.tools): h for h in hosts}
            for future in as_completed(futures):
                result = future.result()
                report['hosts'].append(result)
                print(result['name'] + ': ' + result['status'], flush=True)
                for name, item in result['tools'].items():
                    print('  ' + name + ': ' + item['status'] + '  ' + str(item.get('before') or '—') + ' -> ' + str(item.get('after') or '—'), flush=True)
                    if item.get('command') and args.dry_run:
                        print('    ' + shlex.join(item['command']))
                if result.get('error'):
                    print('  ' + result['error'])
                for name, item in result['services'].items():
                    line = '  ' + name + ': ' + item['status']
                    if item.get('before') is not None or item.get('after') is not None:
                        line += '  ' + str(item.get('before') or '—') + ' -> ' + str(item.get('after') or '—')
                    print(line, flush=True)
                    if item.get('command') and args.dry_run:
                        print('    ' + shlex.join(item['command']))
                    if item.get('error'):
                        print('    ' + item['error'])
        ordering = {h['name']: i for i, h in enumerate(hosts)}
        report['hosts'].sort(key=lambda h: ordering[h['name']])
        args.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = report['checked_at'].replace(':', '').replace('+0000', 'Z')
        stem = 'run-' + stamp + '-' + uuid.uuid4().hex[:8]
        # Serialize report publication and regeneration across simultaneous invocations.
        with (ROOT / '.fleet.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            atomic_write(args.log_dir / (stem + '.json'), json.dumps(report, indent=2) + '\n')
            atomic_write(args.log_dir / (stem + '.md'), report_markdown(report))
            if not args.no_render:
                render_docs(data, args.log_dir)
        print('Report: ' + str(args.log_dir / (stem + '.md')))
        return 0 if all(h['status'] == 'ok' for h in report['hosts']) else 1
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print('fleet: ' + str(exc), file=sys.stderr)
        return 2
