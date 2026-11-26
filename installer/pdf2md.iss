; مثبّت pdf2md — Windows.
; يُبنى داخل GitHub Actions فقط (انظر docs/CODE_SIGNING.md).
; التثبيت لكل مستخدم: بلا صلاحيات إدارية، والمسار %LOCALAPPDATA%\Programs\pdf2md.
; النتيجة: مدخل في لوحة التحكم ← البرامج، ومزيل للبرنامج، واختصارات في قائمة ابدأ،
; ومدخل "pdf2md" في كليك اليمين لويندوز (لهذا المستخدم الذي ثبّت البرنامج).
; لمشاركة المدخل مع كل المستخدمين على جهاز واحد: installer\win\Install-AllUsers.ps1.
;
; البناء:
;   ISCC.exe /DPdf2mdExe=<path-to-pdf2md.exe> /DOutputDir=<dir> /DAppVersion=<x.y.z> pdf2md.iss

#define AppName "pdf2md"
#define AppPublisher "Enezy1"
#define AppExeName "pdf2md.exe"
#define MyAppURL "https://github.com/Enezy1/pdf2md"

#ifndef AppVersion
  #define AppVersion "1.0.7"
#endif
#ifndef Pdf2mdExe
  #error Pdf2mdExe is required. Use: ISCC.exe /DPdf2mdExe=<path> /DOutputDir=<dir> /DAppVersion=<x.y.z> pdf2md.iss
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

[Setup]
AppId={{8C4F2A67-9E3B-4D5A-B8F1-7E2C6D903A41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename={#AppName}-Setup
SetupIconFile=..\assets\pdf2md.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=تحويل ملفات PDF إلى Markdown
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "arabic"; MessagesFile: "compiler:Languages\Arabic.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "contextmenu"; Description: "{cm:CreateContextMenuEntry}"; GroupDescription: "{cm:AdditionalTasks}"

[Files]
Source: "{#Pdf2mdExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\pdf2md.ico"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "win\Install-ContextMenu.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "win\pdf2md-open.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "win\pdf2md-doctor.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "win\Diagnose.cmd"; DestDir: "{app}\installer"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\تشخيص {#AppName}"; Filename: "{app}\installer\Diagnose.cmd"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; تسجيل مدخل "pdf2md" في كليك اليمين — للمستخدم الذي ثبّت البرنامج.
; HKCU، فلا يحتاج صلاحيات مدير، فيحصل كل من يثبّت البرنامج على المدخل.
; المدخل المسجّل يستدعي البرنامج مباشرة (بلا PowerShell مخفي)، فيقلّ احتمال
; أن يفشل بلا سبب ظاهر على جهاز فيه قيود. والسكربت يتحقق من الكتابة فعلاً
; ويخرج بخطأ إن لم تُسجَّل — فيعرض المثبّت رسالة بدل الفشل الصامت.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\Install-ContextMenu.ps1"" -Scope CurrentUser -AppExe ""{app}\{#AppExeName}"""; Flags: runhidden; Tasks: contextmenu

[UninstallRun]
; حذف مدخل الكليك اليمين قبل حذف الملفات.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\Install-ContextMenu.ps1"" -Uninstall"; Flags: runhidden; RunOnceId: "RemovePdf2mdContextMenu"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[CustomMessages]
english.CreateContextMenuEntry=Add the "pdf2md" entry to the Windows right-click menu
arabic.CreateContextMenuEntry=إضافة مدخل "pdf2md" إلى قائمة الكليك اليمين في ويندوز
english.AdditionalTasks=Additional tasks
arabic.AdditionalTasks=مهام إضافية

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  { إعدادات البرنامج ومفاتيحه: لا تُحذف إلا بطلب صريح من المستخدم.
    في الوضع الصامت (UninstallSilent) لا نعرض سؤالاً ولا نحذف شيئاً. }
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
  begin
    DataDir := ExpandConstant('{userappdata}\pdf2md');
    if DirExists(DataDir) then
      if MsgBox('هل تريد حذف إعدادات البرنامج ومفاتيحه المحفوظة؟' + #13#10 + DataDir,
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
