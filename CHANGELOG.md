# 3.3

- Added a **Settings** button to the sidebar, opening a window with an
  **Appearance** page and a **Packages** page.
- Appearance holds the Light / Dark theme controls and an accented palette:
  green (default), red, yellow, pink, blue and cyan.
- Theme and accent are independent. Switching the theme never changes the accent
  and switching the accent never changes the theme; accent colours are fixed
  values, never derived from the light or dark surfaces.
- Removed the Light / Dark buttons from the main header; they now live only in
  Settings → Appearance.
- Packages lists every supported source with a checkbox. Disabled sources are
  not scanned, listed, counted or offered for removal, queued selections from
  them are dropped, and the sidebar updates immediately.
- Preferences are stored together in `preferences.json`. Existing theme-only
  files keep working, and an invalid or unknown value falls back on its own
  without discarding the other settings.
- The `--list` CLI honours the saved disabled sources and reports them.
- Fixed the ITEMS badge being unreadable in dark mode: its dark accent text sat
  on a dark surface. It is now an accent chip whose background and text both come
  from the accent palette, so it reads in every accent and both themes.
- Fixed the Packages page checkboxes disappearing in dark mode: the indicator box
  stayed literal white while the tick was drawn in the light theme foreground.
  The box colour now follows the theme surface, with at least 13:1 tick contrast.
- Accent colours now style the whole window, not just the sidebar: the storage
  cards, search field, package-list surface, info panel and selection bar all
  take a light tint of the chosen accent.
- Fixed the Settings page tabs never highlighting: the tab strip lived outside
  the repainted area, so the active-page indicator stayed on the first page.
- Fixed unreadable widget colours uncovered while tinting: dark-mode secondary
  buttons drew dark accent ink on a dark tinted surface, and the tint strengths
  turned some dark surfaces into mid-tones. All six accents in both themes now
  report zero visible widgets below 3:1 contrast.
- The package list no longer uses zebra striping or a focus-row background.
  Only checked rows are coloured; every other row keeps the plain list surface.
- The list surface now follows the accent in dark mode too. It had been pinned
  to a fixed dark colour, so the list stayed green while the rest of the window
  changed.
- Performance work on the main list, measured on a 1,600-row inventory:
  selecting a row 48 ms -> 1.6 ms, arrow keys 73 ms -> 9 ms, the source sidebar
  50 ms -> 0.25 ms, and a search keystroke 132 ms -> 24 ms for a whole word.
  Startup roughly halved. Achieved by caching per-row strings and protection
  flags, reusing list rows instead of rebuilding them, repainting only the rows
  that change, updating the sidebar in place, and coalescing keystrokes into one
  repaint.
- 111 automated tests, including 31 real Tk GUI tests.

# 3.1

- Added a focused-package information panel above the batch selection area.
- Shows purpose, software type, size and developer/publisher/maintainer role.
- Added full details with source, project website and category explanation.
- Reads local package metadata across supported sources; no new dependencies.
- Labels inferred types and unavailable identities without inventing companies.
- Uses one background worker, a bounded pending queue and session cache; stale
  results cannot overwrite another package's information.
- Preserved keyboard navigation, batch selection and both themes; adjusted the
  storage cards and small-window spacing to make room for package details.
- 90 automated tests passed, including 15 real Tk GUI interaction tests.

# 3.0

- Added per-package installed sizes, numeric sorting and raw-byte size details.
- Added filtered-view and complete-selection storage totals above search.
- Added native size metadata for APT, Pacman, RPM and Flatpak; bounded allocated
  disk measurements for Snap, pipx, Homebrew and AppImage installations.
- Clearly marks estimates and unknown sizes, and distinguishes installed size
  from potential reclaimed space.
- Added persistent Light / Dark controls, including review and backup windows.
- Refined spacing, storage cards and list styling; retains the green/lime palette.
- Preserved batch selection, keyboard controls, launcher repair and recoverable
  leftover cleanup. No new dependencies.
- Expanded validation to 72 passing tests, including 11 real Tk UI tests.

# 0.2.0

- Replaced single-selection removal with a persistent checkbox queue.
- Added batch review and grouped native removal. No per-app REMOVE typing in
  the GUI; authentication and native transaction confirmations remain.
- Added per-item batch results and stops after failure/cancellation.
- Added explicit keyboard focus, arrow navigation, Space toggling, range
  selection, Ctrl+A, Home/End, page navigation and mouse-wheel handling.
- Reworked the interface with a forest-green sidebar, cream background, lime
  selection accents, source counts, scrollable navigation and fixed action area.
- Added `install.sh` and an in-app Install / repair launcher action.
- Launcher setup checks for Python with Tk, prefers the system interpreter,
  installs a dedicated SVG icon, honors the menu's XDG location, and preserves
  existing backups during updates.
- Corrected desktop Exec escaping and added visible startup error reporting.
- Added common user-local executable lookup for GUI sessions with a short PATH.
- Preserved optional APT purge and backup/restore support.
