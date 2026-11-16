# Validation record: 3.3

## Completed

- New preferences layer reads and writes one `preferences.json` holding the
  theme, the accent name and the disabled source list. Tests cover the defaults,
  partial saves that leave other keys alone, duplicate/unknown source names,
  invalid accents, a malformed JSON file, a non-dict JSON file, and value-by-value
  fallback (`theme: neon` + `accent: purple` + `disabled_sources: [Snap, Nix, 7]`
  becomes light / green / `[Snap]`).
- A pre-existing theme-only preference file (`{"theme": "dark"}`) still loads as
  dark with the green accent, so upgrading does not reset the saved theme.
- Accent palettes are checked for distinctness and for readable text: every
  sidebar text colour is at least 4.5:1 against its own sidebar colour, muted
  sidebar text at least 3:1, and ink on each accent fill at least 4.5:1.
- `scan_all(enabled=...)` is tested with instruments that record calls; only the
  enabled managers run, and the default still scans every source.
- The GUI suite verifies the sidebar **Settings** button, both pages, the two
  theme buttons, all six accent swatches, that the old header Light/Dark buttons
  no longer exist, that each accent repaints the sidebar, primary button, active
  navigation entry and settings rail, and that theme and accent never modify each
  other in either direction.
- Source toggles are checked end to end: disabling APT removes its rows, its
  navigation entry and its queued selections and resets the active source to
  All; re-enabling restores them; the status line reports the change.
- Settings controls were checked at the minimum 660 × 480 window size and the
  main window at its minimum 900 × 620 size, including full visibility of the
  accent swatches and the Close button.
- Regression tests for two dark-mode defects, each confirmed to fail when the fix
  is reverted. The ITEMS badge contrast is asserted at >= 4.5:1 for all six
  accents in both themes (10.46:1 measured); every Packages checkbox is asserted
  to have a non-white indicator in dark mode with >= 4.5:1 tick contrast
  (13.47:1 measured), and the tree's checked/unchecked/protected glyphs are
  checked against the real row surface (13.47:1, protected marker 3:1+).
- Regression tests for two more defects, both confirmed to fail when the fix is
  reverted. Accent switching is asserted to restyle at least ten body widgets
  outside the sidebar (34 measured; the sidebar-only build reported 3), and the
  Settings page tabs are asserted to move their highlight to the selected page
  and remove it from the previous one.
- A sweep over all six accents in both themes asserts that no visible widget
  falls below 3:1 text contrast. This found and drove the fix for two real
  problems: accent tints that were strong enough to wash dark surfaces into
  mid-tones, and secondary buttons that used the dark accent ink as text on a
  dark surface. The final build reports zero low-contrast combinations.
- Performance is gated by counting work, not by wall-clock timing, because
  timings on the test machine varied enough to rank a fast build slower than a
  slow one. Navigating a 1,200-row inventory must inspect fewer than 20 rows per
  keypress; the incremental build inspects 3 and the reverted full-scan build
  inspects 1,202, so the guard fails loudly if the old behaviour returns. Wall
  clock figures on a 1,600-row inventory, before -> after: row selection
  48 -> 1.6 ms, arrow keys 73 -> 9 ms, sidebar rebuild 50 -> 0.25 ms, search
  keystroke 132 -> 24 ms, startup 989 -> 438 ms.
- New tests confirm every row still receives a checkbox glyph after incremental
  painting, that search filters to the correct rows, that the list surface takes
  the accent tint in both themes (it previously stayed a fixed dark colour), and
  that unchecked rows carry no background tag of their own.
- **111 automated tests passed**, including thirty-one real Tk GUI interaction
  tests on a virtual X display with standard Linux Tk 8.6.

- Metadata tests cover descriptions and correctly labelled publisher roles for
  APT, Pacman and RPM; matching Flatpak component IDs/scopes; pipx main-package
  identity; local Snap GET requests; installed Homebrew formula text; exact
  AppImage desktop matching; malformed metadata, limits and unknown fallbacks.
- GUI tests verify keyboard-driven package information without selection,
  rapid navigation, rejection of stale results, empty-filter clearing, cached
  details and refresh invalidation. Opening details preserves batch selection.
- New checks cover SI/IEC conversion, zero versus unknown, numeric sorting,
  totals, APT/Pacman/RPM/Flatpak size parsing, hardlink deduplication, symlink
  exclusion, sparse-file allocation, scan limits, pipx size failure, Homebrew
  formula folders, current Snap revision selection and theme preferences.
- A live read-only APT inventory found 696 installed entries with 696 available
  installed-size values. Their reported total was 2.1 GiB. This checks local
  dpkg integration; it does not validate other managers on native installations.
- Actual keyboard/mouse event tests cover Up/Down, Space, Shift selection,
  Ctrl+A with protected-item exclusion, Page Up/Down, Home/End, search-to-list
  focus, mouse selection and Escape.
- Selection persistence was checked across search, source changes and inventory
  refresh. The action buttons were checked at the minimum 900 × 620 size.
- Launcher tests cover repair/update with a home path containing spaces, custom
  XDG data locations, desktop Exec escaping, symlink rejection and preservation
  of existing backups. The installer verifies Python/Tk availability.
- Batch tests cover grouped native commands, separate installation scopes,
  sequential pipx commands, stale selections, refreshed protection metadata,
  cancellation/incomplete transactions, per-item outcomes and stopping later
  groups after a failure. The GUI batch path does not prompt for REMOVE.
- File tests actually move and restore temporary data. They cover no-overwrite
  restore, symlink/shared-directory guards, operation locking, interrupted
  manifests and safe cross-filesystem failures.

## Limits

- Native package **uninstall commands are mocked in the automated suite**. No
  installed package was removed from this development environment.
- A real GNOME/KDE/XFCE application menu, sudo prompts, and every terminal
  emulator were not available for end-to-end testing. The launcher's generated
  files and interpreter selection were tested, rather than claiming menu
  integration on every desktop.
- Full cross-distribution removals, particularly DNF4/DNF5, Zypper, Flatpak,
  Snap, pipx and Homebrew, still require validation on target systems.
- Accent colours and contrast values are checked numerically and the layout is
  checked geometrically; they were not judged by eye on every desktop theme, and
  a system Tk build that ignores widget colour options is not covered.
- Protected-component checks are conservative, not a full dependency-safety
  proof. Native transaction confirmations remain so users can review dependent
  package removals.

Run the suite from the extracted folder:

```bash
python3 -m unittest discover -s tests -v
```

The twenty GUI tests skip when DISPLAY is unavailable. They run when a working
X/XWayland display is present.
