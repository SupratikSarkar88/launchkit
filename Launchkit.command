#!/bin/bash
# Double-click to run launchkit (macOS).
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is needed - macOS will offer to install it."; xcode-select --install 2>/dev/null
  read -r -p "Press Enter to close..." _; exit 1
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo "First run: .env created and opened in TextEdit. Fill in the keys, save, then open Launchkit again."
  open -e .env; read -r -p "Press Enter to close..." _; exit 0
fi
python3 -c "import google_auth_oauthlib" 2>/dev/null || python3 -m pip install --user --quiet google-auth-oauthlib 2>/dev/null
python3 launch.py menu
