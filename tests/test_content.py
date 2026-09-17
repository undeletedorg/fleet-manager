import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fleetlib import content, content_remote as worker, controller


def bundle(text='first', executable=False):
    raw = ('---\nname: sample\ndescription: A sample testing skill\n---\n' + text + '\n').encode()
    entry = {'content': base64.b64encode(raw).decode(), 'sha256': worker.digest(raw), 'executable': executable}
    files = {'SKILL.md': entry}
    hashes = {'SKILL.md': {k: v for k, v in entry.items() if k != 'content'}}
    return {'sample': {'files': files, 'sha256': worker.tree_hash(hashes)}}


def spec(agent='codex', skills=None, text=''):
    return {'selected_skills': ['sample'] if skills is None else skills,
            'instruction_text': text, 'skills_dir': '.' + agent + '/skills',
            'instruction_file': '.' + agent + '/AGENTS.md',
            'audit_skills': ['.' + agent + '/skills'],
            'audit_instructions': ['.' + agent + '/AGENTS.md']}


def config(kind='skills', dry_run=False, text='', skills=None):
    return {'kind': kind, 'action': 'sync', 'dry_run': dry_run, 'adopt': False,
            'prune': False, 'agents': {'codex': spec(text=text, skills=skills)},
            'bundle': bundle() if kind == 'skills' else {}}


class ContentWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.target = self.home / '.codex/skills/sample'
        self.instruction = self.home / '.codex/AGENTS.md'

    def run_sync(self, cfg):
        return worker.execute_content(cfg, self.home)

    def item(self, result):
        return result['agents']['codex']['items'][0]

    def test_empty_collection_does_not_create_state_or_files(self):
        cfg = config(skills=[])
        cfg['bundle'] = {}
        result = self.run_sync(cfg)
        self.assertEqual(result['status'], 'ok')
        self.assertFalse(list(self.home.iterdir()))

    def test_dry_run_creates_nothing(self):
        result = self.run_sync(config(dry_run=True))
        self.assertEqual(self.item(result)['status'], 'planned')
        self.assertFalse(list(self.home.iterdir()))

    def test_sync_then_check_is_idempotent(self):
        result = self.run_sync(config())
        self.assertEqual(self.item(result)['status'], 'synced')
        self.assertTrue((self.target / 'SKILL.md').is_file())
        before = (self.target / 'SKILL.md').stat().st_mtime_ns
        result = self.run_sync(config())
        self.assertEqual(self.item(result)['status'], 'in_sync')
        self.assertEqual(before, (self.target / 'SKILL.md').stat().st_mtime_ns)

    def test_source_update_has_verified_backup(self):
        self.run_sync(config())
        cfg = config()
        cfg['bundle'] = bundle('second')
        result = self.run_sync(cfg)
        row = self.item(result)
        self.assertEqual(row['status'], 'synced')
        self.assertIn('first', (self.home / row['backup'] / 'SKILL.md').read_text())
        self.assertIn('second', (self.target / 'SKILL.md').read_text())

    def test_host_edit_is_not_overwritten_even_with_adopt(self):
        self.run_sync(config())
        (self.target / 'SKILL.md').write_text('local changes')
        cfg = config()
        cfg['adopt'] = True
        result = self.run_sync(cfg)
        self.assertEqual(self.item(result)['status'], 'conflict')
        self.assertEqual((self.target / 'SKILL.md').read_text(), 'local changes')

    def test_conflict_prevents_other_agent_writes_on_same_host(self):
        self.target.mkdir(parents=True)
        (self.target / 'SKILL.md').write_text('unmanaged version')
        cfg = config()
        cfg['agents']['claude'] = spec('claude')
        result = self.run_sync(cfg)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse((self.home / '.claude/skills/sample').exists())

    def test_external_identical_symlink_is_preserved_until_adopted(self):
        external = self.home / 'external-skill'
        external.mkdir()
        raw = base64.b64decode(bundle()['sample']['files']['SKILL.md']['content'])
        (external / 'SKILL.md').write_bytes(raw)
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(external, target_is_directory=True)
        result = self.run_sync(config())
        self.assertEqual(self.item(result)['status'], 'external_identical')
        self.assertTrue(self.target.is_symlink())
        cfg = config()
        cfg['adopt'] = True
        result = self.run_sync(cfg)
        self.assertEqual(self.item(result)['status'], 'synced')
        self.assertFalse(self.target.is_symlink())
        self.assertTrue((self.home / self.item(result)['backup']).is_symlink())
        self.assertEqual((external / 'SKILL.md').read_bytes(), raw)

    def test_references_and_executable_modes_are_preserved(self):
        cfg = config()
        raw = b'#!/bin/sh\necho sample\n'
        entry = {'content': base64.b64encode(raw).decode(), 'sha256': worker.digest(raw), 'executable': True}
        cfg['bundle']['sample']['files']['scripts/helper.sh'] = entry
        cfg['bundle']['sample']['sha256'] = worker.tree_hash({path: {k: v for k, v in val.items() if k != 'content'} for path, val in cfg['bundle']['sample']['files'].items()})
        self.run_sync(cfg)
        self.assertTrue((self.target / 'scripts/helper.sh').stat().st_mode & 0o111)
        self.assertEqual((self.target / 'scripts/helper.sh').read_bytes(), raw)

    def test_symlink_ancestor_cannot_redirect_writes(self):
        outside = self.home / 'elsewhere'
        outside.mkdir()
        (self.home / '.codex').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_sync(config())
        self.assertFalse(list(outside.iterdir()))

    def test_path_traversal_and_corrupt_bundle_are_rejected(self):
        with self.assertRaises(ValueError):
            worker.safe_path(self.home, '../elsewhere')
        cfg = config()
        cfg['bundle']['sample']['files']['SKILL.md']['sha256'] = 'invalid'
        with self.assertRaises(ValueError):
            self.run_sync(cfg)
        self.assertFalse(list(self.home.iterdir()))

    def test_prune_only_removes_unchanged_managed_skills(self):
        self.run_sync(config())
        unmanaged = self.target.parent / 'personal'
        unmanaged.mkdir()
        (unmanaged / 'SKILL.md').write_text('my own skill')
        cfg = config(skills=[])
        cfg['bundle'] = {}
        first = self.run_sync(cfg)
        self.assertEqual(first['status'], 'failed')
        self.assertTrue(self.target.exists())
        cfg['prune'] = True
        result = self.run_sync(cfg)
        self.assertEqual(self.item(result)['status'], 'removed')
        self.assertFalse(self.target.exists())
        self.assertTrue((unmanaged / 'SKILL.md').is_file())
        self.assertTrue((self.home / self.item(result)['backup'] / 'SKILL.md').is_file())

    def test_prune_refuses_modified_managed_skill(self):
        self.run_sync(config())
        (self.target / 'SKILL.md').write_text('local edit')
        cfg = config(skills=[])
        cfg['bundle'] = {}
        cfg['prune'] = True
        result = self.run_sync(cfg)
        self.assertEqual(self.item(result)['status'], 'conflict')
        self.assertTrue(self.target.exists())

    def test_instruction_updates_preserve_surrounding_user_content(self):
        self.instruction.parent.mkdir()
        self.instruction.write_text('My existing preferences.\n')
        self.run_sync(config(kind='instructions', text='Shared guidance one.'))
        original = self.instruction.read_text()
        self.instruction.write_text(original + 'New host-specific preference.\n')
        result = self.run_sync(config(kind='instructions', text='Shared guidance two.'))
        self.assertEqual(self.item(result)['status'], 'synced')
        text = self.instruction.read_text()
        self.assertTrue(text.startswith('My existing preferences.\n'))
        self.assertTrue(text.endswith('New host-specific preference.\n'))
        self.assertIn('Shared guidance two.', text)
        self.assertNotIn('Shared guidance one.', text)

    def test_instruction_conflict_leaves_file_unchanged(self):
        self.run_sync(config(kind='instructions', text='Shared guidance.'))
        text = self.instruction.read_text().replace('Shared guidance.', 'Host changed the shared block.')
        self.instruction.write_text(text)
        result = self.run_sync(config(kind='instructions', text='New shared guidance.'))
        self.assertEqual(self.item(result)['status'], 'conflict')
        self.assertEqual(self.instruction.read_text(), text)

    def test_codex_override_is_reported_as_conflict(self):
        self.instruction.parent.mkdir()
        self.instruction.with_name('AGENTS.override.md').write_text('Override content.')
        result = self.run_sync(config(kind='instructions', text='Shared guidance.'))
        self.assertEqual(self.item(result)['status'], 'conflict')
        self.assertFalse(self.instruction.exists())

    def test_prune_instructions_keeps_personal_content(self):
        self.instruction.parent.mkdir()
        self.instruction.write_text('Personal guidance.\n')
        self.run_sync(config(kind='instructions', text='Shared guidance.'))
        cfg = config(kind='instructions', text='')
        cfg['prune'] = True
        result = self.run_sync(cfg)
        self.assertEqual(self.item(result)['status'], 'removed')
        self.assertIn('Personal guidance.', self.instruction.read_text())
        self.assertNotIn('Shared guidance.', self.instruction.read_text())

    def test_ambiguous_markers_are_rejected(self):
        with self.assertRaises(ValueError):
            worker.split_instruction((worker.BEGIN + worker.BEGIN + worker.END).encode())

    def test_instruction_preview_does_not_change_existing_preferences(self):
        self.instruction.parent.mkdir()
        self.instruction.write_text('Personal preferences.\n')
        result = self.run_sync(config(kind='instructions', text='Shared guidance.', dry_run=True))
        self.assertEqual(self.item(result)['status'], 'planned')
        self.assertEqual(self.instruction.read_text(), 'Personal preferences.\n')
        self.assertFalse((self.home / '.local').exists())

    def test_prune_preview_preserves_owned_skill(self):
        self.run_sync(config())
        cfg = config(skills=[], dry_run=True)
        cfg['bundle'] = {}
        cfg['prune'] = True
        result = self.run_sync(cfg)
        self.assertEqual(self.item(result)['status'], 'planned')
        self.assertEqual(self.item(result)['operation'], 'remove')
        self.assertTrue(self.target.exists())


class ContentControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text((controller.ROOT / 'agent-content/manifest.json').read_text())

    def test_empty_manifest_is_valid(self):
        data = content.load_manifest(self.manifest)
        self.assertEqual(data['skills'], {})
        self.assertEqual(content.make_bundle(data, self.root, ['codex'], 'skills'), {})

    def test_import_does_not_assign_or_deploy(self):
        source = self.root / 'sample'
        source.mkdir()
        (source / 'SKILL.md').write_bytes(base64.b64decode(bundle()['sample']['files']['SKILL.md']['content']))
        code = content.main(['skills', 'import', '--manifest', str(self.manifest), '--source', str(source)])
        self.assertEqual(code, 0)
        data = content.load_manifest(self.manifest)
        self.assertIn('sample', data['skills'])
        self.assertTrue(all(not row['skills'] for row in data['agents'].values()))
        self.assertTrue((self.root / 'skills/sample/SKILL.md').exists())

    def test_nested_symlink_is_rejected_on_import(self):
        source = self.root / 'sample'
        source.mkdir()
        (source / 'SKILL.md').write_bytes(base64.b64decode(bundle()['sample']['files']['SKILL.md']['content']))
        (source / 'private-link').symlink_to('/etc/passwd')
        code = content.main(['skills', 'import', '--manifest', str(self.manifest), '--source', str(source)])
        self.assertEqual(code, 2)
        self.assertFalse((self.root / 'skills/sample').exists())

    def test_unknown_assignment_fails_validation(self):
        data = json.loads(self.manifest.read_text())
        data['agents']['codex']['skills'] = ['missing']
        self.manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            content.load_manifest(self.manifest)

    def test_content_source_cannot_escape_catalogue(self):
        with self.assertRaises(ValueError):
            content.source_file(self.root, '../outside')

    def test_frontmatter_block_description_and_empty_scalar(self):
        path = self.root / 'SKILL.md'
        path.write_text('---\nname: sample\ndescription: >-\n  Describe the\n  sample skill.\n---\nBody\n')
        self.assertEqual(content.skill_metadata(path, 'sample'), [])
        path.write_text('---\nname: sample\ndescription:\nlicense: MIT\n---\nBody\n')
        with self.assertRaises(ValueError):
            content.skill_metadata(path, 'sample')

    def test_skill_import_refuses_symlinked_catalogue_directory(self):
        source = self.root / 'sample'
        source.mkdir()
        (source / 'SKILL.md').write_bytes(base64.b64decode(bundle()['sample']['files']['SKILL.md']['content']))
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / 'skills').symlink_to(outside, target_is_directory=True)
        code = content.main(['skills', 'import', '--manifest', str(self.manifest), '--source', str(source)])
        self.assertEqual(code, 2)
        self.assertFalse(list(outside.iterdir()))

    def test_ssh_payload_roundtrip_with_no_remote_changes(self):
        real_run = subprocess.run
        def fake_ssh(command, **kwargs):
            self.assertIn('StrictHostKeyChecking=yes', command)
            source = kwargs.pop('input')
            # Empty collection does not touch the test runner's home.
            result = real_run([sys.executable, '-'], input=source, **kwargs)
            return subprocess.CompletedProcess(command, result.returncode, 'banner\n' + result.stdout, result.stderr)
        cfg = config(skills=[])
        cfg['bundle'] = {}
        with patch.object(content.subprocess, 'run', side_effect=fake_ssh):
            result = content.query_content({'name': 'sample', 'hostname': 'example.test', 'user': 'test'}, {}, cfg, 10)
        self.assertEqual(result['status'], 'ok')

    def test_main_dispatch_keeps_existing_fleet_commands(self):
        self.assertEqual(controller.main(['skills', 'validate', '--manifest', str(self.manifest)]), 0)
        # The committed example: a fresh clone has no inventory.json or .env.
        example = str(controller.ROOT / 'examples' / 'inventory.json')
        with patch.dict(os.environ, {'FLEET_DOMAIN': 'mgmt.example.internal',
                                     'FLEET_SSH_USER': 'youruser',
                                     'SERVICE_URL': 'http://10.0.0.10'}):
            self.assertEqual(controller.main(['validate', '--inventory', example]), 0)


if __name__ == '__main__':
    unittest.main()
