; LAN Drop Windows 安装程序（Inno Setup 6）。由 packaging/package.py 调用：
;   iscc /DAppVersion=0.3.0 /DSourceDir=dist\LANDrop /DOutputDir=out /DOutputBase=LANDrop-0.3.0-windows-x86_64-setup landrop.iss
;
; * 默认按当前用户安装（不需要管理员）；安装时可切换为所有用户。
; * 同时安装图形界面（LAN Drop.exe）与命令行（landrop.exe）；勾选「添加到 PATH」后终端里可直接用 landrop。
; * 卸载会移除 PATH 条目与防火墙规则；数据目录（%APPDATA%\LANDrop）保留。
; 静默安装：setup.exe /VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER /TASKS=addtopath

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\LANDrop"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef OutputBase
  #define OutputBase "LANDrop-setup"
#endif

#define AppName "LAN Drop"
#define GuiExe "LAN Drop.exe"
#define CliExe "landrop.exe"

[Setup]
AppId={{6E0B7C1A-3E5B-4C55-9C1E-6A3F1B2D8C41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=LAN Drop contributors
AppPublisherURL=https://github.com/Introl-ljl/landrop
AppSupportURL=https://github.com/Introl-ljl/landrop/issues
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBase}
SetupIconFile=..\assets\landrop.ico
UninstallDisplayIcon={app}\{#GuiExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
CloseApplications=yes
ShowLanguageDialog=auto
LanguageDetectionMethod=uilanguage
LicenseFile=..\..\LICENSE

; 简体中文界面：Inno Setup 7.0.2+ 自带官方译文；6.5–6.x 用同目录下随附的译文
; （ChineseSimplified.isl，取自 jrsoftware/issrc 的 is-6_7_1 标签，Inno Setup 许可证）
#if FileExists(AddBackslash(CompilerPath) + "Languages\ChineseSimplified.isl")
  #define ZhIsl "compiler:Languages\ChineseSimplified.isl"
#elif Ver >= EncodeVer(6, 5, 0)
  #define ZhIsl "ChineseSimplified.isl"
#endif
#ifdef ZhIsl
  #define HaveZh
  #pragma message "Chinese (Simplified) UI: " + ZhIsl
#else
  #pragma message "Chinese (Simplified) UI: not available, English only"
#endif

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
#ifdef HaveZh
Name: "zh"; MessagesFile: "{#ZhIsl}"
#endif

[CustomMessages]
en.AddToPath=Add the "landrop" command to PATH (use it from any terminal)
en.DesktopIcon=Create a desktop shortcut
en.Firewall=Allow LAN Drop through Windows Firewall (all users install only)
en.Launch=Launch LAN Drop
en.CliShortcut=LAN Drop Command Prompt
#ifdef HaveZh
zh.AddToPath=把 landrop 命令添加到 PATH（任意终端可直接使用命令行）
zh.DesktopIcon=创建桌面快捷方式
zh.Firewall=在 Windows 防火墙中允许 LAN Drop（仅「所有用户」安装时可用）
zh.Launch=启动 LAN Drop
zh.CliShortcut=LAN Drop 命令行
#endif

[Tasks]
Name: "addtopath"; Description: "{cm:AddToPath}"
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; Flags: unchecked
Name: "firewall"; Description: "{cm:Firewall}"; Check: IsAdminInstallMode

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#GuiExe}"
Name: "{autoprograms}\{cm:CliShortcut}"; Filename: "{cmd}"; Parameters: "/k ""{app}\{#CliExe}"" --help"; WorkingDir: "{userdocs}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#GuiExe}"; Tasks: desktopicon

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""LAN Drop"" dir=in action=allow program=""{app}\{#GuiExe}"" enable=yes profile=private,domain"; Flags: runhidden; Tasks: firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""LAN Drop CLI"" dir=in action=allow program=""{app}\{#CliExe}"" enable=yes profile=private,domain"; Flags: runhidden; Tasks: firewall
Filename: "{app}\{#GuiExe}"; Description: "{cm:Launch}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""LAN Drop"""; Flags: runhidden; RunOnceId: "fw1"; Check: IsAdminInstallMode
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""LAN Drop CLI"""; Flags: runhidden; RunOnceId: "fw2"; Check: IsAdminInstallMode

[Code]
{ PATH：当前用户安装写 HKCU\Environment，所有用户安装写系统环境变量；卸载时原样移除。 }

function EnvRoot: Integer;
begin
  if IsAdminInstallMode then Result := HKEY_LOCAL_MACHINE else Result := HKEY_CURRENT_USER;
end;

function EnvKey: String;
begin
  if IsAdminInstallMode then
    Result := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
  else
    Result := 'Environment';
end;

function PathHas(const Paths, Dir: String): Boolean;
begin
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') > 0;
end;

procedure AddToPath(const Dir: String);
var
  Paths: String;
begin
  if not RegQueryStringValue(EnvRoot, EnvKey, 'Path', Paths) then Paths := '';
  if PathHas(Paths, Dir) then exit;
  if (Paths <> '') and (Copy(Paths, Length(Paths), 1) <> ';') then Paths := Paths + ';';
  RegWriteExpandStringValue(EnvRoot, EnvKey, 'Path', Paths + Dir);
end;

procedure RemoveFromPath(const Dir: String);
var
  Paths, Rest, Item, Kept: String;
  P: Integer;
begin
  if not RegQueryStringValue(EnvRoot, EnvKey, 'Path', Paths) then exit;
  if not PathHas(Paths, Dir) then exit;
  Rest := Paths + ';';
  Kept := '';
  while Rest <> '' do
  begin
    P := Pos(';', Rest);
    Item := Copy(Rest, 1, P - 1);
    Rest := Copy(Rest, P + 1, Length(Rest));
    if (Item <> '') and (Uppercase(Item) <> Uppercase(Dir)) then
    begin
      if Kept <> '' then Kept := Kept + ';';
      Kept := Kept + Item;
    end;
  end;
  RegWriteExpandStringValue(EnvRoot, EnvKey, 'Path', Kept);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddToPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;
