"""Offline tests. Native uninstall commands are always mocked."""
import argparse
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pocket_uninstaller as p


class InventoryTests(unittest.TestCase):
    def test_dpkg_omits_residual_configs_and_marks_essential(self):
        rows = p.parse_apt("foo:amd64\t1.2\tinstalled\t\tA useful app\n"
                           "old\t1\tconfig-files\t\tRemoved app\n"
                           "bash\t5\tinstalled\tyes\tShell\n")
        self.assertEqual([a.ident for a in rows], ["foo:amd64", "bash"])
        self.assertTrue(p.guarded(rows[1]))

    def test_flatpak_preserves_scope_arch_and_branch(self):
        data = "org.example.App/x86_64/stable\tExample\t1.0\tflathub\n"
        user = p.parse_flatpak(data, "user")[0]
        system = p.parse_flatpak(data, "system")[0]
        self.assertNotEqual(user.key, system.key)
        self.assertEqual(user.ident, "app/org.example.App/x86_64/stable")
        with patch.object(p, "tool", return_value="/usr/bin/flatpak"):
            self.assertEqual(p.removal_command(user)[-2:], ["--user", user.ident])

    def test_flatpak_named_installation(self):
        self.assertEqual(p.flatpak_scope("installation:games"), "--installation=games")
        with self.assertRaises(ValueError):
            p.flatpak_scope("installation:--all")

    def test_snap_base_protected(self):
        data = "Name Version Rev Tracking Publisher Notes\ncore24 1 10 latest/stable canonical base\nvlc 3 2 latest/stable videolan -\n"
        apps = p.parse_snap(data)
        self.assertTrue(p.guarded(apps[0]))
        self.assertFalse(p.guarded(apps[1]))

    def test_pipx_venv_name_preserved(self):
        data = {"venvs": {"black@stable": {"metadata": {"main_package": {"package_version": "24.0"}}}}}
        app = p.parse_pipx(json.dumps(data))[0]
        self.assertEqual(app.ident, "black@stable")
        self.assertEqual(app.scope, "user")

    def test_one_failing_backend_does_not_hide_others(self):
        available = {"apt-get", "dpkg-query", "pipx"}
        with patch.object(p, "tool", side_effect=lambda n, *a: "/bin/" + n if n in available else None), \
             patch.object(p, "scan_apt", return_value=[p.App("APT", "foo", "foo")]), \
             patch.object(p, "scan_pipx", side_effect=TimeoutError("timed out")), \
             patch.object(p, "scan_appimages", return_value=[]):
            apps, warnings = p.scan_all()
        self.assertEqual(len(apps), 1)
        self.assertIn("pipx", warnings[0])

    def test_rpm_arch_and_protection(self):
        with patch.object(p, "tool", return_value="/usr/bin/rpm"), \
             patch.object(p, "read_command", return_value="glibc.x86_64\t2.39-1\tCore library\nfoo.i686\t1-2\tApp\n"):
            apps = p.scan_rpm("DNF")
        self.assertTrue(p.guarded(apps[0]))
        self.assertEqual(apps[1].ident, "foo.i686")


class CommandTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(p, "tool", side_effect=lambda name, *a: "/usr/bin/" + name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_automatic_yes_force_or_dependency_bypass(self):
        for manager in ("APT", "Pacman", "DNF", "Zypper", "Snap", "Flatpak", "pipx", "Homebrew"):
            with self.subTest(manager=manager):
                app = p.App(manager, "foo", "Foo")
                args = p.removal_command(app)
                self.assertEqual(args[-1], "foo")
                for forbidden in ("-y", "--assumeyes", "--noconfirm", "--force", "--nodeps", "--delete-data", "--purge"):
                    self.assertNotIn(forbidden, args)

    def test_option_shell_and_selector_injection_rejected(self):
        for value in ("--all", "-y", "foo;touch /tmp/x", "$(whoami)", "foo*", "../foo", "foo-", "foo+", "foo\nbar", "foo=1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                p.removal_command(p.App("APT", value, value))

    def test_debian_multiarch_supported(self):
        self.assertEqual(p.removal_command(p.App("APT", "foo:amd64", "foo"))[-1], "foo:amd64")

    def test_protected_packages_refused(self):
        for name in ("bash", "systemd", "linux", "linux-lts", "python3", "python3-tk", "apt", "sudo"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                p.removal_command(p.App("APT", name, name))

    def test_metadata_protection_even_for_unknown_name(self):
        with self.assertRaises(ValueError):
            p.removal_command(p.App("APT", "critical-custom-app", "Critical", protected=True))

    def test_purge_explicit_and_apt_only(self):
        self.assertIn("remove", p.removal_command(p.App("APT", "foo", "Foo")))
        self.assertIn("purge", p.removal_command(p.App("APT", "foo", "Foo"), True))
        with self.assertRaises(ValueError):
            p.removal_command(p.App("Pacman", "foo", "Foo"), True)

    def test_previews_are_read_only_commands(self):
        for manager, flag in [("APT", "--simulate"), ("Pacman", "--print"), ("DNF", "--assumeno"), ("Zypper", "--dry-run")]:
            self.assertIn(flag, p.preview_command(p.App(manager, "foo", "Foo")))
        self.assertIsNone(p.preview_command(p.App("Snap", "foo", "Foo")))


class FileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.store = self.home / ".local/share/pocket-uninstaller"
        for key, value in (("HOME", self.home), ("STORE", self.store)):
            patcher = patch.object(p, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {key: "" for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def folder(self, relative):
        folder = self.home / relative
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "settings.txt").write_text("precious data")
        return folder

    def test_only_exact_names_are_offered(self):
        expected = self.folder(".config/foo")
        self.folder(".config/foobar")
        self.folder(".cache/foo-old")
        self.assertEqual(p.candidates(p.App("APT", "foo", "Foo")), [expected])

    def test_root_system_home_and_shared_paths_refused(self):
        for value in ("/", "/etc", str(self.home), str(self.home / ".config"), str(self.store), str(self.home / ".local")):
            with self.subTest(path=value), self.assertRaises((ValueError, FileNotFoundError)):
                p.safe_path(value)

    def test_shared_desktop_storage_refused(self):
        self.folder(".local/share/applications")
        self.assertEqual(p.candidates(p.App("APT", "applications", "Applications")), [])

    def test_symlink_and_symlink_parent_refused(self):
        target = self.folder("actual/foo")
        (self.home / ".config").symlink_to(self.home / "actual", target_is_directory=True)
        with self.assertRaises(ValueError):
            p.safe_path(self.home / ".config/foo")
        link = self.home / "link"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            p.safe_path(link)

    def test_backup_and_restore_roundtrip(self):
        folder = self.folder(".config/foo")
        backup, errors = p.backup_paths([folder], "Foo", [folder])
        self.assertFalse(errors)
        self.assertFalse(folder.exists())
        restored, errors = p.restore_backup(backup)
        self.assertEqual(restored, 1)
        self.assertFalse(errors)
        self.assertEqual((folder / "settings.txt").read_text(), "precious data")

    def test_restore_never_overwrites_new_data(self):
        folder = self.folder(".config/foo")
        backup, _ = p.backup_paths([folder], "Foo", [folder])
        folder.mkdir()
        (folder / "new.txt").write_text("new data")
        restored, errors = p.restore_backup(backup)
        self.assertEqual(restored, 0)
        self.assertTrue(errors)
        self.assertEqual((folder / "new.txt").read_text(), "new data")
        self.assertTrue((backup / "0/settings.txt").exists())

    def test_unreviewed_path_refused(self):
        folder = self.folder("Documents")
        with self.assertRaises(ValueError):
            p.backup_paths([folder], "Foo", [])
        self.assertTrue(folder.exists())

    def test_cross_device_move_fails_without_deleting(self):
        folder = self.folder(".config/foo")
        with patch.object(p.os, "rename", side_effect=OSError(errno.EXDEV, "Cross-device link")):
            backup, errors = p.backup_paths([folder], "Foo", [folder])
        self.assertTrue(errors)
        self.assertTrue(folder.exists())
        self.assertEqual(json.loads((backup / "manifest.json").read_text())["entries"][0]["state"], "failed")

    def test_pending_manifest_recovers_after_interrupted_write(self):
        folder = self.folder(".config/foo")
        backup, _ = p.backup_paths([folder], "Foo", [folder])
        manifest = json.loads((backup / "manifest.json").read_text())
        manifest["entries"][0]["state"] = "pending"
        p.atomic_json(backup / "manifest.json", manifest)
        self.assertEqual(p.restore_backup(backup)[0], 1)

    def test_manifest_cannot_escape_payload_folder(self):
        folder = self.folder(".config/foo")
        backup, _ = p.backup_paths([folder], "Foo", [folder])
        manifest = json.loads((backup / "manifest.json").read_text())
        manifest["entries"][0]["stored"] = "../../outside"
        p.atomic_json(backup / "manifest.json", manifest)
        restored, errors = p.restore_backup(backup)
        self.assertEqual(restored, 0)
        self.assertTrue(errors)

    def test_operation_lock_prevents_overlap(self):
        with p.lock_operation():
            with self.assertRaises(RuntimeError):
                p.lock_operation()

    def test_appimage_magic_and_cleanup_mapping(self):
        image = self.home / "Applications/Foo.AppImage"
        image.parent.mkdir()
        image.write_bytes(b"\x7fELF\x00\x00\x00\x00AI\x02payload")
        self.assertTrue(p.appimage_valid(image))
        self.assertEqual(p.scan_appimages()[0].ident, str(image))
        portable = self.folder("Applications/Foo.AppImage.home")
        desktop = self.home / ".local/share/applications/foo.desktop"
        desktop.parent.mkdir(parents=True)
        desktop.write_text('[Desktop Entry]\nExec="' + str(image) + '" %U\n')
        app = p.App("AppImage", str(image), "Foo", scope="user")
        self.assertEqual(set(p.candidates(app)), {portable, desktop})
        image.write_bytes(b"not an appimage")
        self.assertFalse(p.appimage_valid(image))

    def test_foreign_xdg_root_falls_back(self):
        with patch.dict(os.environ, XDG_CONFIG_HOME="/etc"):
            self.assertEqual(p.cleanup_roots()[0], self.home / ".config")


class RunnerTests(unittest.TestCase):
    def args(self):
        return argparse.Namespace(uninstall=["APT", "foo"], scope="system", purge=False, receipt=None)

    def run_mocked(self, answers, current, native_return=0):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(p.os, "geteuid", return_value=1000), \
             patch.object(p, "lock_operation", return_value=open(Path(tmp) / "lock", "w")), \
             patch.object(p, "find_current", side_effect=current), \
             patch.object(p, "plan_text", return_value="preview"), \
             patch.object(p, "tool", side_effect=lambda name, *a: "/usr/bin/" + name), \
             patch("builtins.input", side_effect=answers), \
             patch.object(p.subprocess, "call", return_value=native_return) as call, \
             patch("sys.stdout", new_callable=io.StringIO):
            result = p.run_removal(self.args())
            return result, call.call_args_list

    def test_cancel_never_calls_native_removal(self):
        result, calls = self.run_mocked([""], [p.App("APT", "foo", "Foo")])
        self.assertEqual(result, 1)
        self.assertEqual(calls, [])

    def test_stale_selection_never_calls_native_removal(self):
        result, calls = self.run_mocked([], [None])
        self.assertEqual(result, 1)
        self.assertEqual(calls, [])

    def test_exit_zero_with_package_remaining_is_not_success(self):
        app = p.App("APT", "foo", "Foo")
        result, calls = self.run_mocked(["REMOVE"], [app, app])
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)

    def test_success_requires_absence_and_sudo_only_wraps_manager(self):
        app = p.App("APT", "foo", "Foo")
        result, calls = self.run_mocked(["REMOVE"], [app, None])
        self.assertEqual(result, 0)
        self.assertEqual(calls[0].args[0][:3], ["/usr/bin/sudo", "--", "/usr/bin/apt-get"])

    def test_failed_manager_is_not_success(self):
        result, _ = self.run_mocked(["REMOVE"], [p.App("APT", "foo", "Foo")], native_return=1)
        self.assertEqual(result, 1)

    def test_root_gui_runner_refused(self):
        with patch.object(p.os, "geteuid", return_value=0), self.assertRaises(RuntimeError):
            p.run_removal(self.args())

    def test_reinstalled_app_blocks_cleanup(self):
        app = p.App("APT", "foo", "Foo")
        with patch.object(p, "find_current", return_value=app), self.assertRaises(RuntimeError):
            p.ensure_cleanup_eligible(app)

    def test_other_flatpak_branch_blocks_shared_data_cleanup(self):
        old = p.App("Flatpak", "app/org.example.App/x86_64/stable", "App", scope="user")
        other = p.App("Flatpak", "app/org.example.App/x86_64/beta", "App", scope="system")
        with patch.object(p, "find_current", return_value=None), \
             patch.object(p, "flatpak_installations", return_value=["system"]), \
             patch.object(p, "scan_flatpak", return_value=[other]), self.assertRaises(RuntimeError):
            p.ensure_cleanup_eligible(old)


if __name__ == "__main__":
    unittest.main()
