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
codesign --force --sign - "$APP"
mkdir -p ~/Applications
rm -rf ~/Applications/ИИ-Офис.app
cp -R "$APP" ~/Applications/
echo "installed ~/Applications/ИИ-Офис.app"
