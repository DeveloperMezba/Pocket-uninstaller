#!/usr/bin/env python3
"""Pocket Uninstaller 0.2.0. Python 3.9+, standard library only. MIT license."""
from __future__ import annotations

import argparse
import concurrent.futures
import configparser
import dataclasses
import datetime
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

VERSION = "0.2.0"
TITLE = "Pocket Uninstaller"
HOME = Path.home().resolve()
STORE = HOME / ".local/share/pocket-uninstaller"
ENV = dict(os.environ, LC_ALL="C", LANG="C", NO_COLOR="1",
           HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_INSTALL_CLEANUP="1")
SYSTEM = {"APT", "Pacman", "DNF", "Zypper", "Snap"}
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+@:/-]*\Z")
SHARED_NAMES = {"applications", "autostart", "Trash", "icons", "fonts", "themes", "mime",
                "flatpak", "pipx", "containers", "dconf", "systemd", "keyrings", "gvfs",
                "dbus", "pulse", "pipewire", "fontconfig", "xdg-desktop-portal"}
# A conservative guard, not a complete system-integrity model.
CORE = re.compile(
    r"^(?:apt|dpkg|pacman|dnf[0-9]*|rpm|zypper|sudo|polkit|systemd.*|udev|"
    r"glibc.*|libc6.*|libc-bin|musl.*|bash|dash|coreutils|filesystem|"
    r"base|base-files|base-passwd|base-devel|linux(?:-.*)?|kernel(?:-.*)?|"
    r"grub.*|shim.*|initramfs.*|dracut.*|cryptsetup.*|util-linux|mount|"
    r"networkmanager.*|network-manager.*|openssh.*|python3?(?:[.-].*)?|"
    r"tk(?:[0-9.-].*)?|tcl(?:[0-9.-].*)?|gnome-shell|plasma-workspace|"
    r"kwin.*|xorg-server.*|xserver-xorg.*|ubuntu-desktop.*|"
    r"mint-meta.*|cachyos-keyring|archlinux-keyring|snapd)$", re.I)


@dataclasses.dataclass(frozen=True)
class App:
    manager: str
    ident: str
    name: str
    version: str = ""
    scope: str = "system"
    note: str = ""
    protected: bool = False

    @property
    def key(self):
        return self.manager, self.ident, self.scope


def tool(name, system=False):
    if system:
        for directory in ("/usr/bin", "/usr/sbin", "/bin", "/sbin"):
            p = Path(directory) / name
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
        return None
    found = shutil.which(name)
    if found:
        return found
    # Graphical sessions frequently have a shorter PATH than interactive shells.
    for directory in (HOME / ".local/bin", Path("/home/linuxbrew/.linuxbrew/bin"),
                      HOME / ".linuxbrew/bin", Path("/usr/local/bin")):
        binary = directory / name
        if binary.is_file() and os.access(binary, os.X_OK):
            return str(binary)
    return None


def read_command(argv, timeout=45):
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, errors="replace", env=ENV, timeout=timeout)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout or "Command failed").strip()[:1500])
    return proc.stdout


def valid_ident(value):
    return bool(IDENT.fullmatch(value)) and ".." not in value and not value.endswith(("+", "-"))


def guarded(app):
    base = app.ident.split(":")[0]
    return app.protected or (app.manager in SYSTEM and bool(CORE.fullmatch(base)))


def parse_apt(output):
    result = []
    for line in output.splitlines():
        cols = line.split("\t", 4)
        if len(cols) != 5:
            raise ValueError("Unexpected dpkg-query output")
        name, version, status, essential, summary = cols
        if status == "installed":
            result.append(App("APT", name, name, version, note=summary,
                              protected=essential == "yes"))
    return result


def scan_apt():
    fmt = "${binary:Package}\t${Version}\t${db:Status-Status}\t${Essential}\t${binary:Summary}\\n"
    return parse_apt(read_command([tool("dpkg-query", True), "-W", "-f=" + fmt]))


def scan_pacman():
    result = []
    for line in read_command([tool("pacman", True), "-Q"]).splitlines():
        name, version = line.split(" ", 1)
        result.append(App("Pacman", name, name, version, note="Includes installed AUR / local packages"))
    return result


def scan_rpm(manager):
    fmt = "%{NAME}.%{ARCH}\t%{VERSION}-%{RELEASE}\t%{SUMMARY}\\n"
    result = []
    for line in read_command([tool("rpm", True), "-qa", "--qf", fmt]).splitlines():
        name, version, summary = line.split("\t", 2)
        base = name.rsplit(".", 1)[0]
        result.append(App(manager, name, base, version, note=summary,
                          protected=bool(CORE.fullmatch(base))))
    return result


def parse_flatpak(output, scope):
    result = []
    for line in output.splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) != 4:
            raise ValueError("Flatpak needs support for --columns=ref,name,version,origin")
        ref, name, version, origin = cols
        if not ref.startswith("app/"):
            ref = "app/" + ref
        if len(ref.split("/")) != 4:
            raise ValueError("Unexpected Flatpak reference: " + ref)
        result.append(App("Flatpak", ref, name, version, scope, "Remote: " + origin))
    return result


def flatpak_scope(scope):
    if scope in ("user", "system"):
        return "--" + scope
    if scope.startswith("installation:") and valid_ident(scope[13:]):
        return "--installation=" + scope[13:]
    raise ValueError("Unknown Flatpak installation")


def scan_flatpak(scope):
    return parse_flatpak(read_command([tool("flatpak"), "list", flatpak_scope(scope),
                                      "--app", "--columns=ref,name,version,origin"]), scope)


def flatpak_installations():
    scopes = ["user", "system"]
    for p in sorted(Path("/etc/flatpak/installations.d").glob("*.conf")):
        config = configparser.ConfigParser(interpolation=None)
        config.read(p)
        for section in config.sections():
            match = re.fullmatch(r'Installation "([^"]+)"', section)
            if match and match[1] != "default":
                scopes.append("installation:" + match[1])
    return list(dict.fromkeys(scopes))


def parse_snap(output):
    result = []
    for line in output.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 3:
            raise ValueError("Unexpected snap list output")
        result.append(App("Snap", cols[0], cols[0], cols[1], note="Revision: " + cols[2],
                          protected=("base" in cols[-1].split(",") or cols[0] == "snapd")))
    return result


def scan_snap():
    # snap list returns 0 and a header when installed entries exist.
    p = subprocess.run([tool("snap", True), "list"], capture_output=True,
                       text=True, env=ENV, timeout=45)
    if p.returncode and "No snaps are installed" in p.stderr + p.stdout:
        return []
    if p.returncode:
        raise RuntimeError(p.stderr.strip())
    return parse_snap(p.stdout)


def parse_pipx(output):
    result = []
    for name, venv in json.loads(output)["venvs"].items():
        meta = venv.get("metadata", {}).get("main_package", {})
        result.append(App("pipx", name, name, meta.get("package_version", ""), "user",
                          "Isolated Python command-line app"))
    return result


def scan_pipx():
    return parse_pipx(read_command([tool("pipx"), "list", "--json"]))


def scan_brew():
    result = []
    for line in read_command([tool("brew"), "list", "--formula", "--versions"]).splitlines():
        name, *versions = line.split()
        result.append(App("Homebrew", name, name, " ".join(versions), "user",
                          "Installed Homebrew formula"))
    return result


def image_roots():
    return [HOME / "Applications", HOME / "AppImages", HOME / ".local/bin"]


def appimage_valid(path):
    p = Path(path)
    if not p.is_absolute() or p.suffix.lower() != ".appimage":
        return False
    try:
        st = p.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return False
        if p.resolve() != p or not p.is_relative_to(HOME):
            return False
        with p.open("rb") as f:
            header = f.read(11)
        return header[:4] == b"\x7fELF" and header[8:11] in (b"AI\x01", b"AI\x02")
    except OSError:
        return False


