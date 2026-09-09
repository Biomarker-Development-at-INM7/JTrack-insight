#!/bin/zsh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$PROJECT_ROOT/dist"
RUNTIME_DIR="$DIST_DIR/JTrack Insight"
APP_DIR="$DIST_DIR/JTrack Insight.app"
ICON_SOURCE="$PROJECT_ROOT/.venv/lib/python3.11/site-packages/PyInstaller/bootloader/images/icon-windowed.icns"

if [[ ! -d "$RUNTIME_DIR" ]]; then
  echo "Runtime folder not found: $RUNTIME_DIR" >&2
  exit 1
fi

timestamp="$(date +%Y-%m-%d-%H%M%S)"
if [[ -e "$APP_DIR" ]]; then
  mv "$APP_DIR" "$DIST_DIR/JTrack Insight.app.backup-$timestamp"
fi

mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

cat > "$APP_DIR/Contents/MacOS/JTrack Insight" <<'EOF'
#!/bin/zsh
set -euo pipefail

APP_CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="$APP_CONTENTS/Resources/runtime"
EXECUTABLE="$RUNTIME_DIR/JTrack Insight"
LOG_DIR="${TMPDIR:-/tmp}/jtrack-insight"
LOG_FILE="$LOG_DIR/launcher.log"

mkdir -p "$LOG_DIR"

if [[ ! -x "$EXECUTABLE" ]]; then
  osascript -e 'display dialog "JTrack Insight runtime was not found inside the application bundle." buttons {"OK"} default button "OK" with title "JTrack Insight"' >/dev/null 2>&1 || true
  exit 1
fi

nohup "$EXECUTABLE" >>"$LOG_FILE" 2>&1 &
exit 0
EOF

chmod +x "$APP_DIR/Contents/MacOS/JTrack Insight"

cat > "$APP_DIR/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleDisplayName</key>
  <string>JTrack Insight</string>
  <key>CFBundleExecutable</key>
  <string>JTrack Insight</string>
  <key>CFBundleIconFile</key>
  <string>icon-windowed.icns</string>
  <key>CFBundleIdentifier</key>
  <string>de.fzj.jutrack.jtrackinsight</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>JTrack Insight</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.0.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>11.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
EOF

printf 'APPL????' > "$APP_DIR/Contents/PkgInfo"

if [[ -f "$ICON_SOURCE" ]]; then
  cp "$ICON_SOURCE" "$APP_DIR/Contents/Resources/icon-windowed.icns"
fi

cp -R "$RUNTIME_DIR" "$APP_DIR/Contents/Resources/runtime"

echo "Created macOS launcher app: $APP_DIR"
