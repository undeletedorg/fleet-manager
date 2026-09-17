import ast
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from unittest.mock import patch

from fleetlib import controller, remote


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, PATH='/usr/bin:/bin')

    def executable(self, name, body):
        path = self.root / name
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o700)
        return path

    def spec(self, path, update=True):
        return {'paths': [str(path)], 'version_args': ['--version'], 'update_args': ['update'] if update else None}

    def test_explicit_path_works_when_not_in_ssh_path(self):
        exe = self.executable('grok', 'echo "grok 1.0.13"\n')
        result = remote.tool_result('grok', self.spec(exe), self.env, 'check', False, 2)
        self.assertEqual((result['status'], result['after'], result['path']), ('ok', '1.0.13', str(exe)))

    def test_broken_launcher_falls_back_to_other_known_path(self):
        broken = self.root / 'broken'
        broken.symlink_to(self.root / 'absent')
        exe = self.executable('actual', 'echo 1.0.0\n')
        spec = self.spec(broken)
        spec['paths'].append(str(exe))
        self.assertEqual(remote.discover('unknown', spec, self.env), str(exe))

    def test_missing_is_different_from_failed_version(self):
        missing = remote.tool_result('absent', self.spec(self.root / 'absent'), self.env, 'check', False, 2)
        exe = self.executable('broken', 'echo "failure 1.0.0"; exit 7\n')
        failed = remote.tool_result('broken', self.spec(exe), self.env, 'check', False, 2)
        self.assertEqual(missing['status'], 'missing')
        self.assertEqual(failed['status'], 'check_failed')
        self.assertIsNone(failed['after'])

    def test_dry_run_never_executes_updater(self):
        marker = self.root / 'updated'
        exe = self.executable('agent', 'if [ "$1" = "--version" ]; then echo 1.0.0; else touch "' + str(marker) + '"; fi\n')
        result = remote.tool_result('agent', self.spec(exe), self.env, 'update', True, 2)
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['command'], [str(exe), 'update'])
        self.assertFalse(marker.exists())

    def test_update_verifies_new_version(self):
        state = self.root / 'state'
        state.write_text('1.0.0\n')
        exe = self.executable('agent', 'if [ "$1" = "--version" ]; then cat "' + str(state) + '"; else echo 1.1.0 > "' + str(state) + '"; fi\n')
        result = remote.tool_result('agent', self.spec(exe), self.env, 'update', False, 2)
        self.assertEqual((result['status'], result['before'], result['after']), ('updated', '1.0.0', '1.1.0'))

    def test_failed_update_is_not_masked_by_successful_version_check(self):
        exe = self.executable('agent', 'if [ "$1" = "--version" ]; then echo 1.0.0; else echo "secret=do-not-log"; exit 9; fi\n')
        result = remote.tool_result('agent', self.spec(exe), self.env, 'update', False, 2)
        self.assertEqual(result['status'], 'update_failed')
        self.assertEqual(result['update_exit_code'], 9)
        self.assertEqual(result['after'], '1.0.0')
        self.assertNotIn('do-not-log', json.dumps(result))

    def test_successful_updater_with_missing_binary_fails_verification(self):
        exe = self.executable('agent', 'if [ "$1" = "--version" ]; then echo 1.0.0; else rm -- "$0"; fi\n')
        result = remote.tool_result('unfindable-agent', self.spec(exe), self.env, 'update', False, 2)
        self.assertEqual(result['status'], 'verification_failed')
        self.assertIsNone(result['after'])

    def test_check_only_package_is_not_updated(self):
        exe = self.executable('git', 'echo "git version 2.53.0"\n')
        result = remote.tool_result('git', self.spec(exe, update=False), self.env, 'update', False, 2)
        self.assertEqual(result['status'], 'check_only')
        self.assertNotIn('command', result)

    def test_timeout_kills_descendants(self):
        marker = self.root / 'survived'
        exe = self.executable('slow', '(sleep 0.3; touch "' + str(marker) + '") &\nwait\n')
        result = remote.run([str(exe)], self.env, 0.05)
        self.assertEqual(result['error'], 'timeout')
        time.sleep(0.4)
        self.assertFalse(marker.exists())

    def test_literal_paths_do_not_execute_shell_substitutions(self):
        exe = self.executable('agent $(touch injected)', 'echo 1.2.3\n')
        result = remote.tool_result('agent', self.spec(exe), self.env, 'check', False, 2)
        self.assertEqual(result['after'], '1.2.3')
        self.assertFalse((self.root / 'injected').exists())

    def test_busy_update_does_not_invoke_tools(self):
        lock_dir = self.root / '.cache' / 'fleet-management'
        lock_dir.mkdir(parents=True)
        import fcntl
        with (lock_dir / 'update.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(remote.Path, 'home', return_value=self.root), patch.object(remote, 'tool_result') as tool:
                result = remote.execute({'path_dirs': ['/usr/bin', '/bin'], 'action': 'update', 'dry_run': False, 'tools': {'agent': {}}, 'services': [], 'timeout': 2})
                self.assertEqual(result['status'], 'busy')
                tool.assert_not_called()

    def test_failed_tool_does_not_skip_next_tool(self):
        bad = self.root / 'absent'
        good = self.executable('good', 'echo 1.0.0\n')
        result = remote.execute({'path_dirs': ['/usr/bin', '/bin'], 'action': 'check', 'dry_run': False, 'tools': {'bad': self.spec(bad), 'good': self.spec(good)}, 'services': [], 'timeout': 2})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['tools']['good']['status'], 'ok')

    def test_compose_missing_service_cannot_be_healthy(self):
        outputs = [
            {'exit_code': 0, 'output': 'web\ndatabase\n'},
            {'exit_code': 0, 'output': json.dumps({'Service': 'web', 'State': 'running', 'Health': 'healthy'})},
            {'exit_code': 0, 'output': '200'},
        ]
        with patch.object(remote, 'run', side_effect=outputs):
            result = remote.service_result({'kind': 'compose', 'compose_file': '/compose.yml', 'url': 'http://example.test'}, self.env, 2)
        self.assertEqual(result['status'], 'unhealthy')
        self.assertEqual(result['missing_or_unhealthy_services'], ['database'])

    def test_compose_array_format_and_unhealthy_container(self):
        outputs = [{'exit_code': 0, 'output': 'web\n'},
                   {'exit_code': 0, 'output': json.dumps([{'Service': 'web', 'State': 'running', 'Health': 'unhealthy'}])},
                   {'exit_code': 0, 'output': '200'}]
        with patch.object(remote, 'run', side_effect=outputs):
            result = remote.service_result({'kind': 'compose', 'compose_file': '/compose.yml', 'url': 'http://example.test'}, self.env, 2)
        self.assertEqual(result['status'], 'unhealthy')

    def updater(self, path, args=None):
        return {'name': 't3', 'paths': [str(path)], 'version_args': ['--version'],
                'update_args': args or ['update', '--yes']}

    def service(self, unit='t3code.service'):
        return {'name': 't3-connect', 'kind': 'systemd_user', 'unit': unit, 'updater': 't3'}

    def test_service_update_refuses_to_run_inside_the_unit_cgroup(self):
        """Stopping the unit SIGKILLs its own cgroup, so an update started from in
        there kills itself and leaves the service down. Guard, do not execute."""
        marker = self.root / 'ran'
        exe = self.executable('t3', 'if [ "$1" = "--version" ]; then echo "t3 v1.0.0"; else touch "' + str(marker) + '"; fi\n')
        cgroup = '0::/user.slice/user-1000.slice/user@1000.service/app.slice/t3code.service\n'
        with patch('builtins.open', unittest.mock.mock_open(read_data=cgroup)):
            conflict = remote.cgroup_conflict('t3code.service')
        self.assertTrue(conflict)
        with patch.object(remote, 'cgroup_conflict', return_value=True):
            result = remote.service_update_result(self.service(), self.updater(exe), self.env, False, 2)
        self.assertEqual(result['status'], 'unsafe_cgroup')
        self.assertFalse(marker.exists(), 'updater must not run from inside the unit cgroup')

    def test_service_update_allows_a_shell_outside_the_unit_cgroup(self):
        cgroup = '0::/user.slice/user-1000.slice/user@1000.service/app.slice/ptyxis-spawn-1.scope\n'
        with patch('builtins.open', unittest.mock.mock_open(read_data=cgroup)):
            self.assertFalse(remote.cgroup_conflict('t3code.service'))

    def test_service_update_parses_v_prefixed_version(self):
        exe = self.executable('t3', 'echo "t3 v0.0.41-nightly.20260914.1700"\n')
        found, error = remote.version(str(exe), self.updater(exe), self.env, 2)
        self.assertIsNone(error)
        self.assertEqual(found, '0.0.41-nightly.20260914.1700')

    def test_service_update_dry_run_never_executes(self):
        marker = self.root / 'ran'
        exe = self.executable('t3', 'if [ "$1" = "--version" ]; then echo "t3 v1.0.0"; else touch "' + str(marker) + '"; fi\n')
        with patch.object(remote, 'cgroup_conflict', return_value=False):
            result = remote.service_update_result(self.service(), self.updater(exe), self.env, True, 2)
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['command'], [str(exe), 'update', '--yes'])
        self.assertFalse(marker.exists())

    def test_service_update_fails_when_unit_is_down_afterwards(self):
        """An updater that exits 0 but leaves the unit dead is a failed update."""
        exe = self.executable('t3', 'if [ "$1" = "--version" ]; then echo "t3 v1.0.0"; fi\n')
        with patch.object(remote, 'cgroup_conflict', return_value=False), \
             patch.object(remote, 'service_result', return_value={'status': 'unhealthy', 'details': {'ActiveState': 'inactive'}}):
            result = remote.service_update_result(self.service(), self.updater(exe), self.env, False, 2)
        self.assertEqual(result['status'], 'unhealthy_after_update')
        self.assertEqual(result['update_exit_code'], 0)

    def test_service_update_reports_unchanged_when_already_current(self):
        exe = self.executable('t3', 'if [ "$1" = "--version" ]; then echo "t3 v1.0.0"; fi\n')
        with patch.object(remote, 'cgroup_conflict', return_value=False), \
             patch.object(remote, 'service_result', return_value={'status': 'ok', 'details': {'linger': 'yes'}}):
            result = remote.service_update_result(self.service(), self.updater(exe), self.env, False, 2)
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual((result['before'], result['after']), ('1.0.0', '1.0.0'))

    def test_service_update_failure_does_not_report_updater_output(self):
        exe = self.executable('t3', 'if [ "$1" = "--version" ]; then echo "t3 v1.0.0"; else echo "token=do-not-log"; exit 4; fi\n')
        with patch.object(remote, 'cgroup_conflict', return_value=False):
            result = remote.service_update_result(self.service(), self.updater(exe), self.env, False, 2)
        self.assertEqual((result['status'], result['update_exit_code']), ('update_failed', 4))
        self.assertNotIn('do-not-log', json.dumps(result))

    def test_user_service_requires_linger(self):
        outputs = [{'exit_code': 0, 'output': 'LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\n'},
                   {'exit_code': 0, 'output': 'no\n'}]
        with patch.object(remote, 'run', side_effect=outputs):
            result = remote.service_result({'kind': 'systemd_user', 'unit': 't3code.service'}, self.env, 2)
        self.assertEqual(result['status'], 'unhealthy')


