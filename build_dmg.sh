#!/bin/bash
# Build Claude Usage Monitor .app and .dmg
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.build_venv"

echo "⚡ Claude Usage Monitor 빌드 시작"
echo ""

# Check dependencies
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3이 필요합니다"
    exit 1
fi

# Create build venv
if [ ! -d "$VENV_DIR" ]; then
    echo "📦 빌드용 가상환경 생성 중..."
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# Install dependencies
echo "📦 의존성 설치 중..."
pip install --upgrade pip -q
pip install pyinstaller rumps pyobjc-framework-Cocoa pyobjc-core -q

# Clean previous build
rm -rf build dist

# Build .app with spec file
echo "🔨 앱 빌드 중..."
pyinstaller "Claude Usage Monitor.spec" --clean --noconfirm

echo "✅ 앱 빌드 완료: dist/Claude Usage Monitor.app"

# Create DMG
APP_NAME="Claude Usage Monitor"
DMG_NAME="Claude.Usage.Monitor.dmg"
DMG_DIR="dmg_staging"

rm -rf "$DMG_DIR" "$DMG_NAME"
mkdir -p "$DMG_DIR"
cp -R "dist/${APP_NAME}.app" "$DMG_DIR/"

# Clean resource forks and extended attributes
echo "🧹 확장 속성 정리 중..."
xattr -cr "$DMG_DIR/${APP_NAME}.app"
find "$DMG_DIR/${APP_NAME}.app" -name "._*" -delete
find "$DMG_DIR/${APP_NAME}.app" -name ".DS_Store" -delete

# Ad-hoc code signing
echo "🔏 코드 서명 중..."
codesign --force --deep --sign - "$DMG_DIR/${APP_NAME}.app"

echo "📀 DMG 생성 중..."
hdiutil create -volname "$APP_NAME" \
    -srcfolder "$DMG_DIR" \
    -ov -format UDZO \
    "$DMG_NAME"

rm -rf "$DMG_DIR"

echo ""
echo "══════════════════════════════════════════════"
echo "  ✅ 빌드 완료!"
echo "  📀 DMG: $(pwd)/$DMG_NAME"
echo "══════════════════════════════════════════════"
