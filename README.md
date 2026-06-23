# Legami Pipeline

An open-source VFX/animation pipeline toolkit built around a shared SFTP server.
It standardizes the project folder structure, gives every artist an identical
Blender setup (ACES color management + render settings), and provides a desktop
app to sync work between local machines and the server.

Built for a feature animation workflow (Blender + Premiere today, Maya planned),
but the structure is generic enough to adapt to any DCC pipeline.

## Components

| Component | What it is | Where |
|---|---|---|
| `animpipe` | Python CLI: builds the project folder tree on SFTP, uploads/downloads, and launches Blender with the right OCIO config. | [`animpipe/`](animpipe/) |
| `legami_pipeline` | Blender add-on: pulls project settings from SFTP and applies color/render/units/output to every scene. | [`blender_addon/`](blender_addon/) |
| `workspace_app` | PySide6 desktop GUI: mirrors the structure locally and shows a live size + diff view to sync work/publish files. | [`workspace_app/`](workspace_app/) |
| Color pipeline | Pinned ACES OCIO config + the Blender/Premiere color policy. | [`color_pipeline/`](color_pipeline/) |
| Launchers | Cross-platform double-click setup and run scripts (Win/Mac/Linux). | [`launcher/`](launcher/) |

## How it fits together

One file on the server, `02_pipeline/project_settings.json`, is the single
source of truth for project settings. The launcher syncs it (plus the OCIO
config) to each artist's machine and starts Blender with the correct color
config; the add-on applies the rest inside Blender. The workspace app keeps
local `work/`/`publish/` folders in sync with the server.

```
SFTP server  ──sync──▶  local project copy  ──▶  Blender (addon applies settings)
   ▲                                                     │
   └──────────── workspace app (upload / download) ◀─────┘
```

## Quick start

Requirements: Python 3.10+, and Blender 4.2+/5.x for the add-on.

```bash
git clone <your-repo-url> legami && cd legami

# one-time setup (or double-click launcher/Setup-<os>)
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt        # core tools
pip install -r requirements-gui.txt    # optional: desktop app (PySide6)

cp .env.example .env                    # add your SFTP login
cp config.example.yaml config.yaml      # set project name, code, remote_root
```

Then:

```bash
python -m animpipe init-project --dry-run   # preview the folder structure
python -m animpipe init-project             # create it on the server
python -m animpipe launch                   # sync config + start Blender
python -m workspace_app                      # open the workspace GUI
```

Each sub-folder has its own README with detailed usage.

## Configuration

- `config.yaml` — project name, code, `remote_root`, optional `local_root` and
  `blender_path`. Copy from `config.example.yaml`. **Git-ignored.**
- `.env` — your per-user SFTP credentials. Copy from `.env.example`.
  **Git-ignored — never commit real credentials.**
- `folder_schema.yaml` — the project folder structure (single source of truth;
  edit freely).

## Development

```bash
pip install pytest
python -m pytest tests/ -q
```

Tests cover the folder-schema expansion, SFTP path logic, the Blender add-on's
settings-application logic (against a stubbed Blender), and the workspace
diff/mirror logic (against a fake SFTP). They need neither a live server nor
Blender installed.

## Roadmap

- Publish tool (Blender → versioned `publish/` with the per-user login)
- Maya port of the project-init add-on
- Packaged standalone launcher/app (PyInstaller) so artists need no Python setup

## License

[MIT](LICENSE) © 2026 Marco Parisi
