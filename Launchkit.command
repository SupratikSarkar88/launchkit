#!/bin/bash
# Double-click to run launchkit (macOS).
cd "$(dirname "$0")" || exit 1
command -v python3 >/dev/null 2>&1 || { echo "Python 3 is needed - macOS will offer to install it."; xcode-select --install; read -r -p "Press Enter to close..." _; exit 1; }
python3 launch.py menu
