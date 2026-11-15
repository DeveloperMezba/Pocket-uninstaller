#!/usr/bin/env python3
"""Pocket Uninstaller 3.3. Python 3.9+, standard library only. MIT license."""
from __future__ import annotations

import argparse
import concurrent.futures
import configparser
import dataclasses
import datetime
from decimal import Decimal, InvalidOperation
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
import weakref

VERSION = "3.3"
TITLE = "Pocket Uninstaller"
HOME = Path.home().resolve()
STORE = HOME / ".local/share/pocket-uninstaller"
ENV = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8", NO_COLOR="1",
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
    size_bytes: int | None = None
    size_kind: str = "unknown"
    size_note: str = "Installed size unavailable"

    @property
    def key(self):
        return self.manager, self.ident, self.scope


def parse_size(value, default_unit="B"):
    """Parse C-locale SI/IEC output, preserving unknown instead of inventing zero."""
    if value is None:
        return None
    # Normalize non-breaking and other unicode spaces
    value = str(value).replace('\u00a0', ' ').replace('\u2007', ' ').replace('\u202f', ' ')
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*", value)
    if not match:
        return None
    unit = (match[2] or default_unit).lower()
    units = {"b": 1, "bytes": 1}
    for power, prefix in enumerate("kmgtpe", 1):
        units[prefix + "b"] = 1000 ** power
        units[prefix + "ib"] = 1024 ** power
    try:
        return int(Decimal(match[1]) * units[unit])
    except (KeyError, InvalidOperation, OverflowError):
        return None


def format_size(value):
    if value is None:
        return "Unknown"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"):
        if amount < 1024 or unit == "EiB":
            return f"{int(amount)} B" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024


def size_total(apps):
    entries = list(apps)
    known = [a.size_bytes for a in entries if a.size_bytes is not None]
    return sum(known), len(entries) - len(known)


def total_label(apps):
    apps = list(apps)
    size, missing = size_total(apps)
    if apps and missing == len(apps):
        return "Unknown", f"{missing} size(s) unavailable"
    return format_size(size), f"{missing} unknown, excluded" if missing else "Reported + measured sizes"


def sort_apps(apps, order):
    if order == "largest":
        return sorted(apps, key=lambda a: (a.size_bytes is None, -(a.size_bytes or 0), a.name.casefold(), a.key))
    if order == "smallest":
        return sorted(apps, key=lambda a: (a.size_bytes is None, a.size_bytes or 0, a.name.casefold(), a.key))
    return sorted(apps, key=lambda a: (a.name.casefold(), a.key))


def allocated_size(path, timeout=2.0, max_entries=100000):
    """Bounded, read-only scan. No symlink traversal; count hardlinks once per tree."""
    start = time.monotonic()
    seen, pending, total = set(), [Path(path)], 0
    try:
        root_stat = Path(path).lstat()
        if stat.S_ISLNK(root_stat.st_mode):
            return None, "The installation path is a symlink; not followed"
        while pending:
            if time.monotonic() - start > timeout or len(seen) >= max_entries:
                return None, "Size scan limit reached; partial data was discarded"
            current = pending.pop()
            st = current.lstat()
            if st.st_dev != root_stat.st_dev:
                return None, "Installation crosses a filesystem; size not combined"
            key = st.st_dev, st.st_ino
            if key in seen:
                continue
            seen.add(key)
            total += st.st_blocks * 512
            if stat.S_ISDIR(st.st_mode):
                with os.scandir(current) as entries:
                    for entry in entries:
                        if len(pending) + len(seen) >= max_entries or time.monotonic() - start > timeout:
                            return None, "Size scan limit reached; partial data was discarded"
                        pending.append(Path(entry.path))
        return total, "Filesystem allocated bytes; symlinks not followed, hardlinks counted once within this app"
    except OSError as exc:
        return None, "Size unavailable: " + str(exc)


def measured_app(app, path, note, timeout=2.0):
    value, explanation = allocated_size(path, timeout=timeout)
    return dataclasses.replace(app, size_bytes=value, size_kind="allocated" if value is not None else "unknown",
                               size_note=note + ". " + explanation)


def measure_apps(apps, path_for, note):
    deadline = time.monotonic() + 8
    result = []
    for app in apps:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result.append(dataclasses.replace(app, size_note="Size scan time budget reached. Refresh to retry."))
        else:
            result.append(measured_app(app, path_for(app), note, min(2, remaining)))
    return result


@dataclasses.dataclass(frozen=True)
class PackageInfo:
    description: str = "No description is available in the installed package metadata."
    publisher: str = "Not provided"
    publisher_role: str = "Developer / publisher"
    category: str = "Unknown"
    category_basis: str = "No category was provided"
    homepage: str = ""
    source: str = "Installed package metadata"
    notes: str = ""


def clean_info(value, limit=16000):
    value = str(value or "").replace("\r", "")
    return ''.join(c for c in value if c in '\n\t' or (ord(c) >= 32 and ord(c) != 127))[:limit].strip()


def package_type(description, section="", categories=()):
    """Conservative labels from metadata. Never guess a vendor from a name."""
    declared = set(categories)
    for key, label in [('WebBrowser', 'Web browser'), ('Game', 'Game'), ('Office', 'Office software'),
                       ('Graphics', 'Graphics software'), ('AudioVideo', 'Media software'),
                       ('Development', 'Development tools'), ('Utility', 'Utility'), ('System', 'System tool')]:
        if key in declared:
            return label, 'Declared desktop category: ' + key
    text = description.split('\n', 1)[0].lower()
    section = section.lower().rsplit('/', 1)[-1]
    if section in ('doc', 'documentation') or re.search(r'\b(documentation|manual pages)\b', text.split('\n')[0]):
        label = 'Documentation'
    elif section in ('libdevel', 'devel') or re.search(r'\b(development files|header files|development headers)\b', text):
        label = 'Development tools / files'
    elif re.search(r'\bfirmware\b', text):
        label = 'Device firmware'
    elif re.search(r'\b(driver|drivers)\b', text):
        label = 'Hardware / graphics driver'
    elif section in ('libs', 'oldlibs') or re.search(r'\b(shared library|shared libraries|runtime library)\b', text):
        label = 'Library / dependency'
    elif re.search(r'\b(web browser|internet browser)\b', text):
        label = 'Web browser'
    elif re.search(r'\b(media player|video player|audio player)\b', text):
        label = 'Media player'
    elif re.search(r'\b(text editor|code editor)\b', text):
        label = 'Text / code editor'
    elif section == 'shells' or re.search(r'\b(command shell|shell|command language interpreter)\b', text):
        label = 'Command shell'
    elif re.search(r'\b(programming language|object.oriented language|language interpreter)\b', text):
        label = 'Programming language / runtime'
    elif section in ('fonts', 'x11-fonts'):
        label = 'Fonts'
    elif section in ('games',):
        label = 'Game'
    elif section in ('admin', 'base', 'kernel'):
        label = 'System component / tool'
    else:
        return 'Unknown', 'No reliable category found in metadata'
    return label + ' (inferred)', 'Inferred from package description / section; not a safety assessment'


def make_info(description="", publisher="", role="Developer / publisher", section="", categories=(), homepage="", source="Installed package metadata", notes=""):
    description = clean_info(description)
    category, basis = package_type(description, section, categories)
    publisher = clean_info(publisher, 500)
    if publisher.lower() in ('(none)', 'none', 'unknown', 'n/a'):
        publisher = ''
    homepage = clean_info(homepage, 2000)
    if not homepage.startswith(('https://', 'http://')):
        homepage = ''
    return PackageInfo(description or PackageInfo.description, publisher or 'Not provided', role,
                       category, basis + (f'; section: {section}' if section else ''), homepage, source, clean_info(notes, 2000))


def read_info_file(path, limit=262144):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('Metadata file is missing or too large')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Metadata file is too large')
    return data.decode('utf-8', errors='replace')


def parse_key_info(output):
    fields, current = {}, None
    for line in output.splitlines():
        if line and not line[0].isspace() and ':' in line:
            current, value = line.split(':', 1)
            current = current.strip()
            fields[current] = value.strip()
        elif current and line[:1].isspace():
            fields[current] += ' ' + line.strip()
    return fields


def flatpak_package_info(app):
    from xml.etree import ElementTree as ET
    location = Path(read_command([tool('flatpak'), 'info', flatpak_scope(app.scope), '--show-location', app.ident], timeout=6).strip())
    if not location.is_absolute():
        raise ValueError('Flatpak did not return an absolute installation path')
    app_id = app.ident.split('/')[1]
    # Match the component ID, never take another bundled component's identity.
    for folder in ('metainfo', 'appdata'):
        directory = location / 'files/share' / folder
        for path in sorted(directory.glob('*.xml'))[:100]:
            raw = read_info_file(path)
            if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
                continue
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                continue
            for node in ([root] if root.tag == 'component' else root.findall('component')):
                if node.findtext('id', '').removesuffix('.desktop') != app_id:
                    continue
                def local(tag):
                    return next((x for x in node.findall(tag) if not x.get('{http://www.w3.org/XML/1998/namespace}lang')), None)
                def words(element):
                    return ' '.join(element.itertext()).strip() if element is not None else ''
                description = '\n\n'.join(filter(None, [words(local('summary')), words(local('description'))]))
                developer = words(local('developer/name')) or words(local('developer_name'))
                url = next((x.text for x in node.findall('url') if x.get('type') == 'homepage'), '')
                return make_info(description, developer, 'Developer', categories=[x.text for x in node.findall('categories/category')], homepage=url,
                                 source='Installed Flatpak AppStream metadata', notes=app.note)
    return make_info(source='Installed Flatpak metadata', notes='No matching local AppStream description was found. ' + app.note)


def snap_package_info(app):
    import http.client
    import socket
    from urllib.parse import quote
    class SnapConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect('/run/snapd.socket')
    connection = SnapConnection('localhost', timeout=4)
    try:
        connection.request('GET', '/v2/snaps/' + quote(app.ident, safe=''))
        response = connection.getresponse()
        body = response.read(262145)
        if response.status != 200 or len(body) > 262144:
            raise ValueError('Local snapd metadata is unavailable')
        data = json.loads(body)['result']
        publisher = data.get('publisher') or {}
        name = publisher.get('display-name') or publisher.get('username') or data.get('developer', '')
        description = '\n\n'.join(filter(None, [data.get('summary'), data.get('description')]))
        return make_info(description, name, 'Snap publisher', homepage=data.get('website', ''),
                         source='Local snapd installed-snap metadata', notes='Snap type: ' + data.get('type', 'unknown') + '. Publisher may be a third-party packager.')
    finally:
        connection.close()


