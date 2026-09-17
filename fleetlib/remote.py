"""Self-contained worker sent to python3 over SSH stdin; never imports local code."""
import fcntl
import glob
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

MARKER = 'FLEET_RESULT='
# Optional leading 'v' (t3 prints 't3 v0.0.41-nightly...'); the lookbehind still
# rejects a match inside a longer token, so 'nodev20.1.0' stays unparsed.
VERSION = re.compile(r'(?<![\w.])v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)')


def environment(path_dirs):
    env = os.environ.copy()
    directories = []
    for entry in path_dirs:
        expanded = os.path.expanduser(entry)
        # Highest numeric Node version first when NVM installs coexist.
        matches = sorted(glob.glob(expanded), key=lambda s: tuple(int(n) for n in re.findall(r'\d+', s)), reverse=True)
        directories.extend(matches if '*' in expanded else [expanded])
    env['PATH'] = os.pathsep.join(dict.fromkeys(directories))
    env.update(NO_COLOR='1', TERM='dumb', XDG_RUNTIME_DIR='/run/user/' + str(os.getuid()))
    return env


def discover(name, spec, env):
    candidates = [os.path.expanduser(p) for p in spec['paths']]
    fallback = shutil.which(name, path=env['PATH'])
    if fallback:
        candidates.append(fallback)
    return next((p for p in candidates if os.path.isfile(p) and os.access(p, os.X_OK)), None)


def run(argv, env, timeout):
    """Bound runtime and output memory; kill the whole subprocess group on timeout."""
    started = time.monotonic()
    with tempfile.TemporaryFile() as output:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.STDOUT, env=env, start_new_session=True)
        except OSError as exc:
            return {'exit_code': None, 'error': 'exec_failed', 'errno': exc.errno, 'output': ''}
        error = None
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            error = 'timeout'
        output.seek(0, 2)
        output.seek(max(0, output.tell() - 32768))
        result = {'exit_code': proc.returncode, 'output': output.read().decode('utf-8', 'replace'),
                  'elapsed_seconds': round(time.monotonic() - started, 2)}
        if error:
            result['error'] = error
        return result


def version(exe, spec, env, timeout):
    result = run([exe] + spec['version_args'], env, timeout)
    found = VERSION.search(result['output'])
    if result.get('error') or result['exit_code'] != 0 or not found:
        return None, result.get('error', 'version_check_failed')
    return found.group(1), None


def tool_result(name, spec, env, action, dry_run, timeout):
    exe = discover(name, spec, env)
    result = {'status': 'missing', 'path': exe, 'before': None, 'after': None}
    if not exe:
        return result
    result['resolved_path'] = os.path.realpath(exe)
    before, error = version(exe, spec, env, min(timeout, 30))
    result.update(before=before, after=before)
    if error:
        result.update(status='check_failed', error=error)
        return result
    if action == 'check':
        result['status'] = 'ok'
        return result
    if not spec.get('update_args'):
        result['status'] = 'check_only'
        return result
    argv = [exe] + spec['update_args']
    result['command'] = argv
    if dry_run:
        result['status'] = 'planned'
        return result
    update = run(argv, env, timeout)
    # Never persist updater output: it can contain credentials or private config.
    result['update_exit_code'] = update['exit_code']
    result['elapsed_seconds'] = update.get('elapsed_seconds')
    after_exe = discover(name, spec, env)
    after, after_error = version(after_exe, spec, env, min(timeout, 30)) if after_exe else (None, 'missing_after_update')
    result.update(after=after, path=after_exe,
                  resolved_path=os.path.realpath(after_exe) if after_exe else None)
    if update.get('error') or update['exit_code'] != 0:
        result.update(status='update_failed', error=update.get('error', 'nonzero_exit'))
    elif after_error:
        result.update(status='verification_failed', error=after_error)
    else:
        result['status'] = 'updated' if before != after else 'unchanged'
    return result


