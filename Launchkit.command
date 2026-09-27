#!/bin/bash
# Double-click to run launchkit (macOS). Opens a simple menu in Terminal.
cd "$(dirname "$0")" || exit 1

pause() { echo; read -r -p "Press Enter to continue..." _; }

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is needed. A macOS prompt will offer to install it (Command Line Tools)."
  xcode-select --install 2>/dev/null; pause; exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "First run: I created .env and am opening it in TextEdit."
  echo "Fill in the keys (instructions are inside), save, then double-click Launchkit again."
  open -e .env; pause; exit 0
fi

need_google() {
  if ! grep -qE '^GOOGLE_REFRESH_TOKEN=[^[:space:]#]+' .env; then
    echo "Google isn't connected yet - running the one-time Google login."
    google_login
  fi
}

google_login() {
  python3 -c "import google_auth_oauthlib" 2>/dev/null || python3 -m pip install --user --quiet google-auth-oauthlib
  python3 google_auth_setup.py
}

while true; do
  clear
  cat <<'MENU'
  ==========  launchkit  ==========
   1) Suggest domain names
   2) Check names I already have
   3) Launch a product
   4) Audit a live site
   5) Search Console report
   6) Connect / reconnect Google
   q) Quit
MENU
  read -r -p "  Choose: " choice; echo
  case "$choice" in
    1) read -r -p "Keywords (e.g. invoice tool): " kw
       python3 launch.py suggest $kw; pause ;;
    2) read -r -p "Names, space separated (e.g. tinyinvoice quickbill.io): " names
       python3 launch.py check $names; pause ;;
    3) need_google
       read -r -p "Domain (e.g. myapp.com): " domain
       read -r -p "GitHub repo (owner/repo): " repo
       [ -z "$domain" ] || [ -z "$repo" ] && { echo "Domain and repo are required."; pause; continue; }
       flags=()
       read -r -p "Google Analytics? [Y/n]: " a;        [[ "$a" =~ ^[Nn] ]] && flags+=(--no-ga)
       read -r -p "PostHog (shared project)? [y/N]: " a; [[ "$a" =~ ^[Yy] ]] && flags+=(--posthog)
       read -r -p "Will this domain send email? [y/N]: " a; [[ "$a" =~ ^[Yy] ]] && flags+=(--email)
       read -r -p "Is it a Next.js app? [Y/n]: " a;     [[ "$a" =~ ^[Nn] ]] && flags+=(--framework "")
       echo; echo "Checking $domain (nothing is bought yet)..."
       log=$(mktemp)
       python3 launch.py launch "$domain" --repo "$repo" "${flags[@]}" 2>&1 | tee "$log"
       if ! grep -q "re-run with --buy" "$log"; then rm -f "$log"; pause; continue; fi
       rm -f "$log"
       echo; read -r -p "Type BUY to purchase $domain at the price shown above (anything else cancels): " ok
       if [ "$ok" = "BUY" ]; then python3 launch.py launch "$domain" --repo "$repo" --buy "${flags[@]}"
       else echo "Cancelled - nothing was bought."; fi
       pause ;;
    4) read -r -p "Site (e.g. myapp.com): " site
       python3 launch.py audit "$site"; pause ;;
    5) need_google
       read -r -p "Domain: " domain
       python3 launch.py report "$domain"; pause ;;
    6) google_login; pause ;;
    q|Q) exit 0 ;;
    *) ;;
  esac
done
