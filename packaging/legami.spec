# PyInstaller spec — builds BOTH executables into one onedir bundle (dist/Legami):
#   animpipe(.exe)         the CLI, also shelled to by the Blender addon
#   Legami-Workspace(.exe) the PySide6 desktop app
# They share a single _internal/ folder, so the addon→animpipe call resolves to
# the sibling executable. The same spec builds on macOS and Windows.
#
# Build with:  pyinstaller packaging/legami.spec --noconfirm
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.getcwd())

# Ship the headless Blender script as data under animpipe/ (turntable._bundled_path
# looks for it at sys._MEIPASS/animpipe/), and the imageio-ffmpeg binary so MP4
# encoding works on a machine with no system ffmpeg.
datas = [(os.path.join(ROOT, "animpipe", "blender_turntable.py"), "animpipe")]
datas += collect_data_files("imageio_ffmpeg")

hiddenimports = ["paramiko", "yaml", "dotenv"]

cli_a = Analysis(
    [os.path.join(ROOT, "packaging", "entry_animpipe.py")],
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
MERGE((cli_a, "animpipe", "animpipe"),
      (gui_a, "entry_workspace", "Legami-Workspace"))

cli_pyz = PYZ(cli_a.pure)
cli_exe = EXE(cli_pyz, cli_a.scripts, [], exclude_binaries=True,
              name="animpipe", console=True)

gui_pyz = PYZ(gui_a.pure)
gui_exe = EXE(gui_pyz, gui_a.scripts, [], exclude_binaries=True,
              name="Legami-Workspace", console=False)

coll = COLLECT(
    cli_exe, cli_a.binaries, cli_a.datas,
    gui_exe, gui_a.binaries, gui_a.datas,
    strip=False, upx=False, name="Legami",
)