def service_result(spec, env, timeout):
    if spec['kind'] == 'systemd_user':
        result = run(['systemctl', '--user', 'show', spec['unit'],
                      '--property=LoadState,ActiveState,SubState,UnitFileState'], env, timeout)
        fields = dict(line.split('=', 1) for line in result['output'].splitlines() if '=' in line)
        allowed = {k: fields.get(k) for k in ['LoadState', 'ActiveState', 'SubState', 'UnitFileState']}
        ok = result['exit_code'] == 0 and fields.get('ActiveState') == 'active' and fields.get('SubState') == 'running' and fields.get('UnitFileState') == 'enabled'
        linger = run(['loginctl', 'show-user', str(os.getuid()), '-p', 'Linger', '--value'], env, timeout)
        allowed['linger'] = linger['output'].strip() if linger['exit_code'] == 0 else 'unknown'
        ok = ok and allowed['linger'] == 'yes'
        return {'status': 'ok' if ok else 'unhealthy', 'details': allowed}
    prefix = ['docker', 'compose', '-f', spec['compose_file']]
    expected = run(prefix + ['config', '--services'], env, timeout)
    containers = run(prefix + ['ps', '--all', '--format', 'json'], env, timeout)
    if expected['exit_code'] != 0 or containers['exit_code'] != 0:
        return {'status': 'check_failed', 'error': expected.get('error') or containers.get('error') or 'compose_command_failed'}
    try:
        raw = containers['output'].strip()
        rows = json.loads(raw) if raw.startswith('[') else [json.loads(line) for line in raw.splitlines()]
        states = [{'service': row['Service'], 'state': row['State'], 'health': row.get('Health', '')} for row in rows]
    except (ValueError, KeyError, TypeError):
        return {'status': 'check_failed', 'error': 'invalid_compose_output'}
    names = set(expected['output'].splitlines())
    running = {r['service'] for r in states if r['state'] == 'running' and r['health'] in ('', 'healthy')}
    http = run(['curl', '--noproxy', '*', '--max-time', str(timeout), '-s', '-o', '/dev/null', '-w', '%{http_code}', spec['url']], env, timeout + 1)
    status = http['output'].strip()
    ok = bool(names) and names <= running and all(r['state'] == 'running' and r['health'] in ('', 'healthy') for r in states) and http['exit_code'] == 0 and status == '200'
    return {'status': 'ok' if ok else 'unhealthy', 'containers': states,
            'missing_or_unhealthy_services': sorted(names - running), 'http_status': status if status.isdigit() else None}


def cgroup_conflict(unit):
    """True when this worker runs inside the unit's own cgroup.

    t3code.service and friends use KillMode=mixed, so stopping the unit SIGKILLs
    every process in its cgroup. An updater started from in there kills itself
    partway through and leaves the service down. Refuse instead.
    """
    try:
        with open('/proc/self/cgroup') as handle:
            return unit in handle.read()
    except OSError:
        return False


def service_update_result(spec, updater, env, dry_run, timeout):
    name = updater['name']
    unit = spec.get('unit')
    if spec['kind'] != 'systemd_user':
        return {'status': 'unsupported', 'error': 'only systemd_user services can be updated'}
    if unit and cgroup_conflict(unit):
        return {'status': 'unsafe_cgroup', 'before': None, 'after': None,
                'error': 'worker is inside ' + unit + '; the update would kill itself'}
    exe = discover(name, updater, env)
    result = {'status': 'missing', 'path': exe, 'before': None, 'after': None}
    if not exe:
        return result
    result['resolved_path'] = os.path.realpath(exe)
    before, error = version(exe, updater, env, min(timeout, 30))
    result.update(before=before, after=before)
    if error:
        result.update(status='check_failed', error=error)
        return result
    argv = [exe] + updater['update_args']
    result['command'] = argv
    if dry_run:
        result['status'] = 'planned'
        return result
    update = run(argv, env, timeout)
    # Never persist updater output: it can contain account or relay details.
    result['update_exit_code'] = update['exit_code']
    result['elapsed_seconds'] = update.get('elapsed_seconds')
    after_exe = discover(name, updater, env)
    after, after_error = version(after_exe, updater, env, min(timeout, 30)) if after_exe else (None, 'missing_after_update')
    result.update(after=after, path=after_exe,
                  resolved_path=os.path.realpath(after_exe) if after_exe else None)
    if update.get('error') or update['exit_code'] != 0:
        result.update(status='update_failed', error=update.get('error', 'nonzero_exit'))
        return result
    if after_error:
        result.update(status='verification_failed', error=after_error)
        return result
    # An updater that exits 0 but leaves the unit down is still a failed update.
    health = service_result(spec, env, min(timeout, 30))
    result['health'] = health.get('details')
    if health['status'] != 'ok':
        result.update(status='unhealthy_after_update', error='service is not healthy after the update')
        return result
    result['status'] = 'updated' if before != after else 'unchanged'
    return result


def execute(config):
    env = environment(config['path_dirs'])
    result = {'status': 'ok', 'tools': {}, 'services': {}}
    lock = None
    if config['action'] in ('update', 'service-update') and not config['dry_run']:
        directory = Path.home() / '.cache' / 'fleet-management'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (directory / 'update.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return {'status': 'busy', 'tools': {}, 'services': {}, 'error': 'another_fleet_update_is_running'}
    try:
        for name, spec in config['tools'].items():
            result['tools'][name] = tool_result(name, spec, env, config['action'], config['dry_run'], config['timeout'])
        for spec in config['services']:
            updater = spec.get('updater_spec')
            if config['action'] == 'service-update' and updater:
                result['services'][spec['name']] = service_update_result(
                    spec, updater, env, config['dry_run'], config['timeout'])
            else:
                result['services'][spec['name']] = service_result(spec, env, min(config['timeout'], 30))
        good = {'ok', 'updated', 'unchanged', 'check_only', 'planned'}
        if any(r['status'] not in good for r in list(result['tools'].values()) + list(result['services'].values())):
            result['status'] = 'failed'
        return result
    finally:
        if lock:
            lock.close()