def pipx_package_info(app):
    from email.parser import Parser
    data = json.loads(read_command([tool('pipx'), 'list', '--json'], timeout=6))
    package = data['venvs'][app.ident]['metadata']['main_package'].get('package', app.ident)
    norm = lambda text: re.sub(r'[-_.]+', '-', text).lower()
    root = Path(read_command([tool('pipx'), 'environment', '--value', 'PIPX_LOCAL_VENVS'], timeout=6).strip())
    if not root.is_absolute() or '/' in app.ident:
        raise ValueError('Unexpected pipx installation path')
    for path in sorted((root / app.ident / 'lib').glob('python*/site-packages/*.dist-info/METADATA'))[:1000]:
        message = Parser().parsestr(read_info_file(path))
        if norm(message.get('Name', '')) != norm(package):
            continue
        publisher = message.get('Author') or message.get('Author-email') or ''
        role = 'Author'
        if not publisher:
            publisher = message.get('Maintainer') or message.get('Maintainer-email') or ''
            role = 'Maintainer'
        homepage = message.get('Home-page', '')
        for url in message.get_all('Project-URL', []):
            label, sep, value = url.partition(',')
            if sep and label.strip().lower() == 'homepage':
                homepage = value.strip()
        return make_info(message.get('Summary', ''), publisher, role, homepage=homepage,
                         source='Installed Python distribution METADATA', notes='pipx environment: ' + app.ident)
    return make_info(source='Installed pipx environment', notes='No matching distribution metadata was found.')


def brew_package_info(app):
    cellar = Path(read_command([tool('brew'), '--cellar'], timeout=6).strip())
    if not cellar.is_absolute():
        raise ValueError('Unexpected Homebrew Cellar path')
    name = app.ident.rsplit('/', 1)[-1]
    # Read the installed formula as text; never execute Ruby or fetch a formula.
    for path in sorted((cellar / name).glob('*/.brew/' + name + '.rb'), reverse=True)[:32]:
        raw = read_info_file(path)
        def field(key):
            match = re.search(r'^\s*' + key + r'''\s+(["'])(.*?)\1\s*$''', raw, re.M)
            return match[2] if match and '#{' not in match[2] else ''
        return make_info(field('desc'), homepage=field('homepage'), source='Installed Homebrew formula',
                         notes='Homebrew does not provide a standard developer/company field here.')
    return make_info(source='Installed Homebrew formula', notes='No local formula description was found.')


def appimage_package_info(app):
    directory = xdg_root('XDG_DATA_HOME', '.local/share') / 'applications'
    for path in sorted(directory.glob('*.desktop'))[:1000]:
        try:
            config = configparser.ConfigParser(interpolation=None, strict=False)
            config.read_string(read_info_file(path))
            entry = config['Desktop Entry']
            command = shlex.split(entry.get('Exec', ''))
            if not command or command[0] != app.ident:
                continue
            return make_info(entry.get('Comment') or entry.get('GenericName', ''), categories=entry.get('Categories', '').split(';'),
                             source='Matching local AppImage desktop entry', notes='AppImage files are not executed to inspect metadata. Developer identity is not available in standard desktop entries.')
        except (OSError, ValueError, KeyError, configparser.Error):
            continue
    return make_info(source='AppImage file', notes='No matching local desktop description was found. The AppImage was not executed or mounted.')


def get_package_info(app):
    """Read only local metadata. Failure must not block selection or removal."""
    fallback = app.note if app.manager in ('APT', 'Pacman', 'DNF', 'Zypper') else ''
    try:
        if app.manager != 'AppImage' and not valid_ident(app.ident):
            raise ValueError('Invalid package identifier')
        if app.manager == 'APT':
            fields = ('Description', 'Maintainer', 'Homepage', 'Section')
            fmt = '\x1f'.join('${' + key + '}' for key in fields)
            values = read_command([tool('dpkg-query', True), '-W', '-f=' + fmt, app.ident], timeout=6).split('\x1f')
            if len(values) != 4:
                raise ValueError('Unexpected dpkg metadata')
            description = '\n'.join('' if line.strip() == '.' else line.strip() for line in values[0].splitlines())
            return make_info(description, values[1], 'Package maintainer', values[3], homepage=values[2], source='Installed dpkg package metadata',
                             notes='The package maintainer packages this software for the distribution and may differ from its original developer.')
        if app.manager == 'Pacman':
            fields = parse_key_info(read_command([tool('pacman', True), '-Qi', app.ident], timeout=6))
            return make_info(fields.get('Description'), fields.get('Packager'), 'Packager', homepage=fields.get('URL'), source='Installed Pacman package metadata',
                             notes='The packager may differ from the original developer. Groups: ' + fields.get('Groups', 'not provided'))
        if app.manager in ('DNF', 'Zypper'):
            fmt = '\x1f'.join('%{' + key + '}' for key in ('DESCRIPTION', 'VENDOR', 'PACKAGER', 'URL', 'GROUP'))
            values = read_command([tool('rpm', True), '-q', '--qf', fmt, app.ident], timeout=6).split('\x1f')
            if len(values) != 5:
                raise ValueError('Unexpected RPM metadata')
            vendor = values[1] if values[1] not in ('(none)', '') else values[2]
            return make_info(values[0], vendor, 'Vendor' if vendor == values[1] else 'Packager', values[4], homepage=values[3], source='Installed RPM package metadata',
                             notes='Vendor/packager metadata may describe the distributor rather than the original developer.')
        loaders = {'Flatpak': flatpak_package_info, 'Snap': snap_package_info, 'pipx': pipx_package_info,
                   'Homebrew': brew_package_info, 'AppImage': appimage_package_info}
        if app.manager in loaders:
            return loaders[app.manager](app)
        return make_info(fallback)
    except Exception as exc:
        return make_info(fallback, source=app.manager + ' local metadata', notes='Additional details unavailable: ' + clean_info(exc, 500))


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
                          text=False, env=ENV, timeout=timeout)
    if proc.returncode:
        err = proc.stderr
        try:
            err = err.decode('utf-8', errors='replace')
        except Exception:
            pass
        out = proc.stdout
        try:
            out = out.decode('utf-8', errors='replace')
        except Exception:
            pass
        raise RuntimeError((err or out or "Command failed").strip()[:1500])
    try:
        out = proc.stdout.decode('utf-8')
        # Replace non-breaking spaces
        out = out.replace('\u00a0', ' ')
        return out
    except Exception:
        out = proc.stdout.decode('latin-1')
        out = out.replace('\u00a0', ' ')
        return out


def valid_ident(value):
    return bool(IDENT.fullmatch(value)) and ".." not in value and not value.endswith(("+", "-"))


def guarded(app):
    base = app.ident.split(":")[0]
    return app.protected or (app.manager in SYSTEM and bool(CORE.fullmatch(base)))


def parse_apt(output):
    result = []
    for line in output.splitlines():
        cols = line.split("\t", 5)
        if len(cols) not in (5, 6):
            raise ValueError("Unexpected dpkg-query output")
        name, version, status, essential = cols[:4]
        size, summary = (cols[4], cols[5]) if len(cols) == 6 else ("", cols[4])
        if status == "installed":
            result.append(App("APT", name, name, version, note=summary,
                              protected=essential == "yes", size_bytes=parse_size(size, "KiB"),
                              size_kind="reported" if parse_size(size, "KiB") is not None else "unknown",
                              size_note="dpkg Installed-Size estimate; excludes separately installed dependencies and user data"))
    return result


def scan_apt():
    fmt = "${binary:Package}\t${Version}\t${db:Status-Status}\t${Essential}\t${Installed-Size}\t${binary:Summary}\\n"
    return parse_apt(read_command([tool("dpkg-query", True), "-W", "-f=" + fmt]))


def parse_pacman(output):
    result = []
    for block in re.split(r"\n\s*\n", output.strip()):
        fields = {}
        for line in block.splitlines():
            if line and not line[0].isspace() and ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        if "Name" not in fields:
            if block.strip():
                raise ValueError("Unexpected pacman -Qi output")
            continue
        value = parse_size(fields.get("Installed Size", ""))
        result.append(App("Pacman", fields["Name"], fields["Name"], fields.get("Version", ""),
                          note=fields.get("Description", ""), size_bytes=value,
                          size_kind="reported" if value is not None else "unknown",
                          size_note="Pacman Installed Size (rounded metadata); dependencies and user data excluded"))
    return result


def scan_pacman():
    return parse_pacman(read_command([tool("pacman", True), "-Qi"]))


def scan_rpm(manager):
    fmt = "%{NAME}.%{ARCH}\t%{VERSION}-%{RELEASE}\t%|LONGSIZE?{%{LONGSIZE}}:{%{SIZE}}|\t%{SUMMARY}\\n"
    result = []
    for line in read_command([tool("rpm", True), "-qa", "--qf", fmt]).splitlines():
        cols = line.split("\t", 3)
        name, version = cols[:2]
        size, summary = (cols[2], cols[3]) if len(cols) == 4 else ("", cols[2])
        base = name.rsplit(".", 1)[0]
        result.append(App(manager, name, base, version, note=summary,
                          protected=bool(CORE.fullmatch(base)), size_bytes=parse_size(size),
                          size_kind="reported" if parse_size(size) is not None else "unknown",
                          size_note="RPM installed-file size metadata; dependencies and user data excluded"))
    return result


def parse_flatpak(output, scope, pkg_type="app"):
    result = []
    for line in output.splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) not in (4, 5):
            raise ValueError("Flatpak needs support for --columns=ref,name,version,origin")
        ref, name, version, origin = cols[:4]
        value = parse_size(cols[4]) if len(cols) == 5 else None
        # Prefix the ref with type for filtering
        if not ref.startswith(("app/", "runtime/", "platform/")):
            ref = pkg_type + "/" + ref
        if len(ref.split("/")) != 4:
            raise ValueError("Unexpected Flatpak reference: " + ref)
        result.append(App("Flatpak", ref, name, version, scope, "Remote: " + origin,
                          size_bytes=value, size_kind="reported" if value is not None else "unknown",
                          size_note="Flatpak reported deployment size (rounded); shared runtimes, deduplication and user data are not apportioned"))
    return result


