#!/bin/bash
# Claude Code Usage Monitor v2 — Setup
set -e

echo "⚡ Claude Code Usage Monitor 설치"
echo ""

# Check Python 3
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3이 필요합니다: brew install python3"
    exit 1
fi
echo "✅ $(python3 --version)"

INSTALL_DIR="$HOME/.claude-menubar"
mkdir -p "$INSTALL_DIR"

# Copy script
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$SCRIPT_DIR/claude_menubar.py" "$INSTALL_DIR/claude_menubar.py"

# Venv + deps
echo "📦 설치 중..."
python3 -m venv "$INSTALL_DIR/venv" 2>/dev/null || true
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install rumps -q
echo "✅ 의존성 설치 완료"

# Launcher
cat > "$INSTALL_DIR/launch.sh" << 'EOF'
#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
"$DIR/venv/bin/python3" "$DIR/claude_menubar.py"
EOF
chmod +x "$INSTALL_DIR/launch.sh"

# LaunchAgent
PLIST="$HOME/Library/LaunchAgents/com.claude.usage-monitor.plist"
cat > "$PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.usage-monitor</string>
    <key>ProgramArguments</key>
    <array>
        <string>${INSTALL_DIR}/venv/bin/python3</string>
        <string>${INSTALL_DIR}/claude_menubar.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${INSTALL_DIR}/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${INSTALL_DIR}/stderr.log</string>
</dict>
</plist>
PLIST

echo ""
echo "══════════════════════════════════════════════"
echo "  ✅ 설치 완료!"
echo "══════════════════════════════════════════════"
echo ""
echo "  🚀 실행:  ~/.claude-menubar/launch.sh"
echo ""
echo "  🔄 자동 실행:  launchctl load $PLIST"
echo "  ⏹  해제:      launchctl unload $PLIST"
echo ""
echo "  🗑  제거:"
echo "     launchctl unload $PLIST 2>/dev/null"
echo "     rm -rf ~/.claude-menubar"
echo "     rm -f $PLIST"
echo ""

# Auto-launch (skip if called from auto-update)
if [ -z "$CLAUDE_AUTO_UPDATE" ]; then
    echo "🚀 앱을 실행합니다..."
    nohup "$INSTALL_DIR/launch.sh" > /dev/null 2>&1 &
fi
