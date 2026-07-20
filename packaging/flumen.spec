# PyInstaller spec — builds BOTH executables into one onedir bundle (dist/Flumen):
#   flumen(.exe)         the CLI, also shelled to by the Blender addon
#   Flumen-Workspace(.exe) the PySide6 desktop app
# They share a single _internal/ folder, so the addon→flumen call resolves to
# the sibling executable. The same spec builds on macOS and Windows.
#
# Build with:  pyinstaller packaging/flumen.spec --noconfirm
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.getcwd())

# Ship the Blender-side scripts as data under flumen/ (resolved at runtime via
# sys._MEIPASS/flumen/): the headless turntable render script AND the add-on
# bootstrap the launcher passes to `blender --python` to auto-load the Flumen menu.
# Plus the imageio-ffmpeg binary so MP4 encoding works on a machine with no ffmpeg.
datas = [(os.path.join(ROOT, "flumen", "blender_turntable.py"), "flumen"),
         (os.path.join(ROOT, "flumen", "blender_playblast.py"), "flumen"),
         (os.path.join(ROOT, "flumen", "blender_render.py"), "flumen"),
         (os.path.join(ROOT, "flumen", "blender_cache_shot.py"), "flumen"),
         (os.path.join(ROOT, "flumen", "blender_bootstrap.py"), "flumen"),
         (os.path.join(ROOT, "packaging", "flumen.png"), ".")]  # runtime window icon
datas += collect_data_files("imageio_ffmpeg")

ICON = os.path.join(ROOT, "packaging", "flumen.ico")  # embedded in the .exe files

hiddenimports = ["paramiko", "yaml", "dotenv", "syncsketch", "zstandard", "PIL", "PIL.Image", "PIL.ImageDraw",
                 "PIL.ImageFont"]

cli_a = Analysis(
    [os.path.join(ROOT, "packaging", "entry_flumen.py")],
    pathex=[ROOT], binaries=[], datas=datas,
    hiddenimports=hiddenimports, hookspath=[], runtime_hooks=[],
    excludes=["PySide6", "shiboken6"],  # CLI doesn't need Qt
    noarchive=False,
)
gui_a = Analysis(
    [os.path.join(ROOT, "packaging", "entry_workspace.py")],
    pathex=[ROOT], binaries=[], datas=datas,
    hiddenimports=hiddenimports + collect_submodules("workspace_app"),
    hookspath=[], runtime_hooks=[], excludes=[], noarchive=False,
)

# Dedupe shared dependencies so the bundle isn't doubled.
MERGE((cli_a, "flumen", "flumen"),
      (gui_a, "entry_workspace", "Flumen-Workspace"))

cli_pyz = PYZ(cli_a.pure)
cli_exe = EXE(cli_pyz, cli_a.scripts, [], exclude_binaries=True,
              name="flumen", console=True, icon=ICON)

gui_pyz = PYZ(gui_a.pure)
gui_exe = EXE(gui_pyz, gui_a.scripts, [], exclude_binaries=True,
              name="Flumen-Workspace", console=False, icon=ICON)

coll = COLLECT(
    cli_exe, cli_a.binaries, cli_a.datas,
    gui_exe, gui_a.binaries, gui_a.datas,
    strip=False, upx=False, name="Flumen",
)
