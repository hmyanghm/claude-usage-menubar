@echo off
chcp 65001 >nul 2>&1
setlocal

echo ============================================
echo  Claude Code Usage Monitor - Windows Setup
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.8+ from python.org
    pause
    exit /b 1
)

:: Install dependencies
echo Installing dependencies...
pip install pystray Pillow
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

:: Create install directory
set INSTALL_DIR=%USERPROFILE%\.claude-menubar
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

:: Copy script
echo Copying files...
copy /Y "%~dp0claude_menubar_windows.py" "%INSTALL_DIR%\claude_menubar_windows.py" >nul

:: Create launcher script
echo Creating launcher...
(
echo @echo off
echo start /B pythonw "%INSTALL_DIR%\claude_menubar_windows.py"
) > "%INSTALL_DIR%\launch.bat"

:: Create startup shortcut (optional)
echo.
echo ============================================
echo  Installation complete!
echo ============================================
echo.
echo  Run:  "%INSTALL_DIR%\launch.bat"
echo.
set /p ADD_STARTUP="Add to Windows startup? (y/n): "
if /i not "%ADD_STARTUP%"=="y" goto :skip_startup
echo Creating startup shortcut...
powershell -NoProfile -Command "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut([System.IO.Path]::Combine($env:APPDATA, 'Microsoft\Windows\Start Menu\Programs\Startup', 'Claude Usage Monitor.lnk')); $sc.TargetPath = '%INSTALL_DIR%\launch.bat'; $sc.WorkingDirectory = '%INSTALL_DIR%'; $sc.Description = 'Claude Code Usage Monitor'; $sc.WindowStyle = 7; $sc.Save()"
echo Startup shortcut created. App will start automatically on login.
:skip_startup

echo.
echo Starting app now...
start /B pythonw "%INSTALL_DIR%\claude_menubar_windows.py"
echo Done!
pause
