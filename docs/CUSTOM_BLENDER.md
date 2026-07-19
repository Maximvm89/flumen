# Using a custom Blender install with Flumen

Flumen launches Blender for you ("Open in Blender", publishes, turntables,
playblasts). Normally it finds Blender on its own — you only need this page if
you see:

> **could not find Blender. Set tools.blender_path in config.yaml or the
> FLUMEN_BLENDER environment variable.**

…or if you have several Blenders installed and Flumen picks the wrong one
(e.g. it finds 4.2 but the project runs on 5.1).

## How Flumen finds Blender (first match wins)

1. `tools.blender_path` in your local `config.yaml` *(source checkouts)*
2. The **`FLUMEN_BLENDER`** environment variable *(works everywhere —
   recommended for the installed Windows app)*
3. Standard install locations:
   - **Windows:** `C:\Program Files\Blender Foundation\Blender*\blender.exe`
     (newest version wins)
   - **macOS:** `/Applications/Blender*.app`
   - **Linux:** `blender` on PATH, `/usr/bin`, `/usr/local/bin`, Flatpak

If your Blender came from **Steam, the Microsoft Store, a portable .zip, or a
custom folder**, step 3 won't see it — set the variable.

---

## Windows (installed Flumen app) — set `FLUMEN_BLENDER`

1. Find your `blender.exe`. Examples:
   - Steam: `C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe`
   - Portable zip: `D:\tools\blender-5.1.2\blender.exe`
2. Press **Start**, type `environment`, open **"Edit environment variables for
   your account"**.
3. Under *User variables* click **New…**:
   - Name: `FLUMEN_BLENDER`
   - Value: the full path to `blender.exe` (paste it, no quotes)
4. **OK** twice, then **fully close and reopen the Flumen Workspace app**
   (apps only read environment variables at startup).
5. Verify: open any task with "Open in Blender" — the right Blender should
   start. The version is shown in Blender's splash screen.

Command-line alternative (same effect, then restart the app):

```bat
setx FLUMEN_BLENDER "D:\tools\blender-5.1.2\blender.exe"
```

## macOS

GUI apps on macOS don't read your shell profile, so prefer the config file if
you run from source:

- **Source checkout:** in `config.yaml`:

  ```yaml
  tools:
    blender_path: "/Applications/Blender-5.1.app/Contents/MacOS/Blender"
  ```

  Note the path goes **inside the .app bundle** to the actual executable
  (`…app/Contents/MacOS/Blender`), not just the .app.

- **Terminal sessions** (running `python -m workspace_app` or the CLI):

  ```bash
  export FLUMEN_BLENDER="/Applications/Blender-5.1.app/Contents/MacOS/Blender"
  ```

  Add it to `~/.zprofile` to make it permanent.

## Linux

```bash
export FLUMEN_BLENDER="$HOME/apps/blender-5.1.2-linux-x64/blender"
```

Add to `~/.profile` (or your shell's profile) and log out/in, or launch the
Workspace app from a terminal that has it exported.

---

## Checking what Flumen picked

From a source checkout (any OS):

```bash
python -m flumen launch --dry-run
```

prints the Blender it would use (`Launching: <path>`) without opening
anything. With the installed app, just open a task — the chosen Blender's
version is on its splash screen, and `~/.flumen/workspace.log` records the
launch line.

## Notes

- The value must point at the **executable**, not a folder or the .app/.lnk:
  `blender.exe` on Windows, `…/Contents/MacOS/Blender` on macOS, the `blender`
  binary on Linux.
- `LEGAMI_BLENDER` (the pre-rename variable) still works as a fallback, but
  use `FLUMEN_BLENDER` for anything new.
- `tools.blender_path` in `config.yaml` is per-machine and **git-ignored**; it
  is also stripped when the config is published to the server, so your local
  path never leaks to teammates.
- The project pins its Blender version informally — if the splash shows a
  different major version than the rest of the team uses, expect linked-rig
  and shading differences; point the variable at the team's version.
