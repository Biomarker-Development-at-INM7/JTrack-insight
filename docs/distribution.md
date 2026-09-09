# Local App Distribution

This app can be distributed as a local desktop application while keeping all
data processing on the user's own machine.

## Architecture

- `trackautism_app.server.web`
  - existing FastAPI local app
- `trackautism_app.desktop.launcher`
  - starts the local FastAPI server on a free localhost port
  - waits until `/health` is ready
  - opens the app in the default browser
- `PyInstaller`
  - packages the launcher and backend into a standalone app

## Development Launch

### Browser-launch mode

```bash
cd JTrack-insight
source .venv/bin/activate
python3 main.py --mode browser
```

### Web mode

```bash
cd JTrack-insight
source .venv/bin/activate
python3 main.py --mode web
```

## Install Packaging Dependencies

```bash
cd JTrack-insight
source .venv/bin/activate
pip install -e ".[desktop,server,science,storage,dev]"
```

## Build a macOS App

```bash
cd JTrack-insight
source .venv/bin/activate
pyinstaller packaging/jtrack-insight.spec --noconfirm
```

Result:

- `dist/JTrack Insight.app`

## Internal Distribution

For internal testing, you can distribute:

- the `.app` bundle directly
- or a zipped copy of `dist/JTrack Insight.app`

Example:

```bash
cd dist
zip -r "JTrack Insight-macOS.zip" "JTrack Insight.app"
```

## Recommended Next Steps Before Public Distribution

1. Bundle default resource files explicitly
   - app-category defaults
   - templates
   - icons

2. Add application icons
   - `.icns` for macOS
   - `.ico` for Windows

3. Store user data in an app-specific folder
   - saved projects
   - exports
   - logs

4. Sign and notarize the macOS app
   - required for broad distribution outside developer machines

5. Add a Windows build
   - PyInstaller can also build a local `.exe`
   - later you can wrap that in an installer if needed

## Notes

- The app runs fully locally.
- Launching the app starts a localhost server and opens the user's default browser automatically.
- No internet connection is required for routine use unless you later add
  optional online integrations.
