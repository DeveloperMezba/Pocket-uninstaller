#!/bin/sh
# Install/update the current user's menu entry. No root, pip or shell profile edits.
set -eu
app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    echo 'Python 3 is missing. Install Python 3.9+ and Tk using your Linux package manager.' >&2
    exit 1
fi
exec python3 "$app_dir/pocket_uninstaller.py" --install-launcher
