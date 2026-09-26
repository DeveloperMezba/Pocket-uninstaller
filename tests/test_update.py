import dataclasses
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import io
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pocket_uninstaller as p


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / 'User with spaces'
        self.home.mkdir()
        self.store = self.home / '.local/share/pocket-uninstaller'
        for key, val in [('HOME', self.home), ('STORE', self.store)]:
            patcher = patch.object(p, key, val)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {'XDG_DATA_HOME': '', 'XDG_CONFIG_HOME': '', 'XDG_CACHE_HOME': '', 'XDG_STATE_HOME': ''})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_job(self, apps, inventories, code=0):
        folder = self.home / 'job'
        folder.mkdir(mode=0o700)
        job = folder / 'job.json'
        p.atomic_json(job, {'version': 1, 'reviewed': True, 'apps': [dataclasses.asdict(a) for a in apps]})
        with patch.object(p.os, 'geteuid', return_value=1000), \
             patch.object(p, 'inventory_for', side_effect=inventories), \
             patch.object(p, 'tool', side_effect=lambda n, *a: '/usr/bin/' + n), \
             patch.object(p.subprocess, 'call', return_value=code) as call, \
             patch('builtins.input', side_effect=AssertionError('No per-app typing allowed')), \
             patch('sys.stdout', new_callable=io.StringIO):
            exit_code = p.run_batch(job)
        return exit_code, json.loads((folder/'result.json').read_text()), call.call_args_list

    def test_two_packages_share_one_native_transaction(self):
        a, b = p.App('APT', 'foo', 'Foo'), p.App('APT', 'bar', 'Bar')
        code, result, calls = self.run_job([a, b], [[a, b], []])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0][-3:], ['--', 'foo', 'bar'])
        self.assertEqual([r['status'] for r in result['results']], ['removed', 'removed'])

    def test_mixed_sources_and_scopes_split_into_native_groups(self):
        a = p.App('Flatpak', 'app/org.example.Foo/x86_64/stable', 'Foo', scope='user')
        b = dataclasses.replace(a, scope='system')
        c = p.App('Snap', 'vlc', 'VLC')
        with patch.object(p, 'tool', side_effect=lambda n,*a: '/usr/bin/'+n):
            groups = p.batch_groups([a,b,c,a])
        self.assertEqual(len(groups), 3)
        self.assertEqual([len(g) for g in groups], [1,1,1])

    def test_pipx_uses_separate_commands_without_reconfirmation(self):
        a = p.App('pipx', 'foo', 'Foo', scope='user')
        b = p.App('pipx', 'bar', 'Bar', scope='user')
        code, result, calls = self.run_job([a,b], [[a,b],[b],[b],[]])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('sudo', calls[0].args[0][0])

    def test_partial_failure_preserves_verified_results_and_stops_next_source(self):
        a, b = p.App('APT', 'foo', 'Foo'), p.App('APT', 'bar', 'Bar')
        c = p.App('Snap', 'baz', 'Baz')
        code, result, calls = self.run_job([a,b,c], [[a,b],[b]])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual([r['status'] for r in result['results']], ['removed','failed','skipped'])

    def test_stale_selection_prevents_group_execution(self):
        a, b = p.App('APT', 'foo', 'Foo'), p.App('APT', 'bar', 'Bar')
        _, result, calls = self.run_job([a,b], [[a]])
        self.assertEqual(calls, [])
        self.assertFalse(result['success'])

    def test_nonfirst_protected_target_blocks_entire_batch(self):
        a, b = p.App('APT', 'foo', 'Foo'), p.App('APT', 'bash', 'Bash')
        _, result, calls = self.run_job([a,b], [])
        self.assertEqual(calls, [])
        self.assertIn('Protected', result['message'])

    def test_changed_metadata_protection_is_rechecked(self):
        a = p.App('APT', 'foo', 'Foo')
        _, result, calls = self.run_job([a], [[dataclasses.replace(a, protected=True)]])
        self.assertEqual(calls, [])
        self.assertIn('Protected', result['message'])

    def test_batch_does_not_add_force_or_yes_flags(self):
        for manager in ('APT','Pacman','DNF','Zypper','Flatpak','Snap','Homebrew'):
            with self.subTest(manager=manager), patch.object(p, 'tool', side_effect=lambda n,*a: '/usr/bin/'+n):
                argv = p.group_command([p.App(manager,'foo','Foo'), p.App(manager,'bar','Bar')])
                self.assertEqual(argv[-2:], ['foo','bar'])
                for flag in ('-y','--noconfirm','--force','--nodeps','--delete-data'):
                    self.assertNotIn(flag, argv)

    def test_optional_purge_applies_only_to_apt(self):
        with patch.object(p, "tool", side_effect=lambda n,*a: "/usr/bin/"+n):
            self.assertIn("purge",p.group_command([p.App("APT","foo","Foo")],purge_apt=True))
            self.assertIn("-R",p.group_command([p.App("Pacman","foo","Foo")],purge_apt=True))

    def test_launcher_repair_preserves_existing_backups(self):
        backup = self.store/'backup-existing'
        backup.mkdir(parents=True)
        (backup/'precious').write_text('keep')
        with patch.object(p.os,'geteuid',return_value=1000), \
             patch.object(p,'launcher_python',return_value='/usr/bin/python3'), \
             patch.object(p,'tool',return_value=None), patch('sys.stdout',new_callable=io.StringIO):
            p.install_launcher()
            p.install_launcher()
        desktop = self.home/'.local/share/applications/pocket-uninstaller.desktop'
        self.assertTrue(desktop.exists())
        text = desktop.read_text()
        self.assertIn('"/usr/bin/python3"',text)
        self.assertIn('--desktop-launch',text)
        self.assertIn('StartupWMClass=PocketUninstaller',text)
        self.assertTrue((self.store/'pocket-uninstaller.svg').exists())
        self.assertEqual((backup/'precious').read_text(),'keep')
        self.assertEqual((self.store/'pocket_uninstaller.py').read_bytes(),Path(p.__file__).read_bytes())

    def test_launcher_honors_custom_xdg_directory(self):
        custom = Path(self.tmp.name)/'custom data'
        with patch.dict(os.environ,XDG_DATA_HOME=str(custom)), \
             patch.object(p.os,'geteuid',return_value=1000), \
             patch.object(p,'launcher_python',return_value='/usr/bin/python3'), \
             patch.object(p,'tool',return_value=None), patch('sys.stdout',new_callable=io.StringIO):
            p.install_launcher()
        self.assertTrue((custom/'applications/pocket-uninstaller.desktop').exists())

    def test_desktop_escaping_has_both_required_layers(self):
        self.assertEqual(p.desktop_quote('hello world'), '"hello world"')
        self.assertEqual(p.desktop_quote('a$b'), '"a\\\\$b"')
        self.assertEqual(p.desktop_quote('a\\b'), '"a\\\\\\\\b"')
        self.assertEqual(p.desktop_quote('a%b'), '"a%%b"')
        with self.assertRaises(ValueError):
            p.desktop_quote('a\nb')

    def test_launcher_rejects_symlink_target(self):
        self.store.mkdir(parents=True)
        target=self.home/'unrelated'
        target.write_text('keep')
        (self.store/'pocket_uninstaller.py').symlink_to(target)
        with patch.object(p.os,'geteuid',return_value=1000), \
             patch.object(p,'launcher_python',return_value='/usr/bin/python3'), self.assertRaises(ValueError):
            p.install_launcher()
        self.assertEqual(target.read_text(),'keep')

    def test_gui_short_path_finds_user_installed_tool(self):
        binary=self.home/'.local/bin/pipx'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\n')
        binary.chmod(0o755)
        with patch.object(p.shutil,'which',return_value=None):
            self.assertEqual(p.tool('pipx'),str(binary))


if __name__ == '__main__':
    unittest.main()
