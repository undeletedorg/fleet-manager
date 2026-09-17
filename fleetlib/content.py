"""Workspace content catalogue, preview/sync transport, and dated drift reports."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from fleetlib import controller
from fleetlib.content_remote import BEGIN, END, LIMIT, digest, safe_path, snapshot, tree_hash

AGENTS = {'codex', 'claude', 'grok', 'opencode'}
NAME = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')


def relative_path(value):
    return isinstance(value, str) and bool(value) and not Path(value).is_absolute() and all(p not in ('', '.', '..') for p in value.split('/')) and '\x00' not in value


def source_file(root, relative):
    if not relative_path(relative):
        raise ValueError('invalid source path: ' + str(relative))
    path = root / relative
    if root.resolve() not in path.resolve().parents:
        raise ValueError('source escapes the catalogue: ' + relative)
    return path


def skill_metadata(path, expected_name):
    """Validate portable required fields; preserve the original YAML unchanged."""
    text = path.read_text(encoding='utf-8')
    lines = text.splitlines()
    if not lines or lines[0] != '---' or '---' not in lines[1:]:
        raise ValueError(str(path) + ': missing YAML frontmatter')
    header = '\n'.join(lines[1:lines.index('---', 1)])
    names = re.findall(r'^name:[ \t]*(.*?)[ \t]*$', header, re.M)
    descriptions = re.findall(r'^description:[ \t]*(.*?)[ \t]*$', header, re.M)
    if len(names) != 1 or names[0].strip('\'"') != expected_name or len(descriptions) != 1 or not descriptions[0]:
        raise ValueError(str(path) + ': requires matching name and nonempty description')
    description = descriptions[0]
    if description in ('|', '>', '|-', '>-', '|+', '>+'):
        after = header.split('description:', 1)[1].splitlines()[1:]
        parts = []
        for line in after:
            if line and not line.startswith((' ', '\t')):
                break
            parts.append(line.strip())
        description = ' '.join(parts).strip()
    if description.strip('\'"') in ('', 'null', '~') or len(description.strip('\'"')) > 1024:
        raise ValueError(str(path) + ': description must be 1..1024 characters')
    warnings = []
    if re.search(r'^(disable-model-invocation|allowed-tools|context|agent|model|user-invocable):', header, re.M):
        warnings.append('Agent-specific frontmatter is preserved; invocation and tool restrictions are not portable across all agents.')
    return warnings


def load_manifest(path):
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('content manifest schema_version must be 1')
    if not isinstance(data.get('skills'), dict) or not isinstance(data.get('agents'), dict) or set(data['agents']) != AGENTS:
        raise ValueError('manifest requires a skills object and the four supported agents')
    instructions = data.get('instructions')
    if not isinstance(instructions, dict) or not isinstance(instructions.get('common'), list) or not all(isinstance(p, str) for p in instructions['common']):
        raise ValueError('instructions.common must be a list of source paths')
    for name, skill in data['skills'].items():
        if not NAME.fullmatch(name) or len(name) > 64 or not isinstance(skill, dict) or not relative_path(skill.get('source')):
            raise ValueError('invalid skill entry: ' + name)
        folder = source_file(path.parent, skill['source'])
        skill_metadata(folder / 'SKILL.md', name)
    destinations = set()
    for agent, spec in data['agents'].items():
        if not isinstance(spec, dict):
            raise ValueError('invalid agent configuration: ' + agent)
        for key in ('skills', 'instructions', 'audit_skills', 'audit_instructions'):
            if not isinstance(spec.get(key), list) or not all(isinstance(v, str) for v in spec[key]) or len(set(spec[key])) != len(spec[key]):
                raise ValueError(agent + ': ' + key + ' must be a list of unique strings')
        if any(name not in data['skills'] for name in spec['skills']):
            raise ValueError(agent + ': skill assignment is missing from catalogue')
        for key in ('skills_dir', 'instruction_file'):
            if not relative_path(spec.get(key)):
                raise ValueError(agent + ': invalid ' + key)
            if spec[key] in destinations:
                raise ValueError('agents cannot own the same destination; use native agent paths')
            destinations.add(spec[key])
        if any(not relative_path(p) for p in spec['audit_skills'] + spec['audit_instructions']):
            raise ValueError(agent + ': invalid audit path')
        for relative in instructions['common'] + spec['instructions']:
            file = source_file(path.parent, relative)
            raw = file.read_bytes()
            if len(raw) > LIMIT:
                raise ValueError('instruction source exceeds size limit')
            text = raw.decode('utf-8')
            if BEGIN in text or END in text:
                raise ValueError('source contains reserved fleet markers')
    return data


def make_bundle(manifest, root, agents, kind):
    bundle = {}
    total = 0
    if kind == 'instructions':
        return bundle
    names = {name for agent in agents for name in manifest['agents'][agent]['skills']}
    for name in sorted(names):
        path = source_file(root, manifest['skills'][name]['source'])
        files = snapshot(path)
        content = {}
        for relative, metadata in files.items():
            raw = (path / relative).read_bytes()
            total += len(raw)
            if total > LIMIT:
                raise ValueError('selected catalogue exceeds 16 MiB; sync smaller agent/skill collections')
            if digest(raw) != metadata['sha256']:
                raise ValueError('source changed while preparing bundle; retry')
            content[relative] = dict(metadata, content=base64.b64encode(raw).decode('ascii'))
        bundle[name] = {'sha256': tree_hash(files), 'files': content,
                        'warnings': skill_metadata(path / 'SKILL.md', name)}
    return bundle


def agent_specs(manifest, root, selected):
    specs = {}
    for agent in selected:
        spec = dict(manifest['agents'][agent])
        spec['selected_skills'] = spec.pop('skills')
        paths = manifest['instructions']['common'] + spec['instructions']
        spec['instruction_text'] = '\n\n'.join(source_file(root, p).read_text().strip() for p in paths if source_file(root, p).read_text().strip())
        specs[agent] = spec
    return specs


def query_content(host, data, config, timeout):
    source = (controller.ROOT / 'fleetlib' / 'content_remote.py').read_text()
    source += '\nprint("FLEET_CONTENT=" + json.dumps(execute_content(json.loads(' + repr(json.dumps(config)) + '))))\n'
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ConnectTimeout=' + str(data.get('ssh', {}).get('connect_timeout', 8)),
               '-o', 'ConnectionAttempts=1', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
               host['user'] + '@' + host['hostname'], 'python3 -']
    base = {'name': host['name'], 'hostname': host['hostname'], 'status': 'remote_error', 'agents': {}}
    try:
        proc = subprocess.run(command, input=source, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return dict(base, status='transport_timeout', error='SSH timed out; recheck before retrying because the remote sync may still complete.')
    except OSError:
        return dict(base, status='transport_error', error='Could not launch SSH.')
    if proc.returncode == 255:
        status, error = controller.ssh_failure(proc.stderr)
        return dict(base, status=status, error=error)
    if proc.returncode:
        return dict(base, error='Remote content worker failed; check file permissions and Python availability. Any completed files retain ownership state and backups.')
    try:
        lines = [line[len('FLEET_CONTENT='):] for line in proc.stdout.splitlines() if line.startswith('FLEET_CONTENT=')]
        result = json.loads(lines[-1])
        if result['status'] not in {'ok', 'failed', 'busy'} or not isinstance(result['agents'], dict):
            raise ValueError('invalid response')
        return dict(base, **result)
    except (ValueError, IndexError, KeyError, TypeError):
        return dict(base, error='Invalid remote content report.')


def markdown(report):
    lines = ['# Fleet ' + report['kind'] + ' report', '', 'Checked: ' + report['checked_at'], '',
             'Operation: ' + report['operation'] + (' (dry run)' if report['dry_run'] else ''), '',
             '| Host | Agent | Status | Assigned items | Observed entries |', '| --- | --- | --- | --- | --- |']
    notes = []
    for host in report['hosts']:
        if not host['agents']:
            lines.append('| ' + ' | '.join(map(controller.cell, [host['name'], 'SSH/worker', host['status'], 0, 0])) + ' |')
        if host.get('error'):
            notes.append(host['name'] + ': ' + controller.cell(host['error']))
        for agent, row in host['agents'].items():
            lines.append('| ' + ' | '.join(map(controller.cell, [host['name'], agent, row['status'], len(row['items']), len(row['observed'])])) + ' |')
            if row.get('retained_managed'):
                notes.append(host['name'] + '/' + agent + ': retained managed paths: ' + ', '.join(row['retained_managed']))
    lines.extend(['', '## Assigned content', '', '| Host | Agent | Item | Status | Destination |', '| --- | --- | --- | --- | --- |'])
    for host in report['hosts']:
        for agent, row in host['agents'].items():
            for item in row['items']:
                lines.append('| ' + ' | '.join(map(controller.cell, [host['name'], agent, item['name'], item['status'], item['path']])) + ' |')
                if item.get('error'):
                    notes.append(host['name'] + '/' + agent + '/' + item['name'] + ': ' + controller.cell(item['error']))
    lines.extend(['', '## Observed content', '', '| Host | Agent | Path | SHA-256 prefix | Symlink |', '| --- | --- | --- | --- | --- |'])
    for host in report['hosts']:
        for agent, row in host['agents'].items():
            for item in row['observed']:
                lines.append('| ' + ' | '.join(map(controller.cell, [host['name'], agent, item['path'], item.get('sha256', item['status'])[:12], item['symlink']])) + ' |')
    lines.extend(['', 'The matching JSON includes complete skill-tree hashes, conflicts, and backup paths.',
                  'An empty assignment is an audit only. Observed entries include compatibility roots and are not necessarily unique skills or enabled skills.',
                  'File synchronization does not verify model behavior, plugin dependencies, or instruction precedence in a running session.', ''])
    for name, warnings in report.get('warnings', {}).items():
        notes.extend(name + ': ' + warning for warning in warnings)
    if notes:
        lines.extend(['## Notes', ''] + ['- ' + note for note in notes] + [''])
    return '\n'.join(lines)


def import_source(args, manifest, path, kind):
    if args.source is None:
        raise ValueError('import requires --source PATH (a local skill folder or Markdown instruction file)')
    source = args.source.expanduser().resolve()
    name = args.name or (source.name if kind == 'skills' else source.stem)
    if not NAME.fullmatch(name) or len(name) > 64:
        raise ValueError('name must be lowercase alphanumeric words separated by single hyphens')
    if kind == 'skills':
        skill_metadata(source / 'SKILL.md', name)
        snapshot(source)
        destination = safe_path(path.parent.resolve(), 'skills/' + name)
        if name in manifest['skills'] or destination.exists() or destination.is_symlink():
            raise ValueError('skill already exists; edit the workspace source to update it')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        manifest['skills'][name] = {'source': 'skills/' + name}
    else:
        raw = source.read_bytes()
        text = raw.decode('utf-8')
        if len(raw) > LIMIT or BEGIN in text or END in text:
            raise ValueError('instruction source is too large or contains reserved fleet markers')
        destination = safe_path(path.parent.resolve(), 'instructions/' + name + '.md')
        if destination.exists() or destination.is_symlink():
            raise ValueError('instruction source already exists; edit it in the workspace')
        controller.atomic_write(destination, text)
    # Import is deliberately separate from assignment and deployment.
    controller.atomic_write(path, json.dumps(manifest, indent=2) + '\n')
    print('Imported ' + str(destination) + '. Assign it in ' + str(path) + ' before syncing.')


def main(argv):
    parser = argparse.ArgumentParser(description='Audit and synchronize user skills or global instruction blocks.')
    parser.add_argument('kind', choices=['skills', 'instructions'])
    parser.add_argument('operation', choices=['check', 'sync', 'import', 'validate'])
    parser.add_argument('--manifest', type=Path, default=controller.ROOT / 'agent-content' / 'manifest.json')
    parser.add_argument('--inventory', type=Path, default=controller.ROOT / 'inventory.json')
    parser.add_argument('--group', default='coding')
    parser.add_argument('--host', action='append', dest='hosts')
    parser.add_argument('--agent', action='append', choices=sorted(AGENTS), dest='agents')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--adopt', action='store_true', help='skills sync only: back up unmanaged destinations and replace them with managed copies')
    parser.add_argument('--prune', action='store_true', help='sync only: back up and remove unchanged fleet-owned content no longer assigned')
    parser.add_argument('--source', type=Path, help='local import source; not read during check/sync')
    parser.add_argument('--name', help='import catalogue name (skills must match their frontmatter name)')
    parser.add_argument('--timeout', type=controller.positive, default=180)
    parser.add_argument('--jobs', type=controller.positive, default=3)
    parser.add_argument('--log-dir', type=Path, default=controller.ROOT / 'logs')
    args = parser.parse_args(argv)
    try:
        if args.dry_run and args.operation != 'sync':
            raise ValueError('--dry-run requires sync')
        if args.adopt and (args.kind != 'skills' or args.operation != 'sync'):
            raise ValueError('--adopt requires skills sync')
        if args.prune and args.operation != 'sync':
            raise ValueError('--prune requires sync')
        if (args.source or args.name) and args.operation != 'import':
            raise ValueError('--source and --name require import')
        manifest = load_manifest(args.manifest)
        if args.operation == 'import':
            with (controller.ROOT / '.fleet.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                manifest = load_manifest(args.manifest)
                import_source(args, manifest, args.manifest, args.kind)
            return 0
        selected = list(dict.fromkeys(args.agents or manifest['agents']))
        bundle = make_bundle(manifest, args.manifest.parent, selected, args.kind)
        specs = agent_specs(manifest, args.manifest.parent, selected)
        if args.operation == 'validate':
            print('Valid ' + args.kind + ' catalogue; ' + str(len(manifest['skills'])) + ' skills registered.')
            return 0
        data = controller.load_inventory(args.inventory)
        hosts = controller.select_hosts(data, args.group, args.hosts)
        for host in hosts:
            if any(agent not in host['tools'] for agent in selected):
                raise ValueError(host['name'] + ': selected agent is not assigned to this host in inventory')
        config = {'kind': args.kind, 'action': args.operation, 'dry_run': args.dry_run,
                  'adopt': args.adopt, 'prune': args.prune, 'agents': specs, 'bundle': bundle}
        report = {'schema_version': 1, 'action': 'content', 'kind': args.kind, 'operation': args.operation,
                  'dry_run': args.dry_run, 'checked_at': datetime.now(timezone.utc).isoformat(timespec='microseconds'),
                  'source_hashes': {name: value['sha256'] for name, value in bundle.items()},
                  'warnings': {name: value['warnings'] for name, value in bundle.items() if value['warnings']}, 'hosts': []}
        for name, warnings in report['warnings'].items():
            for warning in warnings:
                print(name + ': ' + warning)
        with ThreadPoolExecutor(max_workers=min(args.jobs, len(hosts))) as pool:
            futures = [pool.submit(query_content, host, data, config, args.timeout) for host in hosts]
            for future in as_completed(futures):
                result = future.result()
                report['hosts'].append(result)
                print(result['name'] + ': ' + result['status'], flush=True)
                for agent, row in result['agents'].items():
                    print('  %s: %s (%d assigned, %d observed entries)' % (agent, row['status'], len(row['items']), len(row['observed'])), flush=True)
                    for item in row['items']:
                        print('    ' + item['name'] + ': ' + item['status'])
                if result.get('error'):
                    print('  ' + result['error'])
        order = {host['name']: i for i, host in enumerate(hosts)}
        report['hosts'].sort(key=lambda host: order[host['name']])
        stem = 'content-' + report['checked_at'].replace(':', '').replace('+0000', 'Z') + '-' + uuid.uuid4().hex[:8]
        with (controller.ROOT / '.fleet.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            controller.atomic_write(args.log_dir / (stem + '.json'), json.dumps(report, indent=2) + '\n')
            controller.atomic_write(args.log_dir / (stem + '.md'), markdown(report))
        print('Report: ' + str(args.log_dir / (stem + '.md')))
        return 0 if all(host['status'] == 'ok' for host in report['hosts']) else 1
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print('fleet: ' + str(exc), file=sys.stderr)
        return 2
