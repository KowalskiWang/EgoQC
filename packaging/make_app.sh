#!/bin/bash
# Builds EgoQC.app (launcher + a copy of the code; Python env stays in $VENV) into ~/Applications.
set -euo pipefail
APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${EGOQC_VENV:-$APP_ROOT/.venv}"
DEST="${1:-$HOME/Applications}"
APP="$DEST/EgoQC.app"
TMP="$(mktemp -d)"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
"$VENV/bin/python" -I "$APP_ROOT/packaging/make_icon.py" "$TMP/EgoQC.iconset"
iconutil -c icns "$TMP/EgoQC.iconset" -o "$APP/Contents/Resources/EgoQC.icns"
rm -rf "$TMP"
# The code is copied into the bundle: folders like ~/Desktop or ~/Documents are privacy-protected, and an app
# launched from Finder that reads its own code there blocks on an "allow access" prompt. Rebuild after code changes.
CODE="$APP/Contents/Resources/EgoQC"
mkdir -p "$CODE" "$APP_ROOT/models"
rsync -a --exclude "__pycache__" --exclude "._*" "$APP_ROOT/egoqc" "$APP_ROOT/app" "$APP_ROOT/models" "$APP_ROOT/egoqc_cli.py" "$CODE/"
# Python runs through a link inside Contents/MacOS so macOS treats the process as EgoQC.app (menu name, Dock icon,
# documents dropped on the icon); pyvenv.cfg + lib make that link use the venv's packages.
ln -sf "$VENV/bin/python" "$APP/Contents/MacOS/python"
cp "$VENV/pyvenv.cfg" "$APP/Contents/pyvenv.cfg"
ln -sfn "$VENV/lib" "$APP/Contents/lib"
cat > "$APP/Contents/MacOS/EgoQC" <<SH
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"   # ffmpeg / ffprobe
exec "$APP/Contents/MacOS/python" -I "$CODE/app/egoqc_app.py" "\$@"
SH
chmod +x "$APP/Contents/MacOS/EgoQC"
cat > "$APP/Contents/Info.plist" <<'PL'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>EgoQC</string>
  <key>CFBundleDisplayName</key><string>EgoQC</string>
  <key>CFBundleIdentifier</key><string>io.github.kowalskiwang.egoqc</string>
  <key>CFBundleVersion</key><string>1.0.0</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>EgoQC</string>
  <key>CFBundleIconFile</key><string>EgoQC</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>CFBundleDocumentTypes</key><array><dict>
    <key>CFBundleTypeName</key><string>Ego dataset</string>
    <key>CFBundleTypeRole</key><string>Viewer</string>
    <key>LSHandlerRank</key><string>Alternate</string>
    <key>LSItemContentTypes</key><array>
      <string>public.folder</string><string>public.archive</string><string>public.zip-archive</string>
      <string>public.movie</string><string>public.data</string>
    </array>
  </dict></array>
</dict></plist>
PL
touch "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" 2>/dev/null || true
echo "$APP"
