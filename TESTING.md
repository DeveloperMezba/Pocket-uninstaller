# Validation record: 0.2.0

## Completed

- **56 automated tests passed**, including seven real Tk GUI interaction tests
  on a virtual X display with standard Linux Tk 8.6.
- Actual keyboard/mouse event tests cover Up/Down, Space, Shift selection,
  Ctrl+A with protected-item exclusion, Page Up/Down, Home/End, search-to-list
  focus, mouse selection and Escape.
- Selection persistence was checked across search, source changes and inventory
  refresh. The action buttons were checked at the minimum 900 × 620 size.
- Visual inspection at 1100 × 740 and 900 × 620 confirmed readable typography,
  visible action buttons, multiple checkbox selections and scrollable content.
  Visual screenshots used a representative sample inventory.
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
- The earlier live APT inventory and read-only transaction simulation are
  preserved as the baseline; native installed inventories and APT batch
  simulations were also checked for this update.

## Limits

- Native package **uninstall commands are mocked in the automated suite**. No
  installed package was removed from this development environment.
- A real GNOME/KDE/XFCE application menu, sudo prompts, and every terminal
  emulator were not available for end-to-end testing. The launcher's generated
  files and interpreter selection were tested, rather than claiming menu
  integration on every desktop.
- Full cross-distribution removals, particularly DNF4/DNF5, Zypper, Flatpak,
  Snap, pipx and Homebrew, still require validation on target systems.
- Protected-component checks are conservative, not a full dependency-safety
  proof. Native transaction confirmations remain so users can review dependent
  package removals.

Run the suite from the extracted folder:

```bash
python3 -m unittest discover -s tests -v
```

The seven GUI tests skip when DISPLAY is unavailable. They run when a working
X/XWayland display is present.