class ControllerTests(unittest.TestCase):
    def setUp(self):
        # The committed example, not the ignored live inventory: the suite must
        # pass on a fresh clone that has no inventory.json or .env yet.
        for key, value in {'FLEET_DOMAIN': 'mgmt.example.internal',
                           'FLEET_SSH_USER': 'youruser',
                           'SERVICE_URL': 'http://10.0.0.10'}.items():
            patcher = patch.dict(os.environ, {key: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.inventory = controller.ROOT / 'examples' / 'inventory.json'
        self.data = controller.load_inventory(self.inventory)
        self.host = self.data['hosts'][0]

    def test_unknown_host_and_group_are_rejected(self):
        with self.assertRaises(ValueError):
            controller.select_hosts(self.data, 'coding', ['typo'])
        with self.assertRaises(ValueError):
            controller.select_hosts(self.data, 'codng', None)
        with self.assertRaises(ValueError):
            controller.select_hosts(self.data, 'coding', ['compose01'])

    def test_ssh_failures_are_distinct(self):
        for stderr, expected in [('Host key verification failed.', 'host_key_untrusted'), ('Permission denied (publickey).', 'authentication_failed'), ('Connection timed out', 'unreachable')]:
            with self.subTest(stderr=stderr), patch.object(controller.subprocess, 'run', return_value=subprocess.CompletedProcess([], 255, '', stderr)):
                result = controller.query_host(self.host, self.data, 'check', False, 2, None)
                self.assertEqual(result['status'], expected)

    def test_worker_payload_is_executable_and_handles_login_banner(self):
        # Execute the exact serialized worker source in an isolated local process.
        # No tool commands selected: this exercises the wire protocol, not fleet hosts.
        real_run = subprocess.run
        def fake_ssh(command, **kwargs):
            self.assertIn('StrictHostKeyChecking=yes', command)
            result = real_run([sys.executable, '-'], **kwargs)
            return subprocess.CompletedProcess(command, result.returncode, 'Login banner\n' + result.stdout, result.stderr)
        with patch.object(controller.subprocess, 'run', side_effect=fake_ssh):
            result = controller.query_host(self.host, self.data, 'check', False, 2, ['not-assigned'])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['tools'], {})

    def test_reports_preserve_subset_observation_and_failed_attempt(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(controller, 'ROOT', Path(folder)):
            root = Path(folder)
            log_dir = root / 'logs'
            log_dir.mkdir()
            for filename, stamp, hosts in [
                ('run-1.json', '2026-09-01T00:00:00Z', [{'name': 'workstation01', 'status': 'ok', 'tools': {'codex': {'status': 'ok', 'after': '1.0.0', 'path': '/one'}}}]),
                ('run-2.json', '2026-09-02T00:00:00Z', [{'name': 'workstation02', 'status': 'ok', 'tools': {'claude': {'status': 'ok', 'after': '2.0.0', 'path': '/two'}}}]),
                ('run-3.json', '2026-09-03T00:00:00Z', [{'name': 'workstation01', 'status': 'unreachable', 'tools': {}}]),
            ]:
                (log_dir / filename).write_text(json.dumps({'action': 'check', 'checked_at': stamp, 'hosts': hosts}))
            controller.render_docs(self.data, log_dir)
            text = (root / 'coding-agents-cli.md').read_text()
            self.assertIn('| workstation01 | codex | 2026-09-01T00:00:00Z | ok | 1.0.0 | /one |', text)
            self.assertIn('| workstation01 | 2026-09-03T00:00:00Z | unreachable |', text)
            self.assertIn('| workstation02 | claude | 2026-09-02T00:00:00Z | ok | 2.0.0 | /two |', text)

    def save(self, data, folder):
        path = Path(folder) / 'inventory.json'
        path.write_text(json.dumps(data))
        return path

    def test_service_updater_must_exist_and_target_a_systemd_service(self):
        with tempfile.TemporaryDirectory() as folder:
            unknown = copy.deepcopy(self.data)
            unknown['services'][1]['updater'] = 'not-a-real-updater'
            with self.assertRaises(ValueError):
                controller.load_inventory(self.save(unknown, folder))

            compose = copy.deepcopy(self.data)
            for service in compose['services']:
                if service['kind'] == 'compose':
                    service['updater'] = 't3'
            with self.assertRaises(ValueError):
                controller.load_inventory(self.save(compose, folder))

    def test_service_updater_requires_update_args(self):
        with tempfile.TemporaryDirectory() as folder:
            data = copy.deepcopy(self.data)
            data['service_updaters']['t3']['update_args'] = None
            with self.assertRaises(ValueError):
                controller.load_inventory(self.save(data, folder))

    def test_service_update_sends_only_updatable_services_and_no_tools(self):
        """service-update must not drag the CLI tool updaters along with it."""
        captured = {}

        def fake_run(command, **kwargs):
            captured['input'] = kwargs['input']
            return subprocess.CompletedProcess(command, 255, '', 'Connection timed out')

        host = next(h for h in self.data['hosts'] if h['name'] == 'workstation02')
        with patch.object(controller.subprocess, 'run', side_effect=fake_run):
            controller.query_host(host, self.data, 'service-update', False, 2, None)
        payload = captured['input']
        # The worker embeds the config as a Python string literal holding JSON.
        literal = re.search(r'execute\(json\.loads\((.*)\)\)\)\)', payload, re.S).group(1)
        config = json.loads(ast.literal_eval(literal))
        self.assertEqual(config['tools'], {})
        self.assertEqual([s['name'] for s in config['services']], ['t3-workstation02'])
        self.assertEqual(config['services'][0]['updater_spec']['name'], 't3')
        self.assertEqual(config['services'][0]['updater_spec']['update_args'],
                         self.data['service_updaters']['t3']['update_args'])

    def test_service_update_rejects_hosts_without_an_updatable_service(self):
        # main() reports inventory errors as exit code 2 rather than raising.
        self.assertEqual(controller.main(['service-update', '--group', 'docker']), 2)

    def test_inventory_rejects_injected_ssh_destination(self):
        data = copy.deepcopy(self.data)
        data['hosts'][0]['hostname'] = '-oProxyCommand=oops'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'inventory.json'
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                controller.load_inventory(path)

    def test_malformed_inventory_fails_before_remote_execution(self):
        variants = [[], dict(self.data, ssh=[]), dict(self.data, services={}),
                    dict(self.data, hosts=['not-a-host']), dict(self.data, tools={'codex': []})]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'inventory.json'
            for data in variants:
                with self.subTest(data_type=type(data).__name__):
                    path.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        controller.load_inventory(path)

    def test_cli_exits_nonzero_and_persists_failed_host(self):
        result = {'name': self.host['name'], 'hostname': self.host['hostname'], 'user': self.host['user'], 'status': 'unreachable', 'error': 'offline', 'tools': {}, 'services': {}}
        with tempfile.TemporaryDirectory() as folder, patch.object(controller, 'query_host', return_value=result):
            status = controller.main(['check', '--host', 'workstation01', '--no-render',
                                      '--inventory', str(self.inventory), '--log-dir', folder])
            self.assertEqual(status, 1)
            reports = list(Path(folder).glob('run-*.json'))
            self.assertEqual(len(reports), 1)
            self.assertEqual(json.loads(reports[0].read_text())['hosts'][0]['status'], 'unreachable')


if __name__ == '__main__':
    unittest.main()
