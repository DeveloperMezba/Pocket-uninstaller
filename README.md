# Pocket Uninstaller 3.3

A small Linux desktop uninstaller with a searchable inventory, native removal
commands, and recoverable cleanup of selected leftover folders.

The runnable application is **one Python file**. No pip dependencies, browser
engine, background service, telemetry, or bundled runtime. Python and Tk must
already be installed or installed through your distribution.

## Install or update the launcher

Extract the new ZIP. Open a terminal **inside its PocketUninstaller folder** and run:

```bash
bash install.sh
```

Run as your normal user, **without sudo**. Close the old app and open **Pocket
Uninstaller** from your application menu. Existing backups are preserved.

You can also start the downloaded app directly:

```bash
python3 pocket_uninstaller.py
```

Then click **Install / repair launcher** in the sidebar. The installer chooses
an available Python 3.9+ interpreter with Tk, preferring the system Python;
copies the app to a stable location; installs its icon; writes the desktop menu
entry with escaped paths; and refreshes the desktop database when available.
It respects an absolute custom `XDG_DATA_HOME` for the menu entry. GUI-started
scans also check common user-local tool locations when the desktop PATH is short.

The equivalent installer command remains available:

```bash
python3 pocket_uninstaller.py --install-launcher
```

If Tk is missing, install the package for your distribution:

| Distribution | Command |
|---|---|
| Ubuntu, Debian, Mint | `sudo apt install python3-tk` |
| Arch, CachyOS, EndeavourOS, Manjaro | `sudo pacman -S --needed python tk` |
| Fedora | `sudo dnf install python3-tkinter` |
| openSUSE | `sudo zypper install python3-tkinter` |

Requires Linux, Python 3.9+, Tk and a graphical desktop. Typical Tk builds use
XWayland on Wayland desktops. No Python packages from pip are required.

## Batch removal

1. Click app rows or their checkboxes to select multiple items. Click again to
   deselect. Use Shift+click for a range or **Select visible** for the current
   filtered view. Protected components are excluded.
2. Search and switch sources freely. Your selection is preserved. The selected
   count includes items hidden by the current filter; **Selected** in the sidebar
   shows the queue. **Clear selection** clears the entire queue.
3. Click **Review & uninstall**. The review includes every selected item,
   grouped by package manager and installation scope, with native previews where
   available. The optional **Review APT purge instead** also previews removal of
   APT-managed system configuration; it is not enabled by default.
4. Click **Uninstall N selected** once. There is no per-app `REMOVE` typing in
   this GUI flow. System authentication and native confirmation prompts remain
   in the terminal so you can inspect dependency changes. Packages with the same
   manager and scope share one transaction where supported. pipx uses sequential
   native commands without additional Pocket Uninstaller confirmations.
5. The app displays progress and a final per-item result: removed, failed, or
   skipped. A failure or cancelled transaction stops later groups. Completed
   removals are verified and reported; partial changes are not rolled back.
6. Use **Review leftovers from completed removals** to review candidate paths
   for all verified removals in the current session. Nothing is selected by
   default. Only choose paths you recognize as belonging to those apps.

Close the target apps before removal. If no supported terminal is found, the app
shows one copyable command for the reviewed batch. Paste it into your terminal
as your normal user. Your native package manager may still ask for confirmation.

## Keyboard and mouse

| Action | Shortcut |
|---|---|
| Move through rows and keep the focused row visible | Up / Down |
| Toggle the focused app without clearing other choices | Space |
| Extend a selection range | Shift+Up / Shift+Down, or Shift+click |
| Select every removable item in the current view | Ctrl+A, while the list has focus |
| Clear all selections | Escape, while the list has focus |
| Scroll by a page | Page Up / Page Down |
| Jump to the beginning / end | Home / End |
| Start batch review | Enter, while the list has focus |
| Focus search | Ctrl+F |
| Move from search into the list | Down |
| Refresh installed apps | F5 |

Mouse-wheel scrolling works over the list. The source sidebar scrolls when it
contains more sources than fit. The main actions stay visible at the minimum
900 × 620 window size.

## Coverage

Installed packages are tracked by package managers, not by a universal Linux
registry. The app reads each supported manager that is available on the host.
Repository lists do not need to be downloaded or refreshed to build the inventory.

| Source | What is indexed | Removal method |
|---|---|---|
| APT / dpkg | Installed Debian-family packages, including PPA and local `.deb` packages | `apt-get remove`; optional explicit `purge` |
| Pacman | Installed Arch-family packages, including packages built from the AUR and local packages | `pacman -R` |
| DNF | Installed RPM packages, including local RPMs and enabled/previous third-party sources | DNF remove with automatic unused-dependency removal disabled |
| Zypper | Installed RPM packages on SUSE systems | `zypper remove --no-clean-deps` |
| Flatpak | Applications in the current user's, default system, and named system installations | Exact app/ID/architecture/branch in its original installation |
| Snap | Installed snaps | `snap remove`, retaining normal snapshot behavior |
| pipx | Apps in the current user's active pipx home | `pipx uninstall` |
| Homebrew | Installed Linux formulae in the active Homebrew installation | `brew uninstall --formula` |
| AppImage | Owned regular files in `~/Applications`, `~/AppImages`, `~/.local/bin`, and one child directory level | Move the selected file to recoverable backup |

For AppImages elsewhere inside your home folder, use **Add AppImage**. File
headers are checked without executing the image. Symlinked files and paths are
excluded. Explicitly added paths last for the current session.