def flatpak_scope(scope):
    if scope in ("user", "system"):
        return "--" + scope
    if scope.startswith("installation:") and valid_ident(scope[13:]):
        return "--installation=" + scope[13:]
    raise ValueError("Unknown Flatpak installation")


def scan_flatpak(scope):
    # Scan both applications and runtimes separately
    apps = parse_flatpak(read_command([tool("flatpak"), "list", flatpak_scope(scope),
                                       "--app", "--columns=ref,name,version,origin,size"]), scope, pkg_type="app")
    runtimes = parse_flatpak(read_command([tool("flatpak"), "list", flatpak_scope(scope),
                                           "--runtime", "--columns=ref,name,version,origin,size"]), scope, pkg_type="runtime")
    return apps + runtimes


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


def scan_snap(include_sizes=True):
    # snap list returns 0 and a header when installed entries exist.
    p = subprocess.run([tool("snap", True), "list"], capture_output=True,
                       text=True, env=ENV, timeout=45)
    if p.returncode and "No snaps are installed" in p.stderr + p.stdout:
        return []
    if p.returncode:
        raise RuntimeError(p.stderr.strip())
    apps = parse_snap(p.stdout)
    if not include_sizes:
        return apps
    return [measured_app(a, Path("/var/lib/snapd/snaps") / (a.ident + "_" + a.note.split()[-1] + ".snap"),
                         "Current compressed Snap revision only; older revisions, snapshots and user data excluded") for a in apps]


def parse_pipx(output):
    result = []
    for name, venv in json.loads(output)["venvs"].items():
        meta = venv.get("metadata", {}).get("main_package", {})
        result.append(App("pipx", name, name, meta.get("package_version", ""), "user",
                          "Isolated Python command-line app"))
    return result


def scan_pipx(include_sizes=True):
    apps = parse_pipx(read_command([tool("pipx"), "list", "--json"]))
    if not apps or not include_sizes:
        return apps
    try:
        root = Path(read_command([tool("pipx"), "environment", "--value", "PIPX_LOCAL_VENVS"]).strip())
        if not root.is_absolute():
            raise ValueError("No absolute pipx environment path")
        if any("/" in a.ident or ".." in a.ident for a in apps):
            raise ValueError("Unexpected pipx environment name")
        return measure_apps(apps, lambda a: root / a.ident,
                            "pipx environment including private dependencies; shared Python and user data excluded")
    except Exception as exc:
        return [dataclasses.replace(a, size_note="pipx size unavailable: " + str(exc)) for a in apps]


def scan_brew(include_sizes=True):
    result = []
    for line in read_command([tool("brew"), "list", "--formula", "--versions"]).splitlines():
        name, *versions = line.split()
        result.append(App("Homebrew", name, name, " ".join(versions), "user",
                          "Installed Homebrew formula"))
    if not result or not include_sizes:
        return result
    try:
        cellar = Path(read_command([tool("brew"), "--cellar"]).strip())
        if not cellar.is_absolute():
            raise ValueError("No absolute Homebrew Cellar path")
        return measure_apps(result, lambda a: cellar / a.ident.split("/")[-1],
                            "Homebrew formula directory including installed versions; other formulae and user data excluded")
    except Exception as exc:
        return [dataclasses.replace(a, size_note="Homebrew size unavailable: " + str(exc)) for a in result]


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


def scan_appimages(extra=(), include_sizes=True):
    paths = set()
    for root in image_roots():
        if root.is_dir() and not root.is_symlink():
            # Deliberately bounded; no expensive or intrusive home-wide scan.
            paths.update(root.glob("*"))
            paths.update(root.glob("*/*"))
    paths.update(Path(p) for p in extra)
    apps = [App("AppImage", str(p), p.stem, "portable", "user", "File: " + str(p))
            for p in sorted(paths) if appimage_valid(p)]
    if not include_sizes:
        return apps
    return [measured_app(app, app.ident,
                         "AppImage file only; external config/data excluded; uninstall moves this file to backup")
            for app in apps]


def scan_all(extra=(), enabled=None):
    """Scan every enabled source. `enabled` is the set of managers to include."""
    jobs, warnings = [], []
    enabled = set(SOURCES) if enabled is None else set(enabled)

    def wanted(source):
        return source in enabled
    if wanted("APT") and tool("apt-get", True) and tool("dpkg-query", True):
        jobs.append(("APT", scan_apt))
    if wanted("Pacman") and tool("pacman", True):
        jobs.append(("Pacman", scan_pacman))
    if wanted("Zypper") or wanted("DNF"):
        if tool("rpm", True):
            if tool("zypper", True) and wanted("Zypper"):
                jobs.append(("Zypper", lambda: scan_rpm("Zypper")))
            elif (tool("dnf", True) or tool("dnf5", True)) and wanted("DNF"):
                jobs.append(("DNF", lambda: scan_rpm("DNF")))
            elif wanted("Zypper") or wanted("DNF"):
                warnings.append("RPM database found, but no supported DNF/Zypper frontend.")
    if wanted("Snap") and tool("snap", True):
        jobs.append(("Snap", scan_snap))
    if wanted("Flatpak") and tool("flatpak"):
        try:
            for scope in flatpak_installations():
                jobs.append(("Flatpak " + scope, lambda s=scope: scan_flatpak(s)))
        except Exception as exc:
            warnings.append("Flatpak installations: " + str(exc))
    if wanted("pipx") and tool("pipx"):
        jobs.append(("pipx", scan_pipx))
    if wanted("Homebrew") and tool("brew"):
        jobs.append(("Homebrew", scan_brew))
    if wanted("AppImage"):
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
        found = scan_appimages(extra, include_sizes=False)
    elif manager == "APT":
        found = scan_apt()
    elif manager == "Pacman":
        found = scan_pacman()
    elif manager in ("DNF", "Zypper"):
        found = scan_rpm(manager)
    elif manager == "Flatpak":
        found = scan_flatpak(scope)
    elif manager == "Snap":
        found = scan_snap(include_sizes=False)
    elif manager == "pipx":
        found = scan_pipx(include_sizes=False)
    elif manager == "Homebrew":
        found = scan_brew(include_sizes=False)
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



DARK_BACKGROUNDS = {
    '#f5f5ef': '#101b18', 'white': '#182722', '#ffffff': '#182722',
    '#eef1e9': '#23392e', '#fafbf8': '#1c2c25', '#e4f2d5': '#35513a',
    '#e7eedf': '#243a2b', '#e4ebd9': '#243a2b', '#e7ecdf': '#293f33',
    '#193c34': '#112d26', '#284a40': '#234a3c', '#c8ef9b': '#c0e994',
    '#d9edc1': '#35513a', '#d5ded0': '#34493c', '#dce3d6': '#34493c',
    '#d9e1d2': '#46614d', '#f6f7f3': '#182722', '#e4e9dd': '#24382b',
    '#d9d9d9': '#34493c', '#000000': '#e7f2e3', 'black': '#e7f2e3',
}
DARK_FOREGROUNDS = {
    '#19332f': '#e7f2e3', '#16243a': '#e7f2e3', '#67776e': '#afc0b2',
    '#193c34': '#c0e994', '#8b958c': '#87978b', '#91a18f': '#899989',
    'black': '#e7f2e3', '#000000': '#e7f2e3', '#62748c': '#afc0b2',
}


# Accent palettes. Every value is fixed here so an accent can never be mixed
# with, or derived from, the light/dark theme. The sidebar uses the accent's
# dark tone; buttons and selections use its bright tone.
ACCENTS = {
    'green': {'accent': '#c8ef9b', 'ink': '#19332f', 'side': '#193c34',
              'side_soft': '#284a40', 'side_text': '#d4e1d3', 'side_muted': '#b4c8b8'},
    'red': {'accent': '#ef5f63', 'ink': '#3d0b0d', 'side': '#4a1a1d',
            'side_soft': '#5f2427', 'side_text': '#ffd9da', 'side_muted': '#e0a9ab'},
    'yellow': {'accent': '#f2d64b', 'ink': '#3a2d00', 'side': '#3d3413',
               'side_soft': '#54471b', 'side_text': '#f7ecc4', 'side_muted': '#d8c98d'},
    'pink': {'accent': '#ef6fa9', 'ink': '#3d0722', 'side': '#45182c',
             'side_soft': '#5c2339', 'side_text': '#ffdcec', 'side_muted': '#e3a6c3'},
    'blue': {'accent': '#6fa8f5', 'ink': '#0a2444', 'side': '#142a45',
             'side_soft': '#1f3d60', 'side_text': '#dbe8fb', 'side_muted': '#a5bcd9'},
    'cyan': {'accent': '#4fd4dc', 'ink': '#04343a', 'side': '#0e3438',
             'side_soft': '#164a4f', 'side_text': '#d5f6f8', 'side_muted': '#95c9cd'},
}
SOURCES = ("APT", "Pacman", "DNF", "Zypper", "Snap", "Flatpak", "pipx", "Homebrew", "AppImage")


def blend(color, other, amount):
    """Mix two #rrggbb colours. Used only to derive safe tints of one accent."""
    try:
        first = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        second = tuple(int(other[i:i + 2], 16) for i in (1, 3, 5))
    except (TypeError, ValueError, IndexError):
        return color
    return '#' + ''.join(f'{round(a + (b - a) * amount):02x}' for a, b in zip(first, second))


