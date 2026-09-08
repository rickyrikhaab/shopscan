; Inno Setup script for ShopScan
;
; Build with:  build_installer.bat   (which builds the exe first, then this)
;
; Two deliberate choices:
;
;   PrivilegesRequired=lowest  -- installs under %LOCALAPPDATA%\Programs, so no
;   UAC prompt. It is also a correctness requirement, not just convenience: the
;   app writes config.json and an output\ folder next to its executable, and
;   Program Files is read-only for a normal user.
;
;   A data-folder wizard page -- the domain database and the ~2 GB Common Crawl
;   cache must NOT live in the install directory. They survive uninstalls and
;   reinstalls, and a fresh install must be able to point at an existing 76k-row
;   database rather than starting empty. The chosen path is written to
;   config.json, which is exactly what app.py reads on startup.

#define AppName        "ShopScan"
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#define AppExe         "ShopScan.exe"
#ifndef DefaultDataDir
  #define DefaultDataDir "{localappdata}\ShopScan\data"
#endif

[Setup]
AppId={{8E4B2C11-7A93-4D26-9F45-2C7E1B0A6D38}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer
OutputBaseFilename=ShopScan-Setup-{#AppVersion}
SetupIconFile=assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
; Upgrade safety. AppMutex lets Setup detect a running copy and ask for it to
; be closed; CloseApplications lets the Restart Manager shut it if the user
; agrees. Without these, upgrading while the app is open fails on a locked
; executable and leaves a half-applied install.
AppMutex=ShopScanRunning
CloseApplications=yes
RestartApplications=no
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
  GroupDescription: "Shortcuts:"

[Files]
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "assets\app.ico";  DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExe}"; \
  IconFilename: "{app}\app.ico"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";     Filename: "{app}\{#AppExe}"; \
  IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Written by the app at runtime, so Inno does not track them.
Type: files;      Name: "{app}\config.json"
Type: files;      Name: "{app}\startup.log"
Type: filesandordirs; Name: "{app}\output"

[Code]
var
  DataPage: TInputDirWizardPage;

function DefaultDataFolder: String;
begin
  // On an upgrade, the folder the user picked LAST time wins over the path
  // baked in at build time -- otherwise a rebuild on a different machine (or
  // after moving the project) would silently repoint a working install at an
  // empty database.
  Result := GetPreviousData('DataDir', '');
  if Result = '' then
    Result := ExpandConstant('{#DefaultDataDir}');
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'DataDir', DataPage.Values[0]);
end;

procedure InitializeWizard;
begin
  DataPage := CreateInputDirPage(wpSelectDir,
    'Select Data Folder',
    'Where should scan data be kept?',
    'The domain database and the Common Crawl cache (~2 GB) are stored here.' + #13#10 +
    'They are kept out of the program folder so they survive upgrades and ' +
    'uninstalls.' + #13#10 + #13#10 +
    'If you already have a data folder from a previous version, point at it ' +
    'and your existing domains will be reused.',
    False, '');
  DataPage.Add('');
  DataPage.Values[0] := DefaultDataFolder;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = DataPage.ID then
  begin
    if Trim(DataPage.Values[0]) = '' then
    begin
      MsgBox('Please choose a data folder.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir, Json: String;
begin
  if CurStep = ssPostInstall then
  begin
    DataDir := Trim(DataPage.Values[0]);
    if not DirExists(DataDir) then
      ForceDirectories(DataDir);
    // app.py reads this on startup; JSON needs backslashes escaped.
    StringChangeEx(DataDir, '\', '\\', True);
    Json := '{' + #13#10 + '  "data_dir": "' + DataDir + '"' + #13#10 + '}';
    SaveStringToFile(ExpandConstant('{app}\config.json'), Json, False);
  end;
end;
