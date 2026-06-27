; Inno Setup script — wraps the PyInstaller onedir bundle (dist\Legami) into a
; single per-user installer (no admin rights needed).
;
; Build the bundle first, then compile this:
;   python build.py                 (produces dist\Legami + VERSION)
;   ISCC /DAppVersion=0.1.0 packaging\legami.iss
; or in one step on Windows:
;   python build.py --installer
;
; Output: dist\Legami-Setup-<version>.exe

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "Legami Workspace"
#define AppPublisher "Legami Pipeline"
#define SourceDir "..\dist\Legami"

[Setup]
; AppId must stay constant across versions so upgrades replace cleanly.
AppId={{8F3A2C10-9B4E-4D7A-AE21-2C5E9F0A77B1}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
; Per-user install — no administrator prompt.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Legami
DefaultGroupName=Legami
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Legami-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\Legami-Workspace.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; \
    GroupDescription: "Additional shortcuts:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{userprograms}\Legami\Legami Workspace"; Filename: "{app}\Legami-Workspace.exe"
Name: "{userprograms}\Legami\Uninstall Legami"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Legami Workspace"; Filename: "{app}\Legami-Workspace.exe"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\Legami-Workspace.exe"; Description: "Launch Legami Workspace now"; \
    Flags: nowait postinstall skipifsilent