def load_preferences():
    path = STORE / 'preferences.json'
    data = {}
    try:
        if not path.is_symlink() and path.stat().st_size <= 4096:
            data = json.loads(path.read_text())
    except (OSError, ValueError, AttributeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    theme = data.get('theme')
    accent = data.get('accent')
    disabled = data.get('disabled_sources')
    return {
        'theme': theme if theme in ('light', 'dark') else 'light',
        'accent': accent if accent in ACCENTS else 'green',
        'disabled_sources': sorted({s for s in disabled if s in SOURCES})
        if isinstance(disabled, list) else [],
    }


def save_preferences(**changes):
    data = load_preferences()
    data.update(changes)
    atomic_json(private_store() / 'preferences.json', data)
    return data


def load_theme():
    return load_preferences()['theme']


def save_theme(mode):
    if mode not in ('light', 'dark'):
        raise ValueError('Unknown theme')
    save_preferences(theme=mode)


def save_accent(name):
    if name not in ACCENTS:
        raise ValueError('Unknown accent colour')
    save_preferences(accent=name)


def save_disabled_sources(names):
    unknown = set(names) - set(SOURCES)
    if unknown:
        raise ValueError('Unknown package source: ' + ', '.join(sorted(unknown)))
    save_preferences(disabled_sources=sorted(set(names)))


class RowCache:
    """Per-scan lookup tables so repaints never redo expensive per-row work."""

    def __init__(self, apps):
        self.apps = list(apps)
        self.protected = {}
        self.search = {}
        self.size = {}
        self.by_source = {}
        self.counts = {}
        self.order_name = []

    def build(self):
        for app in self.apps:
            key = app.key
            self.protected[key] = guarded(app)
            self.search[key] = (app.name + " " + app.ident + " " + app.note).casefold()
            self.size[key] = (("≈ " if app.size_kind == "reported" and app.size_bytes is not None else "")
                              + format_size(app.size_bytes))
            self.by_source.setdefault(app.manager, []).append(app)
            self.counts[app.manager] = self.counts.get(app.manager, 0) + 1
        self.order_name = sorted(self.apps, key=lambda a: (a.name.casefold(), a.key))
        return self

    def is_protected(self, app):
        return self.protected.get(app.key, False)

    def matches(self, app, query):
        return not query or query in self.search.get(app.key, '')

    def sorted_by(self, order):
        if order == 'largest':
            return sorted(self.apps, key=lambda a: (a.size_bytes is None, -(a.size_bytes or 0),
                                                    a.name.casefold(), a.key))
        if order == 'smallest':
            return sorted(self.apps, key=lambda a: (a.size_bytes is None, a.size_bytes or 0,
                                                    a.name.casefold(), a.key))
        return self.order_name


class EmptyCache:
    """Stand-in used before the first scan finishes."""

    counts = {}
    by_source = {}

    def is_protected(self, app):
        return guarded(app)

    def matches(self, app, query):
        return True

    def sorted_by(self, order):
        return []


class PocketUI:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk, messagebox, filedialog
        from tkinter.scrolledtext import ScrolledText
        self.tk, self.ttk, self.dialog, self.files, self.Text = tk, ttk, messagebox, filedialog, ScrolledText
        self.root = root
        preferences = load_preferences()
        self.theme_mode = preferences['theme']
        self.accent = preferences['accent']
        self.disabled_sources = set(preferences['disabled_sources'])
        self.settings_window = None
        self.settings_page = 'appearance'
        self.settings_theme_buttons = {}
        self.settings_accent_buttons = {}
        self.settings_source_vars = {}
        self.source_labels = {}
        self.theme_originals = weakref.WeakKeyDictionary()
        self.accent_tagged = weakref.WeakKeyDictionary()
        self.theme_fixed = weakref.WeakKeyDictionary()
        self.set_accent_palette(self.accent)
        self.refresh_tints()
        self.order = "name"
        self.apps, self.visible, self.chosen, self.extra, self.warnings, self.removed = [], {}, {}, [], [], []
        self.cache = EmptyCache()
        self.painted = {}
        self.painted_nav = {}
        self.painted_focus = None
        self.cells = {}
        self.selection_state = ()
        self.selection_dirty = True
        self.totals_dirty = True
        self.render_job = None
        self.source, self.busy, self.anchor = "All", False, None
        self.flatpak_type = "all"
        self.events = queue.Queue()
        self.info_requests, self.info_results = queue.Queue(maxsize=1), queue.Queue()
        self.info_cache, self.info_epoch, self.info_token = {}, 0, None
        self.info_after, self.current_info = None, None
        info_requests, info_results = self.info_requests, self.info_results
        self.info_stop = threading.Event()
        info_stop = self.info_stop
        def read_info():
            while not info_stop.is_set():
                try:
                    token, app = info_requests.get(timeout=.5)
                except queue.Empty:
                    continue
                info = get_package_info(app)
                if not info_stop.is_set():
                    info_results.put((token, info))
        threading.Thread(target=read_info, daemon=True).start()
        root.bind('<Destroy>', lambda event: info_stop.set() if event.widget == root else None, add='+')
        self.bg, self.ink, self.muted = "#f5f5ef", "#19332f", "#67776e"
        self.set_accent_palette(self.accent)
        root.title(f"{TITLE}  {VERSION}")
        root.geometry("1140x760")
        root.minsize(900, 620)
        root.configure(bg=self.bg)
        root.option_add("*Font", ("DejaVu Sans", 10))
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", foreground=self.ink,
                        rowheight=38, borderwidth=0, font=("DejaVu Sans", 10))
        style.configure("Treeview.Heading", background="#eef1e9", foreground=self.muted,
                        font=("DejaVu Sans", 9, "bold"), padding=(8, 8), relief="flat")
        style.map("Treeview", background=[("selected", "#e4f2d5")], foreground=[("selected", self.ink)])
        style.map("Treeview.Heading", background=[("active", "#e4eadf")])
        style.configure("Vertical.TScrollbar", background="#d9e1d2", troughcolor="#f6f7f3", borderwidth=0, arrowsize=13)
        style.configure("TProgressbar", background=self.green, troughcolor="#e4e9dd", borderwidth=0)
        self.side = tk.Frame(root, bg=self.side_bg, width=212)
        self.side.pack(side="left", fill="y")
        self.side.pack_propagate(False)
        self.draw_sidebar()
        main = tk.Frame(root, bg=self.bg)
        main.pack(side="left", fill="both", expand=True, padx=28, pady=16)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)
        head = tk.Frame(main, bg=self.bg)
        head.grid(row=0, column=0, sticky="ew")
        tk.Label(head, text="YOUR SYSTEM  /  STORAGE & APPLICATIONS", bg=self.bg, fg=self.muted,
                 font=("DejaVu Sans", 8, "bold")).pack(side="left")
        top = tk.Frame(main, bg=self.bg)
        top.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        tk.Label(top, text="Your apps. Your space.", bg=self.bg, fg=self.ink,
                 font=("DejaVu Sans", 22, "bold")).pack(side="left")
        self.total = tk.StringVar(value="READING...")
        self.total_label = tk.Label(top, textvariable=self.total, font=("DejaVu Sans", 9, "bold"),
                                    padx=12, pady=8, relief="flat", borderwidth=0)
        self.total_label.pack(side="right")
        # The chip is accent-owned: a dark accent colour on a theme surface is
        # unreadable in dark mode, so both options come from the accent palette.
        self.tag_accent(self.total_label, {'background': 'accent', 'foreground': 'ink'})
        metrics = tk.Frame(main, bg=self.bg)
        metrics.grid(row=2, column=0, sticky="ew", pady=(8, 10))
        metrics.columnconfigure((0, 1), weight=1, uniform="metrics")
        self.view_space, self.selected_space = tk.StringVar(value="Unknown"), tk.StringVar(value="0 B")
        self.view_note, self.selected_note = tk.StringVar(value="Reading installed sizes..."), tk.StringVar(value="Nothing selected")
        for column, label, value, note in [(0, "IN THIS VIEW", self.view_space, self.view_note),
                                           (1, "SELECTED APPS", self.selected_space, self.selected_note)]:
            bg = "white" if column == 0 else "#e7eedf"
            card = tk.Frame(metrics, bg=bg, padx=12, pady=6)
            card.grid(row=0, column=column, sticky="ew", padx=(0, 8) if column == 0 else (8, 0))
            self.tag_accent(card, {'background': 'tint' if column == 0 else 'tint_soft'})
            value_row = tk.Frame(card, bg=bg)
            self.tag_accent(value_row, {'background': 'tint' if column == 0 else 'tint_soft'})
            value_row.pack(fill="x")
            label_one = tk.Label(value_row, text=label, bg=bg, fg=self.muted, font=("DejaVu Sans", 8, "bold"))
            label_one.pack(side="left")
            value_one = tk.Label(value_row, textvariable=value, bg=bg, fg=self.ink, font=("DejaVu Sans", 17, "bold"))
            value_one.pack(side="right")
            note_one = tk.Label(card, textvariable=note, bg=bg, fg=self.muted, font=("DejaVu Sans", 8))
            note_one.pack(anchor="w")
            for widget in (label_one, value_one, note_one):
                self.tag_accent(widget, {'background': 'tint' if column == 0 else 'tint_soft'})
        row = tk.Frame(main, bg=self.bg)
        row.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        searchbox = tk.Frame(row, bg="white", highlightbackground="#d5ded0", highlightthickness=1)
        self.tag_accent(searchbox, {'background': 'tint', 'highlightbackground': 'tint_line'})
        searchbox.pack(side="left", fill="x", expand=True)
        search_label = tk.Label(searchbox, text="Search", bg="white", fg=self.muted, padx=12)
        search_label.pack(side="left")
        self.search = tk.StringVar()
        self.entry = tk.Entry(searchbox, textvariable=self.search, relief="flat", borderwidth=0, highlightthickness=0,
                              bg="white", fg=self.ink, insertbackground=self.ink, font=("DejaVu Sans", 11))
        self.entry.pack(side="left", fill="x", expand=True, ipady=8)
        self.tag_accent(self.entry, {'background': 'tint', 'insertbackground': 'accent'})
        clear = tk.Button(searchbox, text="×", command=lambda: self.search.set(""), bg="white", fg=self.muted,
                          relief="flat", borderwidth=0, highlightthickness=0, cursor="hand2")
        clear.pack(side="right", padx=7)
        for widget in (search_label, clear):
            self.tag_accent(widget, {'background': 'tint'})
        self.refresh_button = self.button(row, "Refresh", self.refresh)
        self.refresh_button.pack(side="right", padx=(10, 0))
        actions = tk.Frame(main, bg=self.bg)
        actions.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        self.button(actions, "Select visible", self.select_visible, compact=True).pack(side="left")
        self.button(actions, "Clear selection", self.clear, compact=True).pack(side="left", padx=6)
        # Flatpak filter controls - only shown when Flatpak source is selected
        self.flatpak_frame = tk.Frame(actions, bg=self.bg)
        self.flatpak_frame.pack(side="right", padx=(20, 0))
        tk.Label(self.flatpak_frame, text="Flatpak:", bg=self.bg, fg=self.muted, font=("DejaVu Sans", 9)).pack(side="left", padx=(0, 6))
        self.flatpak_all_btn = self.button(self.flatpak_frame, "All", lambda: self.set_flatpak_type("all"), compact=True)
        self.flatpak_all_btn.pack(side="left")
        self.flatpak_apps_btn = self.button(self.flatpak_frame, "Apps", lambda: self.set_flatpak_type("apps"), compact=True)
        self.flatpak_apps_btn.pack(side="left", padx=(2, 0))
        self.flatpak_runtimes_btn = self.button(self.flatpak_frame, "Runtimes", lambda: self.set_flatpak_type("runtimes"), compact=True)
        self.flatpak_runtimes_btn.pack(side="left", padx=(2, 0))
        self.button(actions, "+ AppImage", self.add_image, compact=True).pack(side="right")
        self.button(actions, "Size details", self.size_details, compact=True).pack(side="right", padx=6)
        frame = tk.Frame(main, bg="white", highlightbackground="#dce3d6", highlightthickness=1)
        self.tag_accent(frame, {'background': 'tint', 'highlightbackground': 'tint_line'})
        frame.grid(row=5, column=0, sticky="nsew")
        self.tree = ttk.Treeview(frame, columns=("check", "name", "size", "source", "version", "scope"),
                                 show="headings", selectmode="none", takefocus=True)
        specs = [("check", "", 36, False), ("name", "APPLICATION / PACKAGE", 230, True),
                 ("size", "SIZE ↕", 125, False), ("source", "SOURCE", 85, False),
                 ("version", "VERSION", 100, True), ("scope", "SCOPE", 80, False)]
        for key, title, width, stretch in specs:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=35 if key == "check" else 55,
                             stretch=stretch, anchor="center" if key == "check" else "w")
        self.tree.heading("size", command=self.sort_size)
        self.tree.heading("name", command=self.sort_name)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("protected", foreground="#8b958c")
        self.empty = tk.Label(frame, text="Reading installed applications...", bg="white", fg=self.muted)
        self.empty.place(relx=.5, rely=.5, anchor="center")
        self.detail = tk.StringVar(value="Protected system components cannot be selected.")
        info_panel = tk.Frame(main, bg="white", padx=12, pady=8)
        self.tag_accent(info_panel, {'background': 'tint_soft'})
        info_panel.grid(row=6, column=0, sticky="ew", pady=(8, 8))
        info_head = tk.Frame(info_panel, bg="white")
        self.tag_accent(info_head, {'background': 'tint_soft'})
        info_head.pack(fill="x")
        self.info_title = tk.StringVar(value="ABOUT THIS PACKAGE")
        self.info_description = tk.StringVar(value="Click an app to see what it does.")
        self.info_identity = tk.StringVar(value="Type and developer / publisher will appear here.")
        self.info_button = self.button(info_head, "More details", self.package_details, compact=True)
        self.info_button.pack(side="right", padx=(8, 0))
        self.info_button.configure(state="disabled")
        self.info_title_label = tk.Label(info_head, textvariable=self.info_title, bg="white", fg=self.ink,
                                         anchor="w", font=("DejaVu Sans", 10, "bold"))
        self.info_title_label.pack(side="left", fill="x", expand=True)
        self.info_description_label = tk.Label(info_panel, textvariable=self.info_description, bg="white", fg=self.ink,
                 anchor="nw", justify="left", height=2, font=("DejaVu Sans", 9))
        self.info_description_label.pack(fill="x", pady=(3, 3))
        self.info_identity_label = tk.Label(info_panel, textvariable=self.info_identity, bg="white", fg=self.muted,
                                            anchor="w", font=("DejaVu Sans", 8))
        self.info_identity_label.pack(fill="x")
        for widget in (self.info_title_label, self.info_description_label, self.info_identity_label):
            self.tag_accent(widget, {'background': 'tint_soft'})
        info_panel.bind('<Configure>', lambda event: self.info_description_label.configure(wraplength=max(200, event.width - 24)))
        root.bind('<Configure>', lambda event: self.info_description_label.configure(height=1 if event.height < 700 else 2) if event.widget == root else None, add='+')
        bar = tk.Frame(main, bg="#e7eedf", padx=15, pady=9)
        self.tag_accent(bar, {'background': 'tint_soft'})
        bar.grid(row=7, column=0, sticky="ew")
        text = tk.Frame(bar, bg="#e7eedf")
        self.tag_accent(text, {'background': 'tint_soft'})
        text.pack(side="left")
        self.count = tk.StringVar(value="No apps selected")
        self.hint = tk.StringVar(value="Selection stays with you while you search.")
        self.count_label = tk.Label(text, textvariable=self.count, bg="#e7eedf", fg=self.ink,
                                    font=("DejaVu Sans", 11, "bold"))
        self.count_label.pack(anchor="w")
        self.hint_label = tk.Label(text, textvariable=self.hint, bg="#e7eedf", fg=self.muted,
                                   font=("DejaVu Sans", 8), wraplength=290, justify="left")
        self.hint_label.pack(anchor="w", pady=(3, 0))
        for widget in (self.count_label, self.hint_label):
            self.tag_accent(widget, {'background': 'tint_soft'})
        self.remove_button = self.button(bar, "Review & uninstall", self.review, primary=True)
        self.remove_button.pack(side="right")
        self.leftover_button = self.button(main, "Review leftovers from completed removals", self.cleanup_dialog, compact=True)
        self.leftover_button.grid(row=8, column=0, sticky="w", pady=(6, 0))
        self.leftover_button.configure(state="disabled")
        self.status = tk.StringVar(value="Ready")
        tk.Label(main, textvariable=self.status, bg=self.bg, fg=self.muted,
                 font=("DejaVu Sans", 8), anchor="w").grid(row=9, column=0, sticky="ew", pady=(5, 0))
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
        self.search.trace_add("write", lambda *_: self.queue_render())
        self.draw_nav()
        self.apply_theme(self.theme_mode, persist=False)
        # Hide Flatpak filter initially (will be shown when Flatpak source is selected)
        self.flatpak_frame.pack_forget()
        # Initialize Flatpak filter button appearance
        self.set_flatpak_type(self.flatpak_type)
        self.pump()
        self.refresh()

    def show_app_info(self, app):
        token = (self.info_epoch, app.key, app.version) if app else None
        if token == self.info_token:
            return
        self.info_token = token
        self.current_info = None
        if self.info_after:
            self.root.after_cancel(self.info_after)
            self.info_after = None
        if not app:
            self.info_title.set('ABOUT THIS PACKAGE')
            self.info_description.set('Click an app or move with the arrow keys to see what it does.')
            self.info_identity.set('Type and developer / publisher will appear here.')
            self.info_button.configure(state='disabled')
            self.detail.set('No matching package')
            return
        self.info_title.set(('Protected · ' if self.cache.is_protected(app) else '') + app.name[:48] + '  ·  ' + format_size(app.size_bytes))
        self.info_button.configure(state='disabled')
        if token in self.info_cache:
            self.display_info(self.info_cache[token])
            return
        self.info_description.set(app.note if app.manager in ('APT', 'Pacman', 'DNF', 'Zypper') else 'Reading the installed package description...')
        self.info_identity.set('Loading type and developer / publisher...')
        def submit():
            self.info_after = None
            if self.info_token != token:
                return
            try:
                self.info_requests.get_nowait()
            except queue.Empty:
                pass
            self.info_requests.put_nowait((token, app))
        self.info_after = self.root.after(160, submit)

    def display_info(self, info):
        self.current_info = info
        self.info_description.set(' '.join(info.description.split())[:320])
        publisher = re.sub(r'\s*<[^>]*>', '', info.publisher)
        self.info_identity.set(f'Type: {info.category}   ·   {info.publisher_role}: {publisher}')
        self.info_button.configure(state='normal')

    def poll_info(self):
        try:
            while True:
                token, info = self.info_results.get_nowait()
                if token[0] != self.info_epoch:
                    continue
                self.info_cache[token] = info
                if len(self.info_cache) > 256:
                    self.info_cache.pop(next(iter(self.info_cache)))
                if token == self.info_token:
                    self.display_info(info)
        except queue.Empty:
            pass

    def package_details(self):
        app = self.visible.get(self.tree.focus())
        info = self.current_info
        if not app or not info:
            return
        lines = [app.name, f'{app.manager} / {app.scope}  ·  {app.ident}', '',
                 'WHAT IT DOES', info.description, '', 'TYPE', info.category,
                 info.category_basis, '', info.publisher_role.upper(), info.publisher, '',
                 'PROJECT WEBSITE', info.homepage or 'Not provided', '',
                 'INSTALLED SIZE', format_size(app.size_bytes), app.size_note, '',
                 'INFORMATION SOURCE', info.source, info.notes, '',
                 'This description identifies the software; it does not establish whether removal is safe. Review the native transaction for dependencies.']
        self.text_window('About ' + app.name, '\n'.join(lines))

    def button(self, parent, text, command, primary=False, dark=False, compact=False):
        button = self.tk.Button(parent, text=text, command=command, relief="flat", borderwidth=0, highlightthickness=0,
                              bg=self.side_bg if primary else (self.side_soft if dark else "#e7ecdf"),
                              fg="#f5f9ed" if primary or dark else "#19332f",
                              activebackground=self.lime, activeforeground=self.accent_ink,
                              disabledforeground="#91a18f", font=("DejaVu Sans", 9, "bold"),
                              padx=12, pady=5 if compact else 10, cursor="hand2", takefocus=True)
        if primary:
            self.tag_accent(button, {'background': 'accent', 'foreground': 'ink',
                                     'activebackground': 'accent_ink', 'activeforeground': 'accent'})
        elif dark:
            self.tag_accent(button, {'background': 'soft', 'foreground': 'sidetext',
                                     'activebackground': 'accent', 'activeforeground': 'ink'})
        else:
            # Secondary button: accent-tinted surface, but the text follows the
            # theme so it stays readable on that surface in both modes.
            self.tag_accent(button, {'background': 'tint_soft',
                                     'activebackground': 'accent', 'activeforeground': 'ink'})
        self.theme_tree(button)
        return button

    def tag_accent(self, widget, roles):
        """Mark colour options that come from the accent, never from the theme."""
        self.accent_tagged[widget] = dict(roles)

    def tag_fixed(self, widget, colors):
        """Mark colours that belong to a widget's own palette (e.g. a swatch)."""
        self.theme_fixed[widget] = dict(colors)
        return widget

    def accent_roles(self, widget):
        roles = self.accent_tagged.get(widget)
        if not roles:
            return {}
        mapping = {'accent': self.lime, 'ink': self.accent_ink, 'side': self.side_bg,
                   'accent_ink': self.accent_ink, 'soft': self.side_soft,
                   'sidetext': self.side_text, 'sidemuted': self.side_muted,
                   'tint': self.tint, 'tint_soft': self.tint_soft, 'tint_line': self.tint_line}
        return {key: mapping[role] for key, role in roles.items()}

    def theme_tree(self, widget):
        try:
            if not widget.winfo_exists():
                return
            options = ('background', 'foreground', 'activebackground', 'activeforeground',
                       'disabledforeground', 'highlightbackground', 'highlightcolor',
                       'selectbackground', 'selectforeground', 'insertbackground', 'troughcolor',
                       'selectcolor')
            if widget not in self.theme_originals:
                self.theme_originals[widget] = {key: str(widget.cget(key)) for key in options if key in widget.keys()}
            original = self.theme_originals[widget]
            roles = self.accent_roles(widget)
            fixed = self.theme_fixed.get(widget, {})
            for key, value in original.items():
                if key in roles or key in fixed:
                    continue
                color = value
                if self.theme_mode == 'dark':
                    foreground = key.endswith('foreground') or key == 'insertbackground'
                    color = (DARK_FOREGROUNDS if foreground else DARK_BACKGROUNDS).get(value.lower(), value)
                widget.configure(**{key: color})
            if fixed:
                widget.configure(**fixed)
            if roles:
                widget.configure(**roles)
            for child in widget.winfo_children():
                self.theme_tree(child)
        except self.tk.TclError:
            pass

    def apply_theme(self, mode, persist=True):
        if mode not in ('light', 'dark'):
            return
        self.theme_mode = mode
        dark = mode == 'dark'
        self.refresh_tints()
        background = self.tint
        foreground = '#e7f2e3' if dark else self.ink
        heading = self.heading_bg
        selected_bg = blend(self.lime, background, .62 if dark else .55)
        style = self.ttk.Style(self.root)
        style.configure('Treeview', background=background, fieldbackground=background, foreground=foreground,
                        bordercolor=heading, lightcolor=heading, darkcolor=heading)
        style.configure('Treeview.Heading', background=heading, foreground='#c3d3c7' if dark else self.ink)
        style.map('Treeview', background=[('selected', selected_bg)], foreground=[('selected', foreground)])
        style.map('Treeview.Heading', background=[('active', self.heading_active)])
        style.configure('Vertical.TScrollbar', background=blend(self.lime, background, .30), troughcolor=background,
                        arrowcolor=foreground, bordercolor=background, lightcolor=background, darkcolor=background)
        self.tree.tag_configure('protected', foreground='#9daf9f' if dark else '#6d7a70')
        self.theme_tree(self.root)
        self.render_settings()
        if persist:
            try:
                save_theme(mode)
                self.status.set(f'{mode.title()} mode saved')
            except Exception as exc:
                self.status.set('Theme applied; preference could not be saved: ' + str(exc))

    def set_accent_palette(self, name):
        palette = ACCENTS.get(name, ACCENTS['green'])
        self.accent = name if name in ACCENTS else 'green'
        self.lime, self.accent_ink = palette['accent'], palette['ink']
        self.side_bg, self.side_soft = palette['side'], palette['side_soft']
        self.side_text, self.side_muted = palette['side_text'], palette['side_muted']
        self.green = self.side_bg

    def refresh_tints(self):
        """Wash the accent into the current surfaces so the whole app follows it.

        Deliberately small: the surface keeps almost all of its original
        luminance and only shifts hue. Large blends turn dark surfaces into
        mid-tones where the light body text no longer meets contrast targets.
        """
        dark = self.theme_mode == 'dark'
        surface = '#101b18' if dark else '#ffffff'
        soft = '#182722' if dark else '#f5f5ef'
        self.tint = blend(surface, self.lime, 0.08 if dark else 0.10)
        self.tint_soft = blend(soft, self.lime, 0.06 if dark else 0.06)
        self.tint_line = blend(soft, self.lime, 0.16 if dark else 0.14)
        self.heading_bg = blend('#23392e' if dark else '#eef1e9', self.lime, 0.10 if dark else 0.12)
        self.heading_active = blend('#34503b' if dark else '#e4eadf', self.lime, 0.16 if dark else 0.18)

    def apply_accent(self, name, persist=True):
        if name not in ACCENTS:
            return
        if name == self.accent:
            return
        self.set_accent_palette(name)
        self.draw_sidebar()
        self.apply_theme(self.theme_mode, persist=False)
        self.retag_settings_shell()
        self.render()
        if persist:
            try:
                save_accent(name)
                self.status.set(f'{name.title()} accent saved')
            except Exception as exc:
                self.status.set('Accent applied; preference could not be saved: ' + str(exc))

    def side_tag(self, widget, foreground=None):
        roles = {'background': 'side'}
        if foreground:
            roles['foreground'] = foreground
        self.accent_tagged[widget] = roles
        return widget

    def draw_sidebar(self):
        tk = self.tk
        for child in self.side.winfo_children():
            child.destroy()
        self.side.configure(bg=self.side_bg)
        self.side_tag(self.side)
        mark = tk.Canvas(self.side, width=47, height=47, bg=self.side_bg, highlightthickness=0)
        mark.pack(anchor="w", padx=24, pady=(28, 12))
        mark.create_rectangle(3, 9, 42, 44, fill=self.lime, outline="")
        mark.create_rectangle(13, 1, 32, 10, fill=self.lime, outline="")
        mark.create_line(15, 26, 30, 26, fill=self.side_bg, width=4)
        self.side_tag(mark)
        self.side_tag(tk.Label(self.side, text="pocket", font=("DejaVu Sans", 24, "bold"),
                               bg=self.side_bg, fg="#f4f9ec"), 'sidetext').pack(anchor="w", padx=24)
        self.side_tag(tk.Label(self.side, text=f"UNINSTALLER  /  {VERSION}", font=("DejaVu Sans", 8),
                               bg=self.side_bg, fg=self.side_muted), 'sidemuted').pack(anchor="w", padx=26, pady=(1, 26))
        self.nav_canvas = tk.Canvas(self.side, bg=self.side_bg, highlightthickness=0, height=200)
        self.side_tag(self.nav_canvas)
        self.nav = tk.Frame(self.nav_canvas, bg=self.side_bg)
        self.side_tag(self.nav)
        nav_item = self.nav_canvas.create_window((0, 0), window=self.nav, anchor="nw")
        self.nav.bind("<Configure>", lambda e: self.nav_canvas.configure(scrollregion=self.nav_canvas.bbox("all")))
        self.nav_canvas.bind("<Configure>", lambda e: self.nav_canvas.itemconfigure(nav_item, width=e.width))
        bottom = tk.Frame(self.side, bg=self.side_bg)
        self.side_tag(bottom)
        bottom.pack(side="bottom", fill="x", padx=18, pady=22)
        self.settings_button = self.button(bottom, "Settings", self.settings, dark=True)
        self.settings_button.pack(fill="x", pady=(0, 6))
        self.button(bottom, "Install / repair launcher", self.install, dark=True).pack(fill="x", pady=3)
        self.button(bottom, "Backups & restore", self.backups, dark=True).pack(fill="x", pady=3)
        self.button(bottom, "Scan notes", self.notes, dark=True).pack(fill="x", pady=3)
        self.side_tag(tk.Label(bottom, text="Small footprint.\nMore room for what matters.", justify="left",
                               bg=self.side_bg, fg=self.side_muted, font=("DejaVu Sans", 9)), 'sidemuted').pack(anchor="w", pady=(22, 0))
        self.nav_canvas.pack(fill="both", expand=True, padx=14)
        self.source_labels = {}
        self.painted_nav = {}
        self.draw_nav()

    def settings(self, page=None):
        if self.settings_window and self.settings_window.winfo_exists():
            if page:
                self.settings_page = page
                self.render_settings()
            self.settings_window.deiconify()
            self.settings_window.lift()
            self.settings_window.focus_set()
            return
        window = self.tk.Toplevel(self.root)
        window.title("Settings")
        window.geometry("760x560")
        window.minsize(660, 480)
        window.configure(bg=self.bg)
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", self.close_settings)
        self.settings_window = window
        rail = self.tk.Frame(window, bg=self.side_bg, width=190)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        self.side_tag(rail)
        self.settings_rail = rail
        self.settings_title = self.side_tag(self.tk.Label(rail, text="Settings", font=("DejaVu Sans", 20, "bold"),
                                                          bg=self.side_bg, fg="#f4f9ec"), 'sidetext')
        self.settings_title.pack(anchor="w", padx=22, pady=(28, 4))
        self.settings_subtitle = self.side_tag(self.tk.Label(rail, text="Appearance and packages",
                                                             font=("DejaVu Sans", 8), bg=self.side_bg,
                                                             fg=self.side_muted), 'sidemuted')
        self.settings_subtitle.pack(anchor="w", padx=24, pady=(0, 22))
        self.settings_tabs = {}
        for key, label in (("appearance", "Appearance"), ("packages", "Packages")):
            tab = self.button(rail, label, lambda k=key: self.show_settings_page(k), dark=True)
            tab.configure(anchor="w", font=("DejaVu Sans", 10, "bold"))
            tab.pack(fill="x", padx=14, pady=3)
            self.settings_tabs[key] = tab
        self.close_button = self.button(rail, "Close", self.close_settings, dark=True)
        self.close_button.pack(side="bottom", fill="x", padx=14, pady=20)
        self.settings_content = self.tk.Frame(window, bg=self.bg)
        self.settings_content.pack(side="left", fill="both", expand=True, padx=26, pady=22)
        self.render_settings()
        self.theme_tree(window)
        self.root.after_idle(lambda: self.theme_tree(window))

    def retag_settings_shell(self):
        """Re-point the settings rail at the current accent colours."""
        if not (self.settings_window and self.settings_window.winfo_exists()):
            return
        self.side_tag(self.settings_rail)
        self.side_tag(self.settings_title, 'sidetext')
        self.side_tag(self.settings_subtitle, 'sidemuted')
        self.accent_tagged[self.close_button] = {'background': 'soft', 'foreground': 'sidetext',
                                                 'activebackground': 'accent', 'activeforeground': 'ink'}
        self.theme_tree(self.settings_window)

    def show_settings_page(self, page):
        if page not in ("appearance", "packages"):
            return
        self.settings_page = page
        self.render_settings()

    def close_settings(self):
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.destroy()
        self.settings_window = None
        self.settings_tabs = {}
        self.settings_theme_buttons = {}
        self.settings_accent_buttons = {}
        self.settings_source_vars = {}

    def render_settings(self):
        if not (self.settings_window and self.settings_window.winfo_exists()):
            return
        for child in self.settings_content.winfo_children():
            child.destroy()
        self.settings_theme_buttons, self.settings_accent_buttons, self.settings_source_vars = {}, {}, {}
        for key, tab in self.settings_tabs.items():
            active = key == self.settings_page
            tab.configure(text=("● " if active else "  ") + tab.cget("text").lstrip("● "),
                          font=("DejaVu Sans", 10, "bold" if active else "normal"))
            if active:
                self.accent_tagged[tab] = {'background': 'accent', 'foreground': 'ink',
                                           'activebackground': 'accent', 'activeforeground': 'ink'}
            else:
                self.accent_tagged[tab] = {'background': 'soft', 'foreground': 'sidetext',
                                           'activebackground': 'accent', 'activeforeground': 'ink'}
        if self.settings_page == "appearance":
            self.build_appearance_page()
        else:
            self.build_packages_page()
        self.theme_tree(self.settings_content)
        # The tabs live on the rail, not in settings_content, so they need their
        # own repaint or the active-page highlight never moves.
        for tab in self.settings_tabs.values():
            self.theme_tree(tab)

    def build_appearance_page(self):
        tk = self.tk
        tk.Label(self.settings_content, text="Appearance", bg=self.bg, fg=self.ink,
                 font=("DejaVu Sans", 18, "bold"), anchor="w").pack(fill="x")
        tk.Label(self.settings_content, text="Theme and accent colour are separate. One does not change the other.",
                 bg=self.bg, fg=self.muted, font=("DejaVu Sans", 9), anchor="w",
                 wraplength=480, justify="left").pack(fill="x", pady=(2, 18))
        tk.Label(self.settings_content, text="THEME", bg=self.bg, fg=self.muted,
                 font=("DejaVu Sans", 8, "bold"), anchor="w").pack(fill="x", pady=(0, 6))
        theme_row = tk.Frame(self.settings_content, bg=self.bg)
        theme_row.pack(fill="x")
        for key, label in (("light", "Light"), ("dark", "Dark")):
            button = self.button(theme_row, ("● " if self.theme_mode == key else "  ") + label,
                                 lambda k=key: self.choose_theme(k))
            button.pack(side="left", padx=(0, 8))
            self.settings_theme_buttons[key] = button
        tk.Label(self.settings_content, text="ACCENT COLOUR", bg=self.bg, fg=self.muted,
                 font=("DejaVu Sans", 8, "bold"), anchor="w").pack(fill="x", pady=(22, 6))
        swatches = tk.Frame(self.settings_content, bg=self.bg)
        swatches.pack(fill="x")
        for n, (key, palette) in enumerate(ACCENTS.items()):
            button = tk.Button(swatches, text=("● " if self.accent == key else "  ") + key.title(),
                               command=lambda k=key: self.apply_accent(k), relief="flat", borderwidth=0,
                               highlightthickness=2, highlightbackground=palette['side'],
                               bg=palette['accent'], fg=palette['ink'], activebackground=palette['accent'],
                               activeforeground=palette['ink'], font=("DejaVu Sans", 9, "bold"),
                               padx=12, pady=8, cursor="hand2", takefocus=True)
            # A swatch always shows its own colour in both themes.
            self.tag_fixed(button, {'background': palette['accent'], 'foreground': palette['ink'],
                                    'activebackground': palette['accent'], 'activeforeground': palette['ink'],
                                    'highlightbackground': palette['side'], 'highlightcolor': palette['side']})
            button.grid(row=n // 3, column=n % 3, sticky="ew", padx=(0, 8), pady=(0, 8))
            self.settings_accent_buttons[key] = button
        for column in range(3):
            swatches.columnconfigure(column, weight=1, uniform="swatch")
        tk.Label(self.settings_content,
                 text="Green is the default. The accent changes the sidebar, selection and highlight colours; "
                      "the theme controls only light and dark surfaces.",
                 bg=self.bg, fg=self.muted, font=("DejaVu Sans", 8), anchor="w",
                 wraplength=480, justify="left").pack(fill="x", pady=(10, 0))

    def build_packages_page(self):
        tk = self.tk
        tk.Label(self.settings_content, text="Packages", bg=self.bg, fg=self.ink,
                 font=("DejaVu Sans", 18, "bold"), anchor="w").pack(fill="x")
        tk.Label(self.settings_content,
                 text="Enable or disable package sources. Disabled sources are not scanned, listed or counted.\n"
                      "Installed software is not changed by these switches.",
                 bg=self.bg, fg=self.muted, font=("DejaVu Sans", 9), anchor="w",
                 wraplength=480, justify="left").pack(fill="x", pady=(2, 14))
        counts = {}
        for app in self.apps:
            counts[app.manager] = counts.get(app.manager, 0) + 1
        listing = tk.Frame(self.settings_content, bg=self.bg)
        listing.pack(fill="both", expand=True)
        for n, source in enumerate(SOURCES):
            var = tk.BooleanVar(value=source not in self.disabled_sources)
            self.settings_source_vars[source] = var
            row = tk.Frame(listing, bg=self.bg)
            row.pack(fill="x", pady=1)
            check = tk.Checkbutton(row, text=source, variable=var, onvalue=True, offvalue=False,
                                   command=self.apply_sources, anchor="w", justify="left",
                                   bg=self.bg, fg=self.ink, activebackground=self.bg, activeforeground=self.ink,
                                   selectcolor="#ffffff", highlightthickness=0, borderwidth=0,
                                   font=("DejaVu Sans", 10), padx=2, cursor="hand2", takefocus=True)
            check.pack(side="left")
            note = "detected" if counts.get(source) else "not installed here"
            tk.Label(row, text=f"{counts.get(source, 0)} {note}", bg=self.bg, fg=self.muted,
                     font=("DejaVu Sans", 8)).pack(side="right", padx=(8, 2))
        actions = tk.Frame(self.settings_content, bg=self.bg)
        actions.pack(fill="x", pady=(12, 0))
        self.button(actions, "Enable all", lambda: self.set_all_sources(True)).pack(side="left")
        self.button(actions, "Disable all", lambda: self.set_all_sources(False)).pack(side="left", padx=8)

    def choose_theme(self, mode):
        self.apply_theme(mode)
        for key, button in self.settings_theme_buttons.items():
            button.configure(text=("● " if key == mode else "  ") + key.title())
        self.render_settings()

    def set_all_sources(self, enabled):
        for source, var in self.settings_source_vars.items():
            var.set(enabled)
        self.apply_sources()

    def apply_sources(self):
        disabled = sorted(source for source, var in self.settings_source_vars.items() if not var.get())
        if set(disabled) == self.disabled_sources:
            return
        self.disabled_sources = set(disabled)
        self.chosen = {key: app for key, app in self.chosen.items() if app.manager not in self.disabled_sources}
        if self.source not in ("All", "Selected") and self.source in self.disabled_sources:
            self.source = "All"
        try:
            save_disabled_sources(disabled)
            self.status.set("Package sources updated: " + (", ".join(disabled) + " disabled" if disabled else "all enabled"))
        except Exception as exc:
            self.status.set("Sources applied; preference could not be saved: " + str(exc))
        self.render_settings()
        self.draw_nav()
        self.refresh()

    def sort_size(self):
        self.order = 'smallest' if self.order == 'largest' else 'largest'
        self.anchor = None
        self.tree.heading('size', text='SIZE ↓' if self.order == 'largest' else 'SIZE ↑')
        self.render()

    def sort_name(self):
        self.order = 'name'
        self.anchor = None
        self.tree.heading('size', text='SIZE ↕')
        self.render()

    def size_details(self):
        app = self.visible.get(self.tree.focus())
        lines = ['INSTALLED SIZE, EXPLAINED', '']
        if app:
            lines += [app.name, app.ident, f'{app.manager} / {app.scope}', '',
                      'Size: ' + format_size(app.size_bytes),
                      f'Raw value: {app.size_bytes:,} bytes' if app.size_bytes is not None else 'No reliable size is available.',
                      'Basis: ' + app.size_kind, app.size_note, '']
        lines += [
            '≈ means package-manager metadata, which may be estimated or rounded.',
            'Measured values use filesystem allocated blocks for the app file or installation folder.',
            'Unknown means the value is missing, unreadable, or the bounded size scan could not finish. It never means zero.', '',
            'The cards sum the known values in the filtered view or complete selection. Missing values are shown separately.',
            'Totals are NOT a promise of reclaimable space. Shared files, runtimes, hardlinks across apps, compression and filesystem snapshots can change the actual total.',
            'Separately installed dependencies and app data/caches are excluded unless the basis above says otherwise.',
            'AppImages and selected leftover folders move to backup. Those backups continue occupying disk space until you delete them.', '',
            'Click the SIZE header to sort largest first, then smallest first. Click APPLICATION to restore name order.',
        ]
        self.text_window('Installed size details', '\n'.join(lines))

    def draw_nav(self):
        """Update the source list in place. Rebuilding every button on each
        selection change was the second-largest cost after full re-render."""
        if not hasattr(self, 'nav') or not self.nav:
            return
        self.nav.configure(bg=self.side_bg)
        total = sum(1 for a in self.apps if a.manager not in self.disabled_sources)
        sources = ["All", "Selected"] + sorted(self.cache.counts)
        counts = dict(self.cache.counts)
        wanted = [(source,
                   total if source == "All" else
                   (len(self.chosen) if source == "Selected" else counts.get(source, 0)))
                  for source in sources]
        if list(self.source_labels) != [s for s, _ in wanted]:
            for w in self.nav.winfo_children():
                w.destroy()
            self.source_labels = {}
            self.painted_nav = {}
            for source, _ in wanted:
                button = self.button(self.nav, source, lambda s=source: self.set_source(s), dark=True)
                button.configure(anchor="w", font=("DejaVu Sans", 9), pady=8)
                button.pack(fill="x", pady=2)
                button.bind("<Button-4>", lambda e: self.nav_canvas.yview_scroll(-2, "units"))
                button.bind("<Button-5>", lambda e: self.nav_canvas.yview_scroll(2, "units"))
                self.source_labels[source] = button
        for source, n in wanted:
            state = (n, source == self.source)
            if self.painted_nav.get(source) == state:
                continue
            self.painted_nav[source] = state
            button = self.source_labels[source]
            active = source == self.source
            button.configure(text=f"{source}   {n}",
                             bg=self.lime if active else self.side_bg,
                             fg=self.side_bg if active else self.side_text)
            self.accent_tagged[button] = ({'background': 'accent', 'foreground': 'side',
                                           'activebackground': 'accent', 'activeforeground': 'side'}
                                          if active else
                                          {'background': 'side', 'foreground': 'sidetext',
                                           'activebackground': 'accent', 'activeforeground': 'side'})
            self.theme_tree(button)

    def set_source(self, source):
        if source in self.disabled_sources:
            source = "All"
        self.source = source
        self.draw_nav()
        # Show/hide Flatpak filter based on source
        self.flatpak_frame.pack_forget()
        if source == "Flatpak" and "Flatpak" not in self.disabled_sources:
            self.flatpak_frame.pack(side="right", padx=(20, 0))
            # Reset to all when switching to Flatpak
            self.set_flatpak_type("all")
        else:
            # When not on Flatpak page, show all Flatpak packages
            self.flatpak_type = "all"
            # Update Flatpak filter button colors to match theme
            for btn in [self.flatpak_all_btn, self.flatpak_apps_btn, self.flatpak_runtimes_btn]:
                # Reset to default color
                btn.configure(bg="#e7ecdf")
                if btn in self.theme_originals:
                    self.theme_originals[btn]['background'] = "#e7ecdf"
                self.theme_tree(btn)
        self.render()

    def set_flatpak_type(self, pkg_type):
        self.flatpak_type = pkg_type
        # Update button appearance to show active filter
        for btn, t in [(self.flatpak_all_btn, "all"), (self.flatpak_apps_btn, "apps"), (self.flatpak_runtimes_btn, "runtimes")]:
            active = pkg_type == t
            btn.configure(bg=self.lime if active else "#e7ecdf")
            # Update the original color so theme_tree uses the correct base
            if btn in self.theme_originals:
                self.theme_originals[btn]['background'] = self.lime if active else "#e7ecdf"
            self.accent_tagged[btn] = ({'background': 'accent', 'foreground': 'ink'}
                                       if active else {'activebackground': 'accent', 'activeforeground': 'ink'})
            # Re-apply theme to this button
            self.theme_tree(btn)
        self.render()

    def render(self):
        focused = self.visible.get(self.tree.focus())
        query = self.search.get().casefold()
        rows = []
        for app in self.cache.sorted_by(self.order):
            if app.manager in self.disabled_sources:
                continue
            if self.source == "Selected" and app.key not in self.chosen:
                continue
            if self.source not in ("Selected", "All", app.manager):
                continue
            if app.manager == "Flatpak" and self.flatpak_type != "all":
                parts = app.ident.split("/")
                pkg_type = parts[0] if parts and parts[0] in ("app", "runtime", "platform") else "app"
                if self.flatpak_type == "apps" and pkg_type != "app":
                    continue
                if self.flatpak_type == "runtimes" and pkg_type == "app":
                    continue
            if not self.cache.matches(app, query):
                continue
            rows.append(app)
        self.update_rows(rows, focused)
        # The visible set changed, so the rendered selection must be recomputed.
        self.selection_dirty = True
        self.totals_dirty = True
        self.paint()

    def update_rows(self, rows, focused):
        """Reuse existing tree items; only insert, update or delete what changed.

        Rebuilding every item on each keystroke cost ~130 ms for a 1600-row
        inventory. Inserting with its values already set avoids a second Tk
        round-trip per row on the initial fill.
        """
        existing = self.tree.get_children()
        wanted = [str(n) for n in range(len(rows))]
        self.visible = {str(n): app for n, app in enumerate(rows)}
        grew = len(existing) < len(wanted)
        if len(existing) != len(wanted) or existing[:len(wanted)] != tuple(wanted):
            extra = existing[len(wanted):]
            if extra:
                self.tree.delete(*extra)
                for iid in extra:
                    self.painted.pop(iid, None)
                    self.cells.pop(iid, None)
        # Any reused iid may now point at a different package, so drop cached
        # paint state for rows whose package changed.
        for iid, app in self.visible.items():
            state = self.painted.get(iid)
            if state and state[2] != app.key:
                self.painted.pop(iid, None)
                self.cells.pop(iid, None)
            self.painted.setdefault(iid, None)
        target = None
        for iid, app in zip(wanted, rows):
            values = ("", app.name, self.cache.size.get(app.key, format_size(app.size_bytes)),
                      app.manager, app.version, app.scope)
            if self.cells.get(iid) != values:
                self.cells[iid] = values
                if grew and iid not in existing:
                    self.tree.insert("", "end", iid=iid, values=values)
                else:
                    self.tree.item(iid, values=values)
            if focused and focused.key == app.key:
                target = iid
        if target is not None:
            self.tree.focus(target)
        children = self.tree.get_children()
        if not self.tree.focus() and children:
            self.tree.focus(children[0])
        if children:
            self.empty.place_forget()
        else:
            self.empty.configure(text="No matching apps. Try another search or source.")
            self.empty.place(relx=.5, rely=.5, anchor="center")

    def queue_render(self):
        """Coalesce bursts of keystrokes into one repaint.

        Uses an idle callback rather than a delay so a single change still
        paints on the next event-loop pass.
        """
        if self.render_job is not None:
            return
        self.render_job = self.root.after_idle(self.run_render)

    def run_render(self):
        self.render_job = None
        self.render()

    def paint(self):
        focus = self.tree.focus()
        # Only two rows change on an arrow key, so repaint the old and new focus
        # row instead of walking the whole inventory. Rows inserted by the last
        # render have no cached state yet and are painted here.
        pending = [iid for iid, state in self.painted.items() if state is None]
        for iid in [self.painted_focus, focus] + pending:
            if iid:
                self.paint_row(iid)
        self.painted_focus = focus
        if self.selection_dirty:
            self.selection_dirty = False
            selected = [iid for iid, app in self.visible.items() if app.key in self.chosen]
            if tuple(selected) != self.selection_state:
                self.selection_state = tuple(selected)
                self.tree.selection_set(selected)
        else:
            selected = self.selection_state
        n = len(self.chosen)
        hidden = n - len(selected)
        self.count.set(f"{n} app{'s' if n != 1 else ''} selected" if n else "No apps selected")
        self.hint.set(f"{hidden} selected outside this view. Review shows everything." if hidden else "Selection stays with you while you search.")
        self.remove_button.configure(state="normal" if n and not self.busy else "disabled")
        app = self.visible.get(focus)
        if app:
            self.detail.set(("Protected · " if self.cache.is_protected(app) else "") + app.ident[:48] + "  ·  " +
                            format_size(app.size_bytes) + "  (" + app.size_kind + ")")
        self.show_app_info(app)
        if self.totals_dirty:
            self.totals_dirty = False
            enabled = sum(1 for a in self.apps if a.manager not in self.disabled_sources)
            self.total.set(f"{len(self.visible)} / {enabled} ITEMS" +
                           (f"  ·  {len(self.disabled_sources)} SOURCE(S) OFF" if self.disabled_sources else ""))
            value, note = total_label(self.visible.values())
            self.view_space.set(value)
            self.view_note.set(note)
            value, note = total_label(self.chosen.values())
            self.selected_space.set(value)
            self.selected_note.set(note if self.chosen else "Nothing selected")

    def paint_row(self, iid):
        """Repaint a single row's checkbox glyph and highlight tag."""
        app = self.visible.get(iid)
        if not app or iid not in self.painted:
            return
        checked = app.key in self.chosen
        protected = self.cache.is_protected(app)
        glyph = "−" if protected else ("☑" if checked else "☐")
        tag = "protected" if protected else ""
        state = (glyph, tag, app.key)
        if self.painted.get(iid) != state:
            self.painted[iid] = state
            self.tree.set(iid, "check", glyph)
            self.tree.item(iid, tags=(tag,) if tag else ())

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
        if app and not self.cache.is_protected(app):
            if app.key in self.chosen:
                self.chosen.pop(app.key)
            else:
                self.chosen[app.key] = app
        self.selection_dirty = True
        self.totals_dirty = True
        self.draw_nav()

    def select_range(self, start, end):
        ids = list(self.visible)
        a, b = sorted((ids.index(start), ids.index(end)))
        for iid in ids[a:b + 1]:
            app = self.visible[iid]
            if not self.cache.is_protected(app):
                self.chosen[app.key] = app
        self.selection_dirty = True
        self.totals_dirty = True
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
            self.chosen.update({a.key: a for a in self.visible.values() if not self.cache.is_protected(a)})
            self.selection_dirty = True
            self.totals_dirty = True
            self.draw_nav()
            self.paint()
        return "break"

    def clear(self):
        if not self.busy:
            self.chosen.clear()
            self.selection_dirty = True
            self.totals_dirty = True
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
        self.poll_info()
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
        enabled = set(SOURCES) - self.disabled_sources
        self.work(lambda: scan_all(self.extra, enabled), self.scanned)

    def scanned(self, data):
        self.info_epoch += 1
        self.info_cache.clear()
        self.apps, self.warnings = data
        self.apps = [a for a in self.apps if a.manager not in self.disabled_sources]
        self.rebuild_cache()
        current = {a.key: a for a in self.apps}
        self.chosen = {k: current[k] for k in self.chosen if k in current and not self.cache.is_protected(current[k])}
        if self.source not in ("All", "Selected") and self.source not in self.cache.counts:
            self.source = "All"
        self.draw_nav()
        self.render()
        off = f" · {len(self.disabled_sources)} source(s) disabled" if self.disabled_sources else ""
        self.status.set(f"{len(self.apps)} installed entries · {len(self.warnings)} scan warning(s){off} · Ctrl+F to search")

    def rebuild_cache(self):
        """Precompute per-row strings and flags once per scan, not per repaint.

        The old path re-ran the protected-component regex, rebuilt the search
        haystack and re-formatted the size for every row on every keystroke.
        """
        self.cache = RowCache(self.apps)
        self.cache.build()

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
        self.theme_tree(window)
        self.root.after_idle(lambda: self.theme_tree(window))
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
            self.theme_tree(window)
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
        self.theme_tree(window)

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
            recommended = load_preferences()
            apps, warnings = scan_all(enabled=set(SOURCES) - set(recommended['disabled_sources']))
            if recommended['disabled_sources']:
                warnings = warnings + ["Disabled by settings: " + ", ".join(recommended['disabled_sources'])]
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
