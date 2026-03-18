# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Claude Usage Monitor (macOS menubar app)

a = Analysis(
    ['claude_menubar.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['rumps', 'objc', 'Foundation', 'AppKit', 'CoreFoundation'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Claude Usage Monitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # windowed mode (no terminal)
    target_arch='arm64',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='Claude Usage Monitor',
)

app = BUNDLE(
    coll,
    name='Claude Usage Monitor.app',
    icon='app_icon.icns',
    bundle_identifier='com.claude.usage-monitor',
    info_plist={
        'CFBundleDisplayName': 'Claude Usage Monitor',
        'CFBundleShortVersionString': '1.0.5',
        'NSHighResolutionCapable': True,
        'LSUIElement': True,
    },
)
