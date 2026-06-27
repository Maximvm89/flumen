# Releasing — Windows installer

How to cut a versioned release and produce the per-user Windows installer
(`Legami-Setup-<version>.exe`). PyInstaller can't cross-compile, so the **build
step must run on Windows**, but tagging/publishing can be driven from anywhere.

## One-time setup on the Windows build machine
- Python 3.11+ and the repo cloned, with a virtualenv.
- Build deps: `pip install -r requirements.txt -r requirements-build.txt`
- **Inno Setup 6** — `winget install JRSoftware.InnoSetup`
  (or download from https://jrsoftware.org/isdl.php). Adds `ISCC.exe`.

## Release steps

### 1. Tag the version (from the Mac, on a green `main`)
```bash
git tag -a v0.1.0 -m "Legami v0.1.0"
git push origin v0.1.0
```
The version is stamped from `git describe --tags`, so **tag before building**.

### 2. Build the installer (on Windows)
```powershell
git fetch --tags
git checkout v0.1.0
pip install -r requirements.txt -r requirements-build.txt   # if deps changed
python build.py --installer
```
This builds the onedir bundle (`dist\Legami\`), stamps `VERSION`, then compiles
`packaging\legami.iss` into **`dist\Legami-Setup-0.1.0.exe`**.

(`python build.py` alone just makes the bundle; add `--zip` for a plain zip, or
`--installer` for the Setup.exe.)

### 3. Smoke-test the installer
Run `Legami-Setup-0.1.0.exe` → it installs per-user to
`%LOCALAPPDATA%\Programs\Legami` (no admin prompt) and adds Start-menu/Desktop
shortcuts. Launch **Legami Workspace**, sign in, open a task in Blender, build a
review. Then check Add/Remove Programs shows "Legami Workspace 0.1.0" and that
Uninstall works.

### 4. Publish to GitHub Releases
```powershell
gh release create v0.1.0 dist\Legami-Setup-0.1.0.exe ^
  --title "Legami v0.1.0" --notes "First Windows release."
```
(or upload the `.exe` via the GitHub Releases web UI.)

### 5. Point artists at it
Update the wiki **Installation on Windows** page to link the latest
`Legami-Setup-*.exe` from the Releases page.

## Notes
- **Unsigned installer / SmartScreen.** Without a code-signing certificate Windows
  shows "Windows protected your PC" on first run → **More info → Run anyway**. A
  signing cert removes this; revisit once there's budget for one.
- **Per-user, no admin.** Installs under `%LOCALAPPDATA%`, so locked-down artist
  machines don't need an administrator.
- **Blender is not bundled.** Artists install Blender separately; the bundle ships
  the `blender_addon/` the launcher auto-loads.
- **Upgrades** reuse a fixed `AppId`, so installing a newer version replaces the old
  one in place.

## Later: automate with CI
Once the manual flow is proven, a GitHub Actions workflow on a `windows-latest`
runner can run `python build.py --installer` on tag push and attach the `.exe` to
the Release automatically — no Windows box needed to cut a release.
