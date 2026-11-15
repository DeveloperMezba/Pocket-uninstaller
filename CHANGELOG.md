# 3.3

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
