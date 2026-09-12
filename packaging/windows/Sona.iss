; Application files live in app\ so the updater never moves the uninstaller.
[Setup]
AppId=com.yihang.sona
AppName=Sona
AppVersion={#AppVersion}
AppPublisher=一航同学YIHANG
AppPublisherURL=https://maxcosmos.top
AppSupportURL=https://github.com/yihangtongxue/sona/issues
AppUpdatesURL=https://github.com/yihangtongxue/sona/releases/latest
DefaultDirName={localappdata}\Programs\Sona
UsePreviousAppDir=no
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=Sona-{#AppVersion}-windows-x64-setup
SetupIconFile={#AppIcon}
UninstallDisplayIcon={app}\app\Sona.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayName=Sona

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#WebViewBootstrapper}"; DestName: "MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy

[Icons]
Name: "{userprograms}\Sona"; Filename: "{app}\app\Sona.exe"; WorkingDir: "{app}\app"
Name: "{userdesktop}\Sona"; Filename: "{app}\app\Sona.exe"; WorkingDir: "{app}\app"

[Run]
Filename: "{app}\app\Sona.exe"; Description: "Launch Sona"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only application binaries. Never remove %LOCALAPPDATA%\Sona user data.
Type: filesandordirs; Name: "{app}\app"

[Code]
function HasWebView2: Boolean;
var
  Version: String;
  Key: String;
begin
  Key := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := (RegQueryStringValue(HKLM32, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
  if not Result then
    Result := (RegQueryStringValue(HKCU, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode: Integer;
begin
  Result := '';
  if not HasWebView2 then begin
    ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
    if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/silent /install', '',
                SW_HIDE, ewWaitUntilTerminated, ExitCode) then begin
      Result := 'Could not start Microsoft WebView2 setup. Install WebView2 Runtime and retry.';
      Exit;
    end;
    if not HasWebView2 then
      Result := 'Microsoft WebView2 Runtime is required. Connect to the internet or install it from Microsoft, then retry.';
  end;
end;