def scan_appimages(extra=()):
    paths = set()
    for root in image_roots():
        if root.is_dir() and not root.is_symlink():
            # Deliberately bounded; no expensive or intrusive home-wide scan.
            paths.update(root.glob("*"))
            paths.update(root.glob("*/*"))
    paths.update(Path(p) for p in extra)
    return [App("AppImage", str(p), p.stem, "portable", "user", "File: " + str(p))
            for p in sorted(paths) if appimage_valid(p)]


def scan_all(extra=()):
    jobs, warnings = [], []
    if tool("apt-get", True) and tool("dpkg-query", True):
        jobs.append(("APT", scan_apt))
    if tool("pacman", True):
        jobs.append(("Pacman", scan_pacman))
    if tool("rpm", True):
        if tool("zypper", True):
            jobs.append(("Zypper", lambda: scan_rpm("Zypper")))
        elif tool("dnf", True) or tool("dnf5", True):
            jobs.append(("DNF", lambda: scan_rpm("DNF")))
        else:
            warnings.append("RPM database found, but no supported DNF/Zypper frontend.")
    if tool("snap", True):
        jobs.append(("Snap", scan_snap))
    if tool("flatpak"):
        try:
            for scope in flatpak_installations():
                jobs.append(("Flatpak " + scope, lambda s=scope: scan_flatpak(s)))
        except Exception as exc:
            warnings.append("Flatpak installations: " + str(exc))
    if tool("pipx"):
        jobs.append(("pipx", scan_pipx))
    if tool("brew"):
        jobs.append(("Homebrew", scan_brew))
    jobs.append(("AppImage", lambda: scan_appimages(extra)))
    apps = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): label for label, fn in jobs}
        for future in concurrent.futures.as_completed(futures):
            try:
                apps.extend(future.result())
            except Exception as exc:
                warnings.append(f"{futures[future]}: {exc}")
    unique = {a.key: a for a in apps}
    return sorted(unique.values(), key=lambda a: (a.name.lower(), a.manager, a.scope)), warnings


def removal_command(app, purge=False):
    if guarded(app):
        raise ValueError("Protected system component. Use your distro's administration tools.")
    if app.manager == "AppImage":
        if not appimage_valid(app.ident):
            raise ValueError("AppImage is no longer a regular, owned AppImage file.")
        return []
    if not valid_ident(app.ident):
        raise ValueError("Unsupported package identifier; refusing to interpret it as an option.")
    i, m = app.ident, app.manager
    if purge and m != "APT":
        raise ValueError("System configuration purge is supported only for APT.")
    if m == "APT":
        argv = [tool("apt-get", True), "-o", "APT::Get::AutomaticRemove=false",
                "-o", "APT::Get::Assume-Yes=false", "purge" if purge else "remove", "--", i]
    elif m == "Pacman":
        argv = [tool("pacman", True), "-R", "--", i]
    elif m == "DNF":
        argv = [tool("dnf", True) or tool("dnf5", True),
                "--setopt=clean_requirements_on_remove=False", "--setopt=assumeyes=False",
                "remove", "--", i]
    elif m == "Zypper":
        argv = [tool("zypper", True), "--no-refresh", "remove", "--no-clean-deps", "--", i]
    elif m == "Snap":
        argv = [tool("snap", True), "remove", i]
    elif m == "Flatpak":
        argv = [tool("flatpak"), "uninstall", flatpak_scope(app.scope), i]
    elif m == "pipx":
        argv = [tool("pipx"), "uninstall", i]
    elif m == "Homebrew":
        argv = [tool("brew"), "uninstall", "--formula", i]
    else:
        raise ValueError("Unsupported manager")
    if not argv[0]:
        raise ValueError("Package manager executable is missing")
    return argv


def preview_command(app, purge=False):
    argv = removal_command(app, purge)
    if app.manager == "APT":
        return argv[:1] + ["--simulate"] + argv[1:]
    if app.manager == "Pacman":
        return argv[:1] + ["--print", "--print-format", "%n"] + argv[1:]
    if app.manager == "DNF":
        return argv[:1] + ["--assumeno", "--cacheonly"] + argv[1:]
    if app.manager == "Zypper":
        return argv[:3] + ["--dry-run"] + argv[3:]
    return None


def plan_text(app, purge=False):
    argv = removal_command(app, purge)
    command = shlex.join((["sudo"] if app.manager in SYSTEM else []) + argv) if argv else "Move AppImage to recoverable backup"
    text = f"{app.name}\n{app.manager} / {app.scope}\n\n{command}\n\n"
    text += ("The terminal rechecks the installed package and asks you to type REMOVE.\n"
             "Read the native transaction before accepting it. It may include dependent packages.\n"
             "No automatic orphan sweep is requested. Close the app you are removing first.\n")
    preview = preview_command(app, purge)
    if preview:
        try:
            p = subprocess.run(preview, capture_output=True, text=True, errors="replace",
                               env=ENV, timeout=45)
            text += "\nNative preview (read-only):\n" + (p.stdout + p.stderr)[-28000:]
            if p.returncode:
                text += f"\nPreview exit {p.returncode}. DNF --assumeno normally declines the transaction.\n"
                text += "If the preview could not resolve changes, review them in the terminal.\n"
        except Exception as exc:
            text += f"\nPreview unavailable: {exc}\nReview the native transaction in the terminal.\n"
    else:
        text += "\nNo separate native dry-run is used for this manager.\n"
    if app.manager == "Snap":
        text += "Snap may keep an automatic data snapshot. Existing snapshots are not erased.\n"
    if purge:
        text += "APT purge also removes package-managed system configuration. That is not backed up here.\n"
    return text


def xdg_root(variable, fallback):
    raw = os.environ.get(variable)
    p = Path(raw) if raw and Path(raw).is_absolute() else HOME / fallback
    try:
        if p.resolve() == p and p.is_relative_to(HOME) and p != HOME:
            return p
    except OSError:
        pass
    return HOME / fallback


def cleanup_roots():
    return [xdg_root("XDG_CONFIG_HOME", ".config"),
            xdg_root("XDG_CACHE_HOME", ".cache"),
            xdg_root("XDG_DATA_HOME", ".local/share"),
            xdg_root("XDG_STATE_HOME", ".local/state"), HOME / ".var/app", HOME / "snap"]


def safe_path(path, exists=True):
    p = Path(path)
    if not p.is_absolute() or not p.is_relative_to(HOME) or p == HOME:
        raise ValueError("Only a specific path inside your home folder can be moved.")
    if any(x in ("..", ".ssh", ".gnupg") for x in p.parts):
        raise ValueError("Sensitive or ambiguous path")
    if p.resolve() != p or p.is_symlink():
        raise ValueError("Symlinks and symlinked parent paths are excluded.")
    if p == STORE or p.is_relative_to(STORE) or STORE.is_relative_to(p):
        raise ValueError("The uninstaller and its backups cannot be cleanup candidates.")
    if p in cleanup_roots() or p in image_roots():
        raise ValueError("Shared parent folders cannot be moved.")
    if p.parent in cleanup_roots() and p.name in SHARED_NAMES:
        raise ValueError("Shared desktop or package-manager storage cannot be moved.")
    if exists:
        st = p.lstat()
        if st.st_uid != os.getuid() or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            raise ValueError("Path is not a regular file or folder owned by you.")
        if os.path.ismount(p):
            raise ValueError("Mount points cannot be moved.")
    return p


