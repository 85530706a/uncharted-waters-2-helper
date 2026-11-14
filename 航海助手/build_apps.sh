#!/bin/bash
set -euo pipefail

cd -- "$(dirname -- "$0")"
staging=$(mktemp -d "$PWD/.build-apps.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
swiftc -O app_launcher.swift -o "$staging/launch"
swiftc -O port_picker.swift -o "$staging/pick-port"

build_app() {
    local name=$1 mode=$2 accessory=$3
    local bundle="$staging/$name.app"
    mkdir -p "$bundle/Contents/MacOS"
    cp "$staging/launch" "$bundle/Contents/MacOS/"
    if [ "$mode" = teleport ]; then
        cp "$staging/pick-port" "$bundle/Contents/MacOS/"
    fi
    cat > "$bundle/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>$name</string>
<key>CFBundleDisplayName</key><string>$name</string>
<key>CFBundleExecutable</key><string>launch</string>
<key>CFBundleIdentifier</key><string>local.sea2.helper.$mode</string>
<key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>2</string>
<key>LSUIElement</key><$accessory/>
</dict></plist>
PLIST
    if [ "$mode" = teleport ]; then
        codesign --force --sign - "$bundle/Contents/MacOS/pick-port"
    fi
    codesign --force --sign - "$bundle"
}

build_app '启动游戏副本' game true
build_app '航海修改器' trainer false
build_app '港口瞬移' teleport true
for bundle in "$staging/"*.app; do
    name=${bundle##*/}
    if [ -d "$name" ]; then
        mv -- "$name" "$staging/previous-$name"
    fi
    if ! mv -- "$bundle" "$name"; then
        if [ -d "$staging/previous-$name" ]; then
            mv -- "$staging/previous-$name" "$name"
        fi
        exit 1
    fi
done