The Source column identifies the manager. It is not an original-repository
provenance report. System inventories include libraries, command-line utilities,
and dependencies, not just applications with desktop icons. Flatpak runtimes are
not listed. If both Zypper and DNF are present, the RPM database is listed once
through Zypper.

**Not covered in this version:** Nix, Guix, Alpine APK, Gentoo Portage, immutable
OS deployment layers such as rpm-ostree, containers/distrobox, other users'
private installations, arbitrary shell installers, source builds, global or
project pip/npm/cargo environments, global pipx, multiple Homebrew prefixes, and
AppImages outside your home. Use their native tools. Failed or timed-out scans
appear in **Scan notes** rather than being silently reported as empty.

## Cleanup and safeguards

Package-owned files are handled by the package manager. This app does not
manually delete files from `/usr`, `/etc`, or `/opt`.

Additional cleanup offers exact-name matches inside your home configuration,
cache, data, state, Flatpak, and Snap directories, plus `~/.package-name`. XDG
locations are respected when they are non-symlinked paths inside your home.
These are **possible matches, not proof of ownership**. Vendor folder names,
renamed applications, and arbitrary extra locations cannot be inferred reliably.
For AppImages, exact adjacent `.home`/`.config` paths and user desktop entries
whose first Exec argument is the exact image path are offered instead.

- All cleanup paths require explicit selection and confirmation.
- Cleanup runs as your user, never as root.
- Shared desktop folders, the uninstaller's storage, symlinks, home/system
  roots, sensitive key folders, and directly mounted paths are excluded.
- If the app was reinstalled, cleanup is blocked. For Flatpak, another branch or
  installation using the same ID also blocks shared-data cleanup.
- A conservative list and package metadata block direct removal of common
  critical components. This is **not a complete dependency or system-safety
  audit**. Native removal can propose dependent packages. Read its transaction.
- No automatic `autoremove`, force flags, dependency-check bypass, or blanket
  home-folder deletion is requested.
- An operation lock prevents overlapping actions from this app.
- Removal commands are argument arrays, not shell strings. Passwords stay in
  the terminal's native sudo/system authentication flow.

Backups live in `~/.local/share/pocket-uninstaller/backup-...`. A manifest records
each original location and move status. Moves use filesystem rename; a
cross-filesystem move fails without deleting the original. A pending manifest
can still restore a payload after an interrupted status write.

**Backups continue to occupy disk space.** After you are satisfied, delete the
specific `backup-...` folder manually using your file manager to reclaim space.
Do not delete the whole Pocket Uninstaller folder while you still need backups.
Restoring these backups restores user files or an AppImage, not a removed
package. Reinstall a package through its manager if needed. APT purge's system
configuration removal is not backed up by this app. Snap may retain its own
automatic snapshots, which are managed by Snap separately.

## Terminal / CLI

Supported launchers: x-terminal-emulator, gnome-terminal, Konsole,
xfce4-terminal, kitty, foot, Alacritty, xterm, and mate-terminal.

If a terminal is not detected, the reviewed batch opens a window with a copyable
command. Run it in your terminal as your normal user. That batch flow does not
ask you to type REMOVE for each app.

The older `--uninstall MANAGER IDENTIFIER` CLI remains available for compatibility.
It performs a single interactive removal and retains its typed confirmation.
CLI removals do not activate the GUI's session-only leftover review.

Read-only JSON inventory works without Tk or a display:

```bash
python3 pocket_uninstaller.py --list > installed-apps.json
```

Examples of interactive removal:

```bash
python3 pocket_uninstaller.py --uninstall Pacman vlc
python3 pocket_uninstaller.py --uninstall APT vlc
python3 pocket_uninstaller.py --uninstall Flatpak app/org.videolan.VLC/x86_64/stable --scope user
```

Use the exact identifier and scope from your inventory; the examples do not
imply those applications are installed.

## Testing and release status

This is the 0.2.0 source update. `TESTING.md` records the actual validation
and limits. No packages were uninstalled from the development environment.
Keyboard and layout tests ran on a virtual display; cross-distribution native
uninstalls and real desktop menu behavior still need validation on target systems.

Run the included offline safety suite:

```bash
python3 -m unittest discover -s tests -v
```

## Command references

Implementation was checked against these official command references:

- APT: https://manpages.debian.org/bookworm/apt/apt-get.8.en.html
- Pacman: https://man.archlinux.org/man/pacman.8
- Flatpak: https://docs.flatpak.org/en/latest/flatpak-command-reference.html
- Snap: https://snapcraft.io/docs/how-to-guides/manage-snaps/create-data-snapshots/
- DNF: https://dnf.readthedocs.io/en/latest/command_ref.html
- DNF5: https://dnf5.readthedocs.io/en/latest/commands/remove.8.html
- Zypper: https://manpages.opensuse.org/Tumbleweed/zypper/zypper.8.en.html
- pipx: https://pipx.pypa.io/stable/reference/cli/
- Homebrew: https://docs.brew.sh/Manpage
- Desktop launchers: https://specifications.freedesktop.org/desktop-entry/latest/exec-variables.html
- Tk widgets: https://docs.python.org/3/library/tkinter.ttk.html

## Remove Pocket Uninstaller itself

If you used the launcher installer, remove only
`~/.local/share/pocket-uninstaller/pocket_uninstaller.py`, its
`pocket-uninstaller.svg` icon, and
`$XDG_DATA_HOME/applications/pocket-uninstaller.desktop` (normally
`~/.local/share/applications/pocket-uninstaller.desktop`). Preserve your backup
folders until you have restored or deliberately discarded their contents.

MIT license. See `LICENSE`.
