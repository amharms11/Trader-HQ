#!/bin/bash
# Trader HQ installer and updater for macOS.
#   curl -fsSL https://raw.githubusercontent.com/amharms11/Trader-HQ/main/install.sh | bash
# Installs (or updates) Trader HQ in ~/Trader-HQ and puts the Trader HQ icon on your Desktop.
# Your keys, settings and history in ~/.amh-trader are never touched.
set -euo pipefail

DIR="$HOME/Trader-HQ"
URL="https://github.com/amharms11/Trader-HQ/archive/refs/heads/main.zip"
say() { printf "\n\033[1m%s\033[0m\n" "$1"; }

if ! command -v python3 >/dev/null 2>&1; then
  say "Python 3 is needed. macOS will offer to install the developer tools: click Install, then run this command again."
  xcode-select --install 2>/dev/null || true
  exit 1
fi

say "Downloading the latest Trader HQ..."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "$URL" -o "$TMP/trader-hq.zip"
unzip -q "$TMP/trader-hq.zip" -d "$TMP"
SRC="$(find "$TMP" -maxdepth 1 -type d -name 'Trader-HQ-*' | head -1)"
[ -f "$SRC/trader.py" ] || { echo "The download didn't look like Trader HQ."; exit 1; }

say "Stopping Trader HQ if it's running..."
curl -s -m 3 -X POST -H "X-Trader-HQ: 1" -H "Content-Type: application/json" -d '{}' \
  http://127.0.0.1:8780/api/quit >/dev/null 2>&1 || true
sleep 1

say "Installing into $DIR..."
mkdir -p "$DIR"
cp -R "$SRC"/. "$DIR"/
cd "$DIR"
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import sys" >/dev/null 2>&1; then
  rm -rf .venv
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

say "Creating the Trader HQ icon on your Desktop..."
.venv/bin/python trader.py install-app
.venv/bin/python -c "import updater; updater.version_file().parent.mkdir(parents=True, exist_ok=True)" >/dev/null 2>&1 || true
.venv/bin/python trader.py update --quiet >/dev/null 2>&1 || true   # remembers which version is installed

for OLD in "$HOME/Desktop/Trader-HQ" "$HOME/Desktop/Trader-HQ-options-trading" "$HOME/Downloads/Trader-HQ"; do
  if [ -d "$OLD" ] && [ "$OLD" != "$DIR" ]; then
    echo "Note: an older copy is still at $OLD. It isn't used anymore; you can delete it."
  fi
done

say "Done. Opening Trader HQ..."
echo "From now on, just double-click Trader HQ on your Desktop. It updates itself every time it opens."
open "$HOME/Desktop/Trader HQ.app"
