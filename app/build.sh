#!/bin/zsh
# Build ИИ-Офис.app and install it into ~/Applications. Run on the Mac (needs Xcode command line tools).
set -euo pipefail
cd "$(dirname "$0")"
APP="build/ИИ-Офис.app"
rm -rf build
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp Info.plist "$APP/Contents/Info.plist"
swiftc -O -o "$APP/Contents/MacOS/AIOffice" main.swift -framework Cocoa -framework WebKit
if [[ -f AppIcon.icns ]]; then cp AppIcon.icns "$APP/Contents/Resources/"; fi
# A real «Apple Development» certificate (free with an Apple ID in Xcode → Settings → Accounts) lets macOS show
# notification banners; without one the app is signed ad hoc and falls back to bouncing the Dock icon.
ID=$(security find-identity -v -p codesigning 2>/dev/null | awk -F'"' '/Apple Development|Developer ID Application/{print $2; exit}')
if [[ -n "$ID" ]]; then codesign --force --sign "$ID" "$APP"; echo "signed: $ID"; else codesign --force --sign - "$APP"; echo "signed ad hoc"; fi
mkdir -p ~/Applications
rm -rf ~/Applications/ИИ-Офис.app
cp -R "$APP" ~/Applications/
echo "installed ~/Applications/ИИ-Офис.app"