def candidates(app):
    if guarded(app):
        return []
    if app.manager == "Flatpak":
        names = [app.ident.split("/")[1]]
    elif app.manager == "AppImage":
        possible = [Path(app.ident + ".home"), Path(app.ident + ".config")]
        desktop_root = xdg_root("XDG_DATA_HOME", ".local/share") / "applications"
        for desktop in desktop_root.glob("*.desktop"):
            try:
                config = configparser.ConfigParser(interpolation=None, strict=False)
                config.read(desktop)
                argv = shlex.split(config.get("Desktop Entry", "Exec", fallback=""))
                if argv and argv[0] == app.ident:
                    possible.append(desktop)
            except (OSError, ValueError, configparser.Error):
                pass
        result = []
        for p in possible:
            try:
                safe_path(p)
                result.append(p)
            except (OSError, ValueError):
                pass
        return sorted(set(result))
    else:
        name = app.ident.split(":")[0]
        if app.manager in ("DNF", "Zypper"):
            name = name.rsplit(".", 1)[0]
        names = [name] if "/" not in name else []
    paths = []
    for root in cleanup_roots():
        for name in names:
            p = root / name
            try:
                safe_path(p)
                if p.is_dir():
                    paths.append(p)
            except (OSError, ValueError):
                pass
    # A .name home folder is only a possible match and remains unchecked in the UI.
    for name in names:
        p = HOME / ("." + name)
        try:
            safe_path(p)
            if p.is_dir():
                paths.append(p)
        except (OSError, ValueError):
            pass
    return sorted(set(paths))


