; Inno Setup 6 script for LiveSubtitle
; Build: build_installer.bat  ->  dist\installer\LiveSubtitle-Setup-1.8.1.exe

#define MyAppName "LiveSubtitle"
#define MyAppNameZh "LiveSubtitle 实时字幕翻译"
#define MyAppVersion "1.8.1"
#define MyAppExeName "LiveSubtitle.exe"

[Setup]
AppId={{8E4F2A7C-6D3B-4E1A-9C8F-51B2A9D4E7F0}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=LiveSubtitle Project
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayName={#MyAppName}
SetupIconFile=app\icon.ico
OutputDir=dist\installer
OutputBaseFilename=LiveSubtitle-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
MinVersion=10.0
ChangesEnvironment=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\LiveSubtitle\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 用户数据（模型/配置/缓存）在 %USERPROFILE%\.live_subtitle，卸载时保留
Type: filesandordirs; Name: "{app}"
