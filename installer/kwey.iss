; Kwey 安装包脚本（Inno Setup 6+）
;
; 构建方式：
;   python build.py --installer
; 或手工：
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=0.2.0 installer\kwey.iss
;
; 设计取舍：
; - PrivilegesRequired=lowest + 装到 %LOCALAPPDATA%\Programs\Kwey：
;   不弹 UAC，普通用户/受管电脑也能装（VS Code 同款策略）；
; - 卸载不删 %LOCALAPPDATA%\Kwey（日志、模型缓存）——笔记在 vault 里更不该动；
; - CloseApplications=yes：升级时自动关掉旧实例，避免"装完还是旧代码"。

#ifndef MyAppVersion
  #define MyAppVersion "0.2.0"
#endif
#define MyAppName "Kwey"
#define MyAppPublisher "Kwey"
#define MyAppURL "https://gitee.com/ye-jun-hao55/kwey"
#define MyAppExeName "Kwey.exe"

[Setup]
AppId={{7E2C1B54-9A3D-4F6B-8C21-9B4E5D0A71F3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\release
OutputBaseFilename=Kwey-{#MyAppVersion}-win64-setup
SetupIconFile=..\static\icons\kwey.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes

[Languages]
; 简体中文语言包随仓库分发（installer/ChineseSimplified.isl，v6.5.0+）。
; 缺失时自动退化为纯英文，不让"少一个翻译文件"卡住打包。
#if FileExists(AddBackslash(SourcePath) + "ChineseSimplified.isl")
Name: "chinesesimplified"; MessagesFile: "{#SourcePath}ChineseSimplified.isl"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\release\Kwey\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: quicklaunchicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清程序目录；%LOCALAPPDATA%\Kwey 里的日志/模型缓存保留（重装可复用）
Type: filesandordirs; Name: "{app}"

[Code]
// 卸载时提醒：笔记在 vault 目录里，卸载不会删除；模型缓存也保留
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('卸载完成。' + #13#10 + #13#10 +
      '你的笔记（vault 目录）与模型缓存（%LOCALAPPDATA%\Kwey）均已保留，' +
      '如需彻底清理请手动删除。', mbInformation, MB_OK) = IDOK then
    begin
    end;
  end;
end;
