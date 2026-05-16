; TimeChecker Windows installer (Inno Setup 6+)
;
; Build:
;   1) installer\build.bat            -> dist\TimeChecker.exe
;   2) ISCC installer\timechecker.iss -> installer\Output\TimeCheckerSetup.exe
;
; The custom wizard page asks for SERVER URL + API KEY and writes them to
; %APPDATA%\TimeChecker\config.json on install.

#define MyAppName       "TimeChecker"
#define MyAppVersion    "0.1.0"
#define MyAppPublisher  "TimeChecker"
#define MyAppExeName    "TimeChecker.exe"

[Setup]
AppId={{B0F1A7C2-7E2A-4F4D-9C7F-2E7F1B6A91D2}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=Output
OutputBaseFilename=TimeCheckerSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "Run on Windows startup"; GroupDescription: "Additional options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional options:"; Flags: unchecked

[Files]
Source: "..\dist\TimeChecker.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"""; \
  Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\{#MyAppName}\logs"

[Code]
var
  ServerPage: TInputQueryWizardPage;

procedure InitializeWizard();
begin
  ServerPage := CreateInputQueryPage(wpSelectTasks,
    'Server Settings',
    'Enter your remote TimeChecker server info',
    'Leave blank to run in LOCAL mode (local SQLite + local dashboard).');
  ServerPage.Add('Server URL (e.g. https://timechecker.up.railway.app):', False);
  ServerPage.Add('API Key:', True);
end;

function GetConfigPath(): string;
begin
  Result := ExpandConstant('{userappdata}\{#MyAppName}\config.json');
end;

procedure WriteConfigFile();
var
  Dir, ServerUrl, ApiKey, Json: string;
begin
  Dir := ExpandConstant('{userappdata}\{#MyAppName}');
  if not DirExists(Dir) then ForceDirectories(Dir);

  ServerUrl := Trim(ServerPage.Values[0]);
  ApiKey    := Trim(ServerPage.Values[1]);

  Json :=
    '{' + #13#10 +
    '  "idle_threshold_seconds": 60,' + #13#10 +
    '  "poll_interval_seconds": 30,' + #13#10 +
    '  "flask_port": 5000,' + #13#10 +
    '  "server_url": "' + ServerUrl + '",' + #13#10 +
    '  "api_key": "' + ApiKey + '",' + #13#10 +
    '  "excluded_processes": ["vlc.exe", "netflix.exe", "spotify.exe"],' + #13#10 +
    '  "excluded_title_keywords": ["YouTube", "Netflix", "Twitch"]' + #13#10 +
    '}';

  if not FileExists(GetConfigPath()) then
    SaveStringToFile(GetConfigPath(), Json, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    WriteConfigFile();
end;