def private_store():
    if STORE.resolve() != STORE:
        raise ValueError("Backup storage must not have symlinked parents.")
    STORE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if STORE.stat().st_uid != os.getuid():
        raise ValueError("Backup storage is not owned by this user.")
    os.chmod(STORE, 0o700)
    return STORE


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as f:
        os.chmod(temporary, 0o600)
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def backup_paths(paths, label, allowed):
    """Moves only reviewed candidates, using rename, never recursive deletion/copy."""
    allowed = {str(p) for p in allowed}
    checked = []
    for path in paths:
        p = safe_path(path)
        if str(p) not in allowed:
            raise ValueError("Path was not an offered cleanup candidate")
        if p not in checked:
            checked.append(p)
    if not checked:
        raise ValueError("Select at least one path")
    store = private_store()
    folder = store / ("backup-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    folder.mkdir(mode=0o700)
    manifest = {"version": 1, "label": label, "created": datetime.datetime.now().isoformat(), "entries": []}
    manifest_path = folder / "manifest.json"
    atomic_json(manifest_path, manifest)
    errors = []
    for n, p in enumerate(checked):
        entry = {"original": str(p), "stored": str(n), "state": "pending"}
        manifest["entries"].append(entry)
        atomic_json(manifest_path, manifest)
        try:
            safe_path(p)
            # Cross-filesystem moves fail safely instead of copying partially.
            os.rename(p, folder / str(n))
            entry["state"] = "moved"
        except (OSError, ValueError) as exc:
            entry["state"] = "failed"
            errors.append(f"{p}: {exc}")
        atomic_json(manifest_path, manifest)
    return folder, errors


def restore_backup(folder):
    folder = Path(folder)
    if folder.parent != private_store() or folder.resolve() != folder or not folder.name.startswith("backup-"):
        raise ValueError("Not a Pocket Uninstaller backup")
    manifest_path = folder / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("Invalid backup manifest")
    manifest = json.loads(manifest_path.read_text())
    errors, restored = [], 0
    for entry in manifest["entries"]:
        if entry["state"] not in ("moved", "pending"):
            continue
        try:
            if not re.fullmatch(r"[0-9]+", entry["stored"]):
                raise ValueError("Invalid stored path")
            src = folder / entry["stored"]
            if not src.exists() or src.is_symlink():
                raise ValueError("Backup payload is missing or invalid")
            target = safe_path(entry["original"], exists=False)
            if target.exists() or target.is_symlink():
                raise ValueError("Original path already exists; refusing to overwrite")
            if not target.parent.is_dir():
                raise ValueError("Original parent folder no longer exists")
            os.rename(src, target)
            entry["state"] = "restored"
            restored += 1
        except (OSError, ValueError) as exc:
            errors.append(f"{entry.get('original')}: {exc}")
        atomic_json(manifest_path, manifest)
    return restored, errors


def lock_operation():
    p = private_store() / "operation.lock"
    fd = os.open(p, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    f = os.fdopen(fd, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise RuntimeError("Another Pocket Uninstaller operation is running.")
    return f


def inventory_for(manager, scope, extra=()):
    if manager == "AppImage":
        found = scan_appimages(extra)
    elif manager == "APT":
        found = scan_apt()
    elif manager == "Pacman":
        found = scan_pacman()
    elif manager in ("DNF", "Zypper"):
        found = scan_rpm(manager)
    elif manager == "Flatpak":
        found = scan_flatpak(scope)
    elif manager == "Snap":
        found = scan_snap()
    elif manager == "pipx":
        found = scan_pipx()
    elif manager == "Homebrew":
        found = scan_brew()
    else:
        raise ValueError("Unsupported manager")
    return found


def find_current(manager, ident, scope):
    found = inventory_for(manager, scope, [ident])
    return next((a for a in found if a.key == (manager, ident, scope)), None)


def run_removal(args):
    if os.geteuid() == 0:
        raise RuntimeError("Run Pocket Uninstaller as your normal user, not with sudo.")
    receipt = Path(args.receipt) if args.receipt else None
    if receipt and (receipt.parent.resolve() != receipt.parent or
                    receipt.parent.stat().st_uid != os.getuid() or
                    receipt.parent.stat().st_mode & 0o077):
        raise ValueError("Receipt directory must be private and owned by you")
    result = {"state": "running", "success": False, "pid": os.getpid()}
    if receipt:
        atomic_json(receipt, result)
    lock = None
    try:
        lock = lock_operation()
        manager, ident = args.uninstall
        app = find_current(manager, ident, args.scope)
        if not app:
            raise RuntimeError("This exact package is no longer installed. Refresh the inventory.")
        argv = removal_command(app, args.purge)
        print(plan_text(app, args.purge), flush=True)
        if input("Type REMOVE to continue, or press Enter to cancel: ").strip() != "REMOVE":
            raise RuntimeError("Cancelled. No uninstall command was run.")
        if manager == "AppImage":
            folder, errors = backup_paths([ident], app.name, [ident])
            if errors:
                raise RuntimeError("\n".join(errors))
            print("AppImage backed up to", folder)
        else:
            if manager in SYSTEM:
                sudo = tool("sudo", True)
                if not sudo:
                    raise RuntimeError("sudo is required for system package removal.")
                argv = [sudo, "--"] + argv
            code = subprocess.call(argv, env=ENV)
            if code:
                raise RuntimeError(f"Package manager exited with status {code}. Check its output.")
        if find_current(manager, ident, args.scope) is not None:
            raise RuntimeError("Package is still installed. Removal was cancelled or incomplete.")
        result.update(state="done", success=True, message="Removal verified. You may now review leftover folders.")
    except (Exception, KeyboardInterrupt) as exc:
        result.update(state="done", success=False, message=str(exc) or "Interrupted")
    finally:
        if lock:
            lock.close()
        if receipt:
            atomic_json(receipt, result)
    print("\n" + result["message"], flush=True)
    if receipt:
        try:
            input("Press Enter to close this terminal...")
        except (EOFError, KeyboardInterrupt):
            pass
    return 0 if result["success"] else 1


def terminal_command(command):
    terminals = [("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--wait", "--"]),
                 ("konsole", ["--nofork", "-e"]), ("xfce4-terminal", ["--disable-server", "-x"]),
                 ("kitty", []), ("foot", []), ("alacritty", ["-e"]),
                 ("xterm", ["-e"]), ("mate-terminal", ["--disable-factory", "-x"])]
    for name, flags in terminals:
        binary = tool(name)
        if binary:
            return [binary] + flags + command
    raise RuntimeError("No supported terminal found. Run the CLI command shown in the review window in your terminal.")


def batch_groups(apps):
    groups = {}
    for app in dict((a.key, a) for a in apps).values():
        removal_command(app)  # Validate every target, including non-first entries.
        key = (app.manager, app.scope, app.ident if app.manager == "pipx" else "")
        groups.setdefault(key, []).append(app)
    return list(groups.values())


def group_command(group, preview=False, purge_apt=False):
    if not group or len({(a.manager, a.scope) for a in group}) != 1:
        raise ValueError("Invalid removal group")
    for app in group:
        removal_command(app)
    purge = purge_apt and group[0].manager == "APT"
    argv = preview_command(group[0], purge) if preview else removal_command(group[0], purge)
    if argv:
        argv += [a.ident for a in group[1:]]
    return argv


def batch_preview(apps, purge_apt=False):
    lines = [f"REVIEW {len(apps)} SELECTED APPLICATIONS", "",
             "Click Uninstall selected once. No per-app REMOVE typing.",
             "Native confirmations and authentication stay in the terminal.",
             "Read dependency changes before accepting a native transaction.",
             "If a group fails or is cancelled, later groups are skipped.", ""]
    if purge_apt:
        lines += ["APT PURGE: package-managed system configuration will also be removed.",
                  "That configuration is not backed up by this app.", ""]
    for group in batch_groups(apps):
        first = group[0]
        lines += [f"{first.manager} / {first.scope} ({len(group)})"]
        lines += ["  " + a.name + "  [" + a.ident + "]" for a in group]
        argv = group_command(group, purge_apt=purge_apt)
        lines += ["", shlex.join((["sudo"] if first.manager in SYSTEM else []) + argv)
                  if argv else "Move the selected AppImage files to recoverable backup", ""]
        preview = group_command(group, preview=True, purge_apt=purge_apt)
        if preview:
            try:
                result = subprocess.run(preview, capture_output=True, text=True,
                                        errors="replace", env=ENV, timeout=45)
                lines += [(result.stdout + result.stderr)[-22000:]]
                if result.returncode:
                    lines += [f"Preview exit {result.returncode}; check the terminal transaction. DNF --assumeno declines by design."]
            except Exception as exc:
                lines += [f"Preview unavailable: {exc}. Check the native transaction before confirming."]
        else:
            lines += ["This manager has no separate dry-run here."]
        lines += [""]
    return "\n".join(lines)


def run_batch(job_path):
    """The GUI writes a private, reviewed selection. No arbitrary commands in jobs."""
    if os.geteuid() == 0:
        raise RuntimeError("Run the app as your normal user, without sudo.")
    path = Path(job_path)
    st = path.lstat()
    parent = path.parent.stat()
    if (path.resolve() != path or not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or st.st_mode & 0o077 or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        raise ValueError("Batch job must be a private file in a private directory you own.")
    job = json.loads(path.read_text())
    if job.get("version") != 1 or not job.get("reviewed") or not 1 <= len(job.get("apps", [])) <= 1000:
        raise ValueError("Invalid batch selection")
    apps = list({a.key: a for a in (App(**a) for a in job["apps"])}.values())
    receipt = path.parent / "result.json"
    result = {"state": "running", "pid": os.getpid(), "completed": 0,
              "total": len(apps), "message": "Checking selection", "results": []}
    atomic_json(receipt, result)
    recorded = set()
    def record(app, status, message):
        if app.key not in recorded:
            recorded.add(app.key)
            result["results"].append({"app": dataclasses.asdict(app), "status": status, "message": message})
            result["completed"] = len(recorded)
    try:
        with lock_operation():
            groups = batch_groups(apps)
            for group in groups:
                first = group[0]
                current = {a.key: a for a in inventory_for(first.manager, first.scope, [a.ident for a in group])}
                if any(a.key not in current for a in group):
                    raise RuntimeError("The installed selection changed. Refresh and review again.")
                fresh = [current[a.key] for a in group]
                argv = group_command(fresh, purge_apt=job.get("purge_apt") is True)
                result["message"] = f"Removing {len(group)} item(s) with {first.manager} ({first.scope})"
                atomic_json(receipt, result)
                print("\n" + result["message"], flush=True)
                if argv:
                    if first.manager in SYSTEM:
                        sudo = tool("sudo", True)
                        if not sudo:
                            raise RuntimeError("sudo is required for system package removal.")
                        argv = [sudo, "--"] + argv
                    print(shlex.join(argv), flush=True)
                    code = subprocess.call(argv, env=ENV)
                    errors = [] if code == 0 else [f"Native command exited with status {code}"]
                else:
                    _, errors = backup_paths([a.ident for a in fresh], "AppImage batch", [a.ident for a in fresh])
                remaining = {a.key for a in inventory_for(first.manager, first.scope, [a.ident for a in group])}
                for app in fresh:
                    if app.key not in remaining:
                        record(app, "removed", "Verified absent after the native transaction")
                    else:
                        record(app, "failed", "Still installed; native removal was cancelled or incomplete")
                atomic_json(receipt, result)
                if errors or any(a.key in remaining for a in fresh):
                    raise RuntimeError("; ".join(errors) or "Removal was cancelled or incomplete")
            result["message"] = "Batch finished. Review leftovers for removed applications."
    except (Exception, KeyboardInterrupt) as exc:
        result["message"] = str(exc) or "Interrupted"
    finally:
        for app in apps:
            record(app, "skipped", "Batch stopped before this target could be verified")
        result["state"] = "done"
        result["success"] = all(r["status"] == "removed" for r in result["results"])
        atomic_json(receipt, result)
    print("\n" + result["message"], flush=True)
    # Do not keep the terminal waiting for another per-app confirmation.
    return 0 if result["success"] else 1


ICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
<rect x="4" y="4" width="120" height="120" rx="30" fill="#172c2b"/>
<path d="M34 42h60v45c0 10-8 17-18 17H52c-10 0-18-7-18-17Z" fill="#b5ef73"/>
<path d="M49 28h30v14H49z" fill="#b5ef73"/>
<path d="M51 64h26" stroke="#172c2b" stroke-width="8" stroke-linecap="round"/>
</svg>'''


def desktop_quote(value):
    value = str(value)
    if any(c in value for c in "\n\r\t"):
        raise ValueError("Launcher paths cannot contain control characters")
    # First the Exec quoting layer, then the desktop file string-value layer.
    escaped = "".join("\\" + c if c in '\\"`$' else c for c in value)
    return '"' + escaped.replace("\\", "\\\\").replace("%", "%%") + '"'


def launcher_python():
    choices = ["/usr/bin/python3", sys.executable, shutil.which("python3")]
    for candidate in dict.fromkeys(c for c in choices if c):
        if Path(candidate).is_file():
            try:
                check = subprocess.run([candidate, "-c", "import sys, tkinter; assert sys.version_info >= (3, 9)"],
                                       capture_output=True, timeout=10)
                if check.returncode == 0:
                    return str(Path(candidate).absolute())
            except (OSError, subprocess.TimeoutExpired):
                pass
    raise RuntimeError("Install Python 3.9+ with Tk first: python3-tk (Debian/Mint), python + tk (Arch), or python3-tkinter (Fedora).")


def install_launcher():
    if os.geteuid() == 0:
        raise RuntimeError("Install the launcher as your normal user.")
    interpreter = launcher_python()
    target = private_store() / "pocket_uninstaller.py"
    if target.is_symlink():
        raise ValueError("Launcher target cannot be a symlink")
    if Path(__file__).resolve() != target:
        temporary = target.with_suffix(".new")
        if temporary.is_symlink():
            raise ValueError("Unsafe temporary launcher path")
        shutil.copyfile(__file__, temporary)
        os.replace(temporary, target)
    target.chmod(0o700)
    custom = os.environ.get("XDG_DATA_HOME", "")
    data = Path(custom) if custom and Path(custom).is_absolute() else HOME / ".local/share"
    apps = data.resolve() / "applications"
    if apps.is_symlink():
        raise ValueError("Applications directory cannot be a symlink")
    apps.mkdir(parents=True, exist_ok=True)
    if apps.stat().st_uid != os.getuid():
        raise ValueError("Application menu directory must be owned by you")
    icon = private_store() / "pocket-uninstaller.svg"
    if icon.is_symlink():
        raise ValueError("Icon target cannot be a symlink")
    icon.write_text(ICON_SVG)
    desktop = apps / "pocket-uninstaller.desktop"
    if desktop.is_symlink():
        raise ValueError("Desktop entry cannot be a symlink")
    desktop.write_text("[Desktop Entry]\nType=Application\nName=Pocket Uninstaller\n"
                       "Comment=Review and remove Linux applications\n"
                       f"Exec={desktop_quote(interpreter)} {desktop_quote(target)} --desktop-launch\n"
                       f"Icon={str(icon).replace(chr(92), chr(92) * 2)}\n"
                       "Terminal=false\nCategories=System;Utility;\nStartupWMClass=PocketUninstaller\n"
                       "Keywords=uninstall;remove;packages;cleanup;\n")
    desktop.chmod(0o644)
    updater = tool("update-desktop-database")
    if updater:
        try:
            subprocess.run([updater, str(apps)], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    message = f"Installed version {VERSION}. Search your application menu for Pocket Uninstaller.\n\n{desktop}\n\nIf an old instance is open, close it and relaunch. Your backups have been preserved."
    print(message)
    return message



class PocketUI:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk, messagebox, filedialog
        from tkinter.scrolledtext import ScrolledText
        self.tk, self.ttk, self.dialog, self.files, self.Text = tk, ttk, messagebox, filedialog, ScrolledText
        self.root = root
        self.apps, self.visible, self.chosen, self.extra, self.warnings, self.removed = [], {}, {}, [], [], []
        self.source, self.busy, self.anchor = "All applications", False, None
        self.events = queue.Queue()
        self.bg, self.ink, self.muted, self.green, self.lime = "#f5f5ef", "#19332f", "#67776e", "#193c34", "#c8ef9b"
        root.title(f"{TITLE}  {VERSION}")
        root.geometry("1100x740")
        root.minsize(900, 620)
        root.configure(bg=self.bg)
        root.option_add("*Font", ("DejaVu Sans", 10))
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", foreground=self.ink,
                        rowheight=42, borderwidth=0, font=("DejaVu Sans", 10))
        style.configure("Treeview.Heading", background="#eef1e9", foreground=self.muted,
                        font=("DejaVu Sans", 9, "bold"), padding=(8, 11), relief="flat")
        style.map("Treeview", background=[("selected", "#e4f2d5")], foreground=[("selected", self.ink)])
        style.map("Treeview.Heading", background=[("active", "#e4eadf")])
        style.configure("Vertical.TScrollbar", background="#d9e1d2", troughcolor="#f6f7f3", borderwidth=0, arrowsize=13)
        style.configure("TProgressbar", background=self.green, troughcolor="#e4e9dd", borderwidth=0)
        self.side = tk.Frame(root, bg=self.green, width=212)
        self.side.pack(side="left", fill="y")
        self.side.pack_propagate(False)
        mark = tk.Canvas(self.side, width=47, height=47, bg=self.green, highlightthickness=0)
        mark.pack(anchor="w", padx=24, pady=(28, 12))
        mark.create_rectangle(3, 9, 42, 44, fill=self.lime, outline="")
        mark.create_rectangle(13, 1, 32, 10, fill=self.lime, outline="")
        mark.create_line(15, 26, 30, 26, fill=self.green, width=4)
        tk.Label(self.side, text="pocket", font=("DejaVu Sans", 24, "bold"), bg=self.green, fg="#f4f9ec").pack(anchor="w", padx=24)
        tk.Label(self.side, text="UNINSTALLER  /  0.2", font=("DejaVu Sans", 8), bg=self.green, fg="#b4c8b8").pack(anchor="w", padx=26, pady=(1, 26))
        self.nav_canvas = tk.Canvas(self.side, bg=self.green, highlightthickness=0, height=200)
        self.nav = tk.Frame(self.nav_canvas, bg=self.green)
        nav_item = self.nav_canvas.create_window((0, 0), window=self.nav, anchor="nw")
        self.nav.bind("<Configure>", lambda e: self.nav_canvas.configure(scrollregion=self.nav_canvas.bbox("all")))
        self.nav_canvas.bind("<Configure>", lambda e: self.nav_canvas.itemconfigure(nav_item, width=e.width))
        bottom = tk.Frame(self.side, bg=self.green)
        bottom.pack(side="bottom", fill="x", padx=18, pady=22)
        self.button(bottom, "Install / repair launcher", self.install, dark=True).pack(fill="x", pady=(0, 6))
        self.button(bottom, "Backups & restore", self.backups, dark=True).pack(fill="x", pady=3)
        self.button(bottom, "Scan notes", self.notes, dark=True).pack(fill="x", pady=3)
        tk.Label(bottom, text="Small footprint.\nMore room for what matters.", justify="left", bg=self.green,
                 fg="#b4c8b8", font=("DejaVu Sans", 9)).pack(anchor="w", pady=(22, 0))
        self.nav_canvas.pack(fill="both", expand=True, padx=14)
        main = tk.Frame(root, bg=self.bg)
        main.pack(side="left", fill="both", expand=True, padx=28, pady=26)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)
        tk.Label(main, text="YOUR SYSTEM  /  APPLICATIONS", bg=self.bg, fg=self.muted,
                 font=("DejaVu Sans", 8, "bold")).grid(row=0, column=0, sticky="w")
        top = tk.Frame(main, bg=self.bg)
        top.grid(row=1, column=0, sticky="ew", pady=(10, 3))
        tk.Label(top, text="A little less clutter.", bg=self.bg, fg=self.ink,
                 font=("DejaVu Sans", 25, "bold")).pack(side="left")
        self.total = tk.StringVar(value="READING...")
        tk.Label(top, textvariable=self.total, bg="#e4ebd9", fg=self.green,
                 font=("DejaVu Sans", 9, "bold"), padx=12, pady=8).pack(side="right")
        tk.Label(main, text="Pick your apps. Review once. Let each package manager handle the rest.",
                 bg=self.bg, fg=self.muted, font=("DejaVu Sans", 10), wraplength=620, justify="left").grid(row=2, column=0, sticky="w", pady=(1, 22))
        row = tk.Frame(main, bg=self.bg)
        row.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        searchbox = tk.Frame(row, bg="white", highlightbackground="#d5ded0", highlightthickness=1)
        searchbox.pack(side="left", fill="x", expand=True)
        tk.Label(searchbox, text="Search", bg="white", fg=self.muted, padx=12).pack(side="left")
        self.search = tk.StringVar()
        self.entry = tk.Entry(searchbox, textvariable=self.search, relief="flat", borderwidth=0,
                              bg="white", fg=self.ink, insertbackground=self.ink, font=("DejaVu Sans", 11))
        self.entry.pack(side="left", fill="x", expand=True, ipady=11)
        tk.Button(searchbox, text="×", command=lambda: self.search.set(""), bg="white", fg=self.muted,
                  relief="flat", borderwidth=0, cursor="hand2").pack(side="right", padx=7)
        self.refresh_button = self.button(row, "Refresh", self.refresh)
        self.refresh_button.pack(side="right", padx=(10, 0))
        actions = tk.Frame(main, bg=self.bg)
        actions.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        self.button(actions, "Select visible", self.select_visible, compact=True).pack(side="left")
        self.button(actions, "Clear selection", self.clear, compact=True).pack(side="left", padx=6)
        self.button(actions, "+ AppImage", self.add_image, compact=True).pack(side="right")
        tk.Label(actions, text="↑ ↓ navigate   ·   Space select", bg=self.bg, fg=self.muted,
                 font=("DejaVu Sans", 8)).pack(side="right", padx=8)
        frame = tk.Frame(main, bg="white", highlightbackground="#dce3d6", highlightthickness=1)
        frame.grid(row=5, column=0, sticky="nsew")
        self.tree = ttk.Treeview(frame, columns=("check", "name", "source", "version", "scope"),
                                 show="headings", selectmode="none", takefocus=True)
        specs = [("check", "", 42, False), ("name", "APPLICATION / PACKAGE", 240, True),
                 ("source", "SOURCE", 90, False), ("version", "VERSION", 110, True), ("scope", "SCOPE", 85, False)]
        for key, title, width, stretch in specs:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=35 if key == "check" else 55,
                             stretch=stretch, anchor="center" if key == "check" else "w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("protected", foreground="#8b958c")
        self.tree.tag_configure("active", background="#eef1e9")
        self.tree.tag_configure("odd", background="#fafbf8")
        self.empty = tk.Label(frame, text="Reading installed applications...", bg="white", fg=self.muted)
        self.empty.place(relx=.5, rely=.5, anchor="center")
        self.detail = tk.StringVar(value="Protected system components cannot be selected.")
        tk.Label(main, textvariable=self.detail, bg=self.bg, fg=self.muted,
                 anchor="w", font=("DejaVu Sans", 9)).grid(row=6, column=0, sticky="ew", pady=(9, 12))
        bar = tk.Frame(main, bg="#e7eedf", padx=15, pady=13)
        bar.grid(row=7, column=0, sticky="ew")
        text = tk.Frame(bar, bg="#e7eedf")
        text.pack(side="left")
        self.count = tk.StringVar(value="No apps selected")
        self.hint = tk.StringVar(value="Selection stays with you while you search.")
        tk.Label(text, textvariable=self.count, bg="#e7eedf", fg=self.ink,
                 font=("DejaVu Sans", 11, "bold")).pack(anchor="w")
        tk.Label(text, textvariable=self.hint, bg="#e7eedf", fg=self.muted,
                 font=("DejaVu Sans", 8), wraplength=290, justify="left").pack(anchor="w", pady=(3, 0))
        self.remove_button = self.button(bar, "Review & uninstall", self.review, primary=True)
        self.remove_button.pack(side="right")
        self.leftover_button = self.button(main, "Review leftovers from completed removals", self.cleanup_dialog, compact=True)
        self.leftover_button.grid(row=8, column=0, sticky="w", pady=(10, 0))
        self.leftover_button.configure(state="disabled")
        self.status = tk.StringVar(value="Ready")
        tk.Label(main, textvariable=self.status, bg=self.bg, fg=self.muted,
                 font=("DejaVu Sans", 8), anchor="w").grid(row=9, column=0, sticky="ew", pady=(8, 0))
        self.tree.bind("<Button-1>", self.click)
        self.tree.bind("<Double-1>", lambda e: "break")
        self.tree.bind("<space>", self.toggle_focus)
        for key in ("Up", "Down", "Prior", "Next", "Home", "End"):
            self.tree.bind("<" + key + ">", lambda e, k=key: self.navigate(k, bool(e.state & 1)))
            self.tree.bind("<Shift-" + key + ">", lambda e, k=key: self.navigate(k, True))
        self.tree.bind("<Control-a>", lambda e: self.select_visible())
        self.tree.bind("<Escape>", lambda e: self.clear())
        self.tree.bind("<Return>", lambda e: self.review())
        self.tree.bind("<Button-4>", lambda e: self.wheel(-3))
        self.tree.bind("<Button-5>", lambda e: self.wheel(3))
        self.tree.bind("<MouseWheel>", lambda e: self.wheel(-1 if e.delta > 0 else 1))
        self.entry.bind("<Down>", lambda e: self.focus_list())
        root.bind("<Control-f>", lambda e: self.entry.focus_set())
        root.bind("<F5>", lambda e: self.refresh())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.search.trace_add("write", lambda *_: self.render())
        self.draw_nav()
        self.pump()
        self.refresh()

    def button(self, parent, text, command, primary=False, dark=False, compact=False):
        return self.tk.Button(parent, text=text, command=command, relief="flat", borderwidth=0,
                              bg=self.green if primary else ("#284a40" if dark else "#e7ecdf"),
                              fg="#f5f9ed" if primary or dark else self.ink,
                              activebackground=self.lime, activeforeground=self.ink,
                              disabledforeground="#91a18f", font=("DejaVu Sans", 9, "bold"),
                              padx=12, pady=6 if compact else 11, cursor="hand2", takefocus=True)

    def draw_nav(self):
        for w in self.nav.winfo_children():
            w.destroy()
        sources = ["All applications", "Selected"] + sorted({a.manager for a in self.apps})
        for source in sources:
            n = len(self.apps) if source == "All applications" else (len(self.chosen) if source == "Selected" else sum(a.manager == source for a in self.apps))
            button = self.button(self.nav, f"{source}   {n}", lambda s=source: self.set_source(s), dark=True)
            button.configure(anchor="w", font=("DejaVu Sans", 9), pady=8,
                             bg=self.lime if source == self.source else self.green,
                             fg=self.green if source == self.source else "#d4e1d3")
            button.pack(fill="x", pady=2)
            button.bind("<Button-4>", lambda e: self.nav_canvas.yview_scroll(-2, "units"))
            button.bind("<Button-5>", lambda e: self.nav_canvas.yview_scroll(2, "units"))

    def set_source(self, source):
        self.source = source
        self.draw_nav()
        self.render()

    def render(self):
        focused = self.visible.get(self.tree.focus())
        self.tree.delete(*self.tree.get_children())
        query = self.search.get().casefold()
        self.visible = {}
        for n, app in enumerate(self.apps):
            if self.source == "Selected" and app.key not in self.chosen:
                continue
            if self.source not in ("Selected", "All applications", app.manager):
                continue
            if query not in (app.name + " " + app.ident + " " + app.note).casefold():
                continue
            iid = str(n)
            self.visible[iid] = app
            self.tree.insert("", "end", iid=iid, values=("", app.name, app.manager, app.version, app.scope))
            if focused and focused.key == app.key:
                self.tree.focus(iid)
        children = self.tree.get_children()
        if not self.tree.focus() and children:
            self.tree.focus(children[0])
        if children:
            self.empty.place_forget()
        else:
            self.empty.configure(text="No matching apps. Try another search or source.")
            self.empty.place(relx=.5, rely=.5, anchor="center")
        self.paint()

    def paint(self):
        selected = []
        focus = self.tree.focus()
        for n, (iid, app) in enumerate(self.visible.items()):
            checked = app.key in self.chosen
            self.tree.set(iid, "check", "−" if guarded(app) else ("☑" if checked else "☐"))
            self.tree.item(iid, tags=("protected",) if guarded(app) else (("active",) if iid == focus else (("odd",) if n % 2 else ())))
            if checked:
                selected.append(iid)
        self.tree.selection_set(selected)
        n = len(self.chosen)
        hidden = n - len(selected)
        self.count.set(f"{n} app{'s' if n != 1 else ''} selected" if n else "No apps selected")
        self.hint.set(f"{hidden} selected outside this view. Review shows everything." if hidden else "Selection stays with you while you search.")
        self.remove_button.configure(state="normal" if n and not self.busy else "disabled")
        app = self.visible.get(focus)
        if app:
            self.detail.set(("Protected · " if guarded(app) else "") + app.ident[:85])
        self.total.set(f"{len(self.visible)} / {len(self.apps)} ITEMS")

    def click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid or self.tree.identify_region(event.x, event.y) == "heading":
            return
        self.tree.focus_set()
        self.tree.focus(iid)
        if not self.busy:
            if event.state & 1 and self.anchor in self.visible:
                self.select_range(self.anchor, iid)
            else:
                self.toggle(iid)
                self.anchor = iid
        self.paint()
        return "break"

    def toggle(self, iid):
        app = self.visible.get(iid)
        if app and not guarded(app):
            if app.key in self.chosen:
                self.chosen.pop(app.key)
            else:
                self.chosen[app.key] = app
        self.draw_nav()

    def select_range(self, start, end):
        ids = list(self.visible)
        a, b = sorted((ids.index(start), ids.index(end)))
        for iid in ids[a:b + 1]:
            app = self.visible[iid]
            if not guarded(app):
                self.chosen[app.key] = app
        self.draw_nav()

    def navigate(self, key, extend=False):
        ids = list(self.visible)
        if not ids:
            return "break"
        current = self.tree.focus()
        at = ids.index(current) if current in ids else 0
        page = max(1, self.tree.winfo_height() // 42 - 1)
        new = {"Up": at - 1, "Down": at + 1, "Prior": at - page, "Next": at + page,
               "Home": 0, "End": len(ids) - 1}[key]
        target = ids[max(0, min(new, len(ids) - 1))]
        self.tree.focus_set()
        self.tree.focus(target)
        self.tree.see(target)
        if extend and not self.busy:
            if self.anchor not in self.visible:
                self.anchor = current if current in self.visible else target
            self.select_range(self.anchor, target)
        elif not extend:
            self.anchor = target
        self.paint()
        return "break"

    def focus_list(self):
        self.tree.focus_set()
        ids = self.tree.get_children()
        if ids:
            self.tree.focus(ids[0])
            self.tree.see(ids[0])
            self.paint()
        return "break"

    def toggle_focus(self, event=None):
        if not self.busy:
            self.toggle(self.tree.focus())
            self.paint()
        return "break"

    def select_visible(self):
        if not self.busy:
            self.chosen.update({a.key: a for a in self.visible.values() if not guarded(a)})
            self.draw_nav()
            self.paint()
        return "break"

    def clear(self):
        if not self.busy:
            self.chosen.clear()
            self.draw_nav()
            self.render()
        return "break"

    def wheel(self, amount):
        self.tree.yview_scroll(amount, "units")
        return "break"

    def set_busy(self, busy):
        self.busy = busy
        self.refresh_button.configure(state="disabled" if busy else "normal")
        self.paint()

    def work(self, function, callback):
        if self.busy:
            return
        self.set_busy(True)
        def run():
            try:
                self.events.put((callback, function(), None))
            except Exception as exc:
                self.events.put((callback, None, str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def pump(self):
        try:
            while True:
                callback, value, error = self.events.get_nowait()
                self.set_busy(False)
                if error:
                    self.status.set("Could not complete operation")
                    self.dialog.showerror(TITLE, error, parent=self.root)
                else:
                    callback(value)
        except queue.Empty:
            pass
        self.root.after(80, self.pump)

    def refresh(self):
        if self.busy:
            return
        self.status.set("Reading installed applications...")
        self.work(lambda: scan_all(self.extra), self.scanned)

    def scanned(self, data):
        self.apps, self.warnings = data
        current = {a.key: a for a in self.apps}
        self.chosen = {k: current[k] for k in self.chosen if k in current and not guarded(current[k])}
        self.draw_nav()
        self.render()
        self.status.set(f"{len(self.apps)} installed entries · {len(self.warnings)} scan warning(s) · Ctrl+F to search")

    def text_window(self, title, text):
        window = self.tk.Toplevel(self.root)
        window.title(title)
        window.geometry("830x610")
        window.minsize(640, 440)
        window.configure(bg=self.bg)
        window.transient(self.root)
        box = self.Text(window, wrap="word", bg="white", fg=self.ink, relief="flat",
                        font=("DejaVu Sans Mono", 10), padx=16, pady=16)
        box.pack(fill="both", expand=True, padx=18, pady=18)
        box.insert("1.0", text)
        box.configure(state="disabled")
        return window

    def review(self, purge_apt=False):
        if self.busy or not self.chosen:
            return "break"
        apps = list(self.chosen.values())
        self.status.set(f"Building one review for {len(apps)} selected apps...")
        def ready(text):
            window = self.text_window(f"Review {len(apps)} selected apps", text)
            window.grab_set()
            bar = self.tk.Frame(window, bg=self.bg)
            bar.pack(fill="x", padx=18, pady=(0, 18))
            self.button(bar, "Back to selection", window.destroy).pack(side="left")
            if any(a.manager == "APT" for a in apps) and not purge_apt:
                def purge_review():
                    window.destroy()
                    self.review(True)
                self.button(bar, "Review APT purge instead", purge_review).pack(side="left", padx=8)
            self.button(bar, f"Uninstall {len(apps)} selected", lambda: self.execute(apps, window, purge_apt), primary=True).pack(side="right")
        self.work(lambda: batch_preview(apps, purge_apt), ready)
        return "break"

    def execute(self, apps, window, purge_apt=False):
        if self.busy:
            return
        folder = Path(tempfile.mkdtemp(prefix="pocket-batch-"))
        job = folder / "job.json"
        atomic_json(job, {"version": 1, "reviewed": True, "purge_apt": purge_apt,
                          "apps": [dataclasses.asdict(a) for a in apps]})
        command = [sys.executable, str(Path(__file__).resolve()), "--run-batch", str(job)]
        try:
            subprocess.Popen(terminal_command(command), env=os.environ.copy())
        except Exception as exc:
            window.grab_release()
            fallback = self.text_window("Run the reviewed batch in your terminal", str(exc) + "\n\n" + shlex.join(command))
            self.button(fallback, "Copy command", lambda: self.copy(shlex.join(command))).pack(pady=(0, 14))
        window.destroy()
        self.set_busy(True)
        self.status.set("Batch started. Complete any native confirmation in the terminal.")
        started = time.monotonic()
        receipt = folder / "result.json"
        def poll():
            try:
                result = json.loads(receipt.read_text()) if receipt.exists() else None
                if result and result.get("state") == "done":
                    self.set_busy(False)
                    self.batch_finished(result)
                    return
                if result:
                    self.status.set(f"{result['completed']} / {result['total']} checked · {result['message']}")
                    try:
                        os.kill(result["pid"], 0)
                    except ProcessLookupError:
                        self.set_busy(False)
                        self.dialog.showwarning(TITLE, "The runner stopped unexpectedly. Check the terminal before retrying. No unverified leftovers will be offered.")
                        self.refresh()
                        return
                elif time.monotonic() - started > 180:
                    self.set_busy(False)
                    self.dialog.showwarning(TITLE, "The batch runner has not started. Close any delayed terminal before retrying. Your selection is preserved.")
                    return
            except (OSError, ValueError):
                pass
            self.root.after(400, poll)
        poll()

    def batch_finished(self, result):
        successes = [App(**r["app"]) for r in result["results"] if r["status"] == "removed"]
        self.removed = list({a.key: a for a in self.removed + successes}.values())
        for app in successes:
            self.chosen.pop(app.key, None)
        self.leftover_button.configure(state="normal" if self.removed else "disabled")
        summary = [result["message"], "", f"{len(successes)} removed / {len(result['results'])} selected", ""]
        summary += [f"{r['status'].upper()}  {r['app']['name']} ({r['app']['manager']})\n{r['message']}\n" for r in result["results"]]
        window = self.text_window("Batch results", "\n".join(summary))
        if successes:
            self.button(window, "Review leftover folders", self.cleanup_dialog, primary=True).pack(pady=(0, 16))
        self.refresh()

    def cleanup_dialog(self):
        if self.busy or not self.removed:
            return
        apps = list(self.removed)
        def find():
            rows, notes = [], []
            for app in apps:
                try:
                    ensure_cleanup_eligible(app)
                    rows.extend((app, path) for path in candidates(app))
                except Exception as exc:
                    notes.append(f"{app.name}: {exc}")
            return rows, notes
        def ready(data):
            rows, notes = data
            window = self.tk.Toplevel(self.root)
            window.title("Review leftovers")
            window.geometry("860x480")
            window.configure(bg=self.bg)
            self.tk.Label(window, text="Choose only folders you recognize. Nothing is selected automatically.\nSelected paths move to recoverable backup; disk space stays in that backup.",
                          bg=self.bg, fg=self.ink, justify="left").pack(anchor="w", padx=20, pady=18)
            listing = self.tk.Listbox(window, selectmode="multiple", exportselection=False, bg="white", fg=self.ink,
                                      selectbackground="#d9edc1", selectforeground=self.ink)
            listing.pack(fill="both", expand=True, padx=20)
            for app, path in rows:
                listing.insert("end", f"{app.name}  ·  {path}")
            if not rows:
                listing.insert("end", "No exact-name leftover paths were found.")
                listing.configure(state="disabled")
            if notes:
                self.tk.Label(window, text="Some shared/still-installed data was skipped. See Scan notes.", bg=self.bg, fg=self.muted).pack()
                self.warnings.extend(notes)
            def move():
                chosen = [rows[i] for i in listing.curselection()] if rows else []
                if not chosen or self.busy:
                    return
                if not self.dialog.askyesno(TITLE, "Move these paths to backup?\n\n" + "\n".join(str(path) for _, path in chosen), parent=window):
                    return
                def run():
                    with lock_operation():
                        allowed = set()
                        for app in dict((a.key, a) for a, _ in chosen).values():
                            ensure_cleanup_eligible(app)
                            allowed.update(candidates(app))
                        return backup_paths([path for _, path in chosen], "Batch leftovers", allowed)
                def done(value):
                    folder, errors = value
                    window.destroy()
                    self.dialog.showinfo(TITLE, "Backup created:\n" + str(folder) + "\n\n" + "\n".join(errors))
                self.work(run, done)
            self.button(window, "Move selected to backup", move, primary=True).pack(pady=18)
        self.work(find, ready)

    def backups(self):
        if self.busy:
            return
        try:
            folders = sorted(private_store().glob("backup-*"), reverse=True)
        except Exception as exc:
            self.dialog.showerror(TITLE, str(exc))
            return
        window = self.tk.Toplevel(self.root)
        window.title("Backups & restore")
        window.geometry("760x420")
        window.configure(bg=self.bg)
        self.tk.Label(window, text="Restore moved files without overwriting existing data.\nBackups occupy disk space until you delete their folders manually.",
                      bg=self.bg, fg=self.ink, justify="left").pack(anchor="w", padx=20, pady=18)
        listing = self.tk.Listbox(window, bg="white", fg=self.ink, exportselection=False)
        listing.pack(fill="both", expand=True, padx=20)
        for folder in folders:
            try:
                label = json.loads((folder / "manifest.json").read_text())["label"]
            except Exception:
                label = "Unreadable manifest"
            listing.insert("end", f"{folder.name}  ·  {label}")
        bar = self.tk.Frame(window, bg=self.bg)
        bar.pack(fill="x", padx=20, pady=18)
        self.button(bar, "Copy backup location", lambda: self.copy(str(STORE))).pack(side="left")
        def restore():
            if not listing.curselection() or self.busy:
                return
            folder = folders[listing.curselection()[0]]
            if not self.dialog.askyesno(TITLE, "Restore " + folder.name + "?", parent=window):
                return
            def run():
                with lock_operation():
                    return restore_backup(folder)
            def done(value):
                count, errors = value
                self.dialog.showinfo(TITLE, f"Restored {count} path(s).\n" + "\n".join(errors))
                self.refresh()
            self.work(run, done)
        self.button(bar, "Restore selected", restore, primary=True).pack(side="right")

    def install(self):
        if not self.busy:
            self.status.set("Checking Python and repairing the application menu entry...")
            self.work(install_launcher, lambda message: self.dialog.showinfo("Launcher ready", message))

    def notes(self):
        self.text_window("Scan notes", "\n\n".join(self.warnings) if self.warnings else
                         "No scan errors.\n\nSupported: APT, Pacman, DNF/Zypper, Flatpak, Snap, pipx, Homebrew and AppImages.\n\nSystem inventories include libraries and dependencies. Arbitrary installer scripts, Nix, containers and other users' private apps are not indexed.\n\nKeyboard: arrows move focus; Space toggles; Shift+arrows selects a range; Ctrl+A selects visible removable items; Escape clears; Page Up/Down and Home/End navigate. From search, Down enters the list.")

    def add_image(self):
        if self.busy:
            return
        path = self.files.askopenfilename(filetypes=[("AppImage", "*.AppImage *.appimage"), ("All files", "*")])
        if path:
            if appimage_valid(path):
                self.extra.append(path)
                self.refresh()
            else:
                self.dialog.showerror(TITLE, "Choose a regular AppImage file inside your home. Symlinked files are excluded.")

    def copy(self, value):
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.status.set("Copied")

    def close(self):
        if self.busy and not self.dialog.askyesno(TITLE, "An operation is running. Closing the window will not cancel a native transaction. Close anyway?"):
            return
        self.root.destroy()


def gui():
    try:
        import tkinter as tk
    except ImportError:
        raise RuntimeError("Tk is missing. Install python3-tk (Debian/Mint), tk (Arch), or python3-tkinter (Fedora).")
    if os.geteuid() == 0:
        raise RuntimeError("Start the GUI as your normal user, without sudo.")
    root = tk.Tk(className="PocketUninstaller")
    PocketUI(root)
    root.mainloop()

def ensure_cleanup_eligible(app):
    if find_current(app.manager, app.ident, app.scope) is not None:
        raise RuntimeError("This application is still installed. Uninstall it before cleanup.")
    if app.manager == "Flatpak":
        app_id = app.ident.split("/")[1]
        for scope in flatpak_installations():
            if any(a.ident.split("/")[1] == app_id for a in scan_flatpak(scope)):
                raise RuntimeError("Another Flatpak installation/branch uses this app ID. Shared data cleanup is blocked.")


def main():
    parser = argparse.ArgumentParser(description=TITLE + " " + VERSION)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--list", action="store_true", help="Print read-only installed inventory as JSON")
    parser.add_argument("--install-launcher", action="store_true", help="Add a menu entry for the current user")
    parser.add_argument("--uninstall", nargs=2, metavar=("MANAGER", "IDENTIFIER"), help="Interactive native removal; never run as root")
    parser.add_argument("--scope", default="system", help="system, user, or installation:NAME for Flatpak")
    parser.add_argument("--purge", action="store_true", help="APT only: also remove system configuration")
    parser.add_argument("--receipt", help=argparse.SUPPRESS)
    parser.add_argument("--run-batch", help=argparse.SUPPRESS)
    parser.add_argument("--desktop-launch", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("This app is for Linux.")
    try:
        if args.list:
            apps, warnings = scan_all()
            print(json.dumps({"version": VERSION, "applications": [dataclasses.asdict(a) for a in apps], "warnings": warnings}, indent=2))
        elif args.install_launcher:
            install_launcher()
        elif args.run_batch:
            return run_batch(args.run_batch)
        elif args.uninstall:
            return run_removal(args)
        else:
            gui()
        return 0
    except Exception as exc:
        print(f"{TITLE}: {exc}", file=sys.stderr)
        if args.desktop_launch:
            try:
                from tkinter import messagebox
                messagebox.showerror(TITLE, str(exc))
            except Exception:
                helper = tool("zenity") or tool("kdialog")
                if helper:
                    flags = ["--error", "--text=" + str(exc)] if Path(helper).name == "zenity" else ["--error", str(exc)]
                    subprocess.run([helper] + flags, timeout=60)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
