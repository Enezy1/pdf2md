<#
    pdf2md-doctor.ps1
    ---------------------------------------------------------------------------
    Diagnoses the pdf2md right-click entry on a machine where it "does nothing".

    Run it on the affected machine:

        powershell -NoProfile -ExecutionPolicy Bypass -File pdf2md-doctor.ps1
        powershell -NoProfile -ExecutionPolicy Bypass -File pdf2md-doctor.ps1 "C:\path\to\file.pdf"

    It checks, in order:

      1. Is the right-click entry registered, and what does it point at?
      2. Does the program at that path still exist?
      3. Is the program blocked (downloaded-from-the-internet mark, Defender)?
      4. Does the program actually start?

    The result is printed, saved to %TEMP%\pdf2md-doctor.txt, and shown in a
    message box. Nothing is changed and nothing is deleted.
#>
[CmdletBinding()]
param(
    [string] $TargetPath = '',
    [switch] $NoLaunch
)

$ErrorActionPreference = 'Continue'
$VerbId = 'MCodeConvertToMarkdown'
$reportPath = Join-Path $env:TEMP 'pdf2md-doctor.txt'
$report = New-Object System.Collections.Generic.List[string]

function Add-Report {
    param([string] $Text, [string] $Level = '')
    $line = if ($Level) { "[$Level] $Text" } else { $Text }
    $report.Add($line)
    Write-Host $line
}

function Show-Result {
    param([string] $Title, [string] $Body)
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show(
            $Body, $Title,
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information)
    } catch {}
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Add-Report ("pdf2md — تشخيص مدخل الكليك اليمين") 'INFO'
Add-Report ("التاريخ: {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) 'INFO'
Add-Report ("المجلد: {0}" -f $scriptDir) 'INFO'
Add-Report ''

# ------------------------------------------------- 1) مدخل الكليك اليمين ---
$verbLocations = @(
    @{ Where = 'HKCU'; Path = "HKCU:\Software\Classes\SystemFileAssociations\.pdf\shell\$VerbId\command";   What = 'ملف .pdf' }
    @{ Where = 'HKCU'; Path = "HKCU:\Software\Classes\Directory\shell\$VerbId\command";                    What = 'مجلد' }
    @{ Where = 'HKCU'; Path = "HKCU:\Software\Classes\Directory\Background\shell\$VerbId\command";        What = 'مساحة فارغة في مجلد' }
    @{ Where = 'HKCU'; Path = "HKCU:\Software\Classes\DesktopBackground\shell\$VerbId\command";           What = 'سطح المكتب' }
    @{ Where = 'HKLM'; Path = "HKLM:\Software\Classes\SystemFileAssociations\.pdf\shell\$VerbId\command";   What = 'ملف .pdf (كل المستخدمين)' }
    @{ Where = 'HKLM'; Path = "HKLM:\Software\Classes\Directory\shell\$VerbId\command";                    What = 'مجلد (كل المستخدمين)' }
    @{ Where = 'HKLM'; Path = "HKLM:\Software\Classes\Directory\Background\shell\$VerbId\command";        What = 'مساحة فارغة (كل المستخدمين)' }
    @{ Where = 'HKLM'; Path = "HKLM:\Software\Classes\DesktopBackground\shell\$VerbId\command";           What = 'سطح المكتب (كل المستخدمين)' }
)

$foundCommands = New-Object System.Collections.Generic.List[string]
Add-Report '1) مدخل الكليك اليمين في الريجستري' 'INFO'
foreach ($loc in $verbLocations) {
    $value = (Get-ItemProperty -LiteralPath $loc.Path -ErrorAction SilentlyContinue).'(default)'
    if ($value) {
        Add-Report ("   مسجّل [{0}] {1}: {2}" -f $loc.Where, $loc.What, $value)
        if (-not $foundCommands.Contains($value)) { $foundCommands.Add($value) }
    } else {
        Add-Report ("   غير موجود [{0}] {1}" -f $loc.Where, $loc.What) 'WARN'
    }
}
Add-Report ''

# ------------------------------------------- 2) مسار البرنامج في المدخل ---
$referencedExe = $null
foreach ($cmd in $foundCommands) {
    if ($cmd -match '"([^"]+\.exe)"') {
        $referencedExe = $Matches[1]
        break
    }
}

$installedExe = $null
if ($referencedExe -and (Test-Path -LiteralPath $referencedExe)) {
    $installedExe = (Get-Item -LiteralPath $referencedExe).FullName
    Add-Report ("2) البرنامج الذي يستدعيه المدخل موجود: {0}" -f $installedExe) 'OK'
} else {
    if ($referencedExe) {
        Add-Report ("2) مدخل الكليك اليمين يستدعي ملفاً غير موجود: {0}" -f $referencedExe) 'ERROR'
        Add-Report '   السبب: نُقل المجلد أو حُذف بعد التسجيل. الحل: شغّل Register.cmd من مجلد' 'INFO'
        Add-Report '   البرنامج (أو أعد تثبيته) ليتسجّل المدخل من جديد.' 'INFO'
    } else {
        Add-Report '2) مدخل الكليك اليمين غير مسجّل أصلاً على هذا الجهاز.' 'ERROR'
        Add-Report '   الحل: شغّل Register.cmd داخل مجلد البرنامج، أو أعد تثبيت النسخة.' 'INFO'
    }
}

# البحث عن البرنامج في أماكنه المعتادة، حتى لو لم يكن مسجّلاً.
if (-not $installedExe) {
    foreach ($candidate in @(
        (Join-Path $scriptDir 'pdf2md.exe'),
        (Join-Path (Split-Path -Parent $scriptDir) 'pdf2md.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\pdf2md\pdf2md.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\pdf2md\installer\pdf2md.exe'))) {
        if (Test-Path -LiteralPath $candidate) {
            $installedExe = (Get-Item -LiteralPath $candidate).FullName
            Add-Report ("   لكن البرنامج موجود هنا: {0}" -f $installedExe) 'WARN'
            break
        }
    }
}
Add-Report ''

# ------------------------------------------------ 3) حجب ومضاد الفيروسات ---
Add-Report '3) حالة الحجب' 'INFO'
if ($installedExe) {
    $zone = Get-Content -LiteralPath $installedExe -Stream Zone.Identifier -ErrorAction SilentlyContinue
    if ($zone) {
        Add-Report ("   الملف محمّل من الإنترنت (Mark of the Web) — قد يحذبه SmartScreen أو Defender:") 'WARN'
        $zone | ForEach-Object { Add-Report ("      {0}" -f $_) 'WARN' }
    } else {
        Add-Report '   لا يوجد Mark of the Web على الملف.' 'OK'
    }
    try {
        $mp = Get-MpComputerStatus -ErrorAction Stop
        Add-Report ("   الحماية من الفيروسات: Real-time = {0}" -f $mp.RealTimeProtectionEnabled) 'INFO'
    } catch {
        Add-Report '   تعذّر قراءة حالة Microsoft Defender (قد يحتاج صلاحيات مدير).' 'INFO'
    }
    try {
        $detections = @(Get-MpThreatDetection -ErrorAction Stop |
                        Where-Object { ($_.Resources -join ' ') -match 'pdf2md' })
        if ($detections.Count -gt 0) {
            Add-Report ("   Defender حذف/حجب ملفات pdf2md ({0} حالة):" -f $detections.Count) 'ERROR'
            foreach ($d in $detections) {
                Add-Report ("      {0}" -f (($d.Resources | Select-Object -First 1) -join '')) 'ERROR'
            }
        }
    } catch {
        # لا صلاحيات أو Defender غير مفعّل — لا بأس.
    }
} else {
    Add-Report '   لا يوجد ملف برنامج لفحصه.' 'WARN'
}
Add-Report ''

# ------------------------------------------------- 4) هل يعمل البرنامج؟ ---
$launchOk = $false
$launched = $false
$launchError = ''
Add-Report '4) تشغيل البرنامج' 'INFO'
if ($installedExe -and -not $NoLaunch) {
    $argList = @()
    if ($TargetPath) { $argList = @(('"{0}"' -f $TargetPath)) }
    try {
        if ($argList.Count -gt 0) {
            $proc = Start-Process -FilePath $installedExe -ArgumentList $argList -PassThru -ErrorAction Stop
        } else {
            $proc = Start-Process -FilePath $installedExe -PassThru -ErrorAction Stop
        }
        # النسخة المجمّعة (onefile) تفكّ نفسها في مجلّد مؤقّت عند أول تشغيل،
        # فالنافذة قد تتأخر عشر ثوانٍ. ننتظر ونراقب بدل الفحص مرة واحدة.
        $window = $null
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Milliseconds 500
            $window = Get-Process -Name 'pdf2md' -ErrorAction SilentlyContinue |
                      Where-Object { $_.MainWindowTitle } | Select-Object -First 1
            if ($window) { break }
            if ($proc.HasExited) { break }
        }
        if ($window) {
            $launchOk = $true
            $launched = $true
            Add-Report ("   البرنامج فتح نافذة بنجاح: {0}" -f $window.MainWindowTitle) 'OK'
        } elseif ($proc.HasExited) {
            $launchError = "البرنامج انطلق ثم أُغلق فوراً (رمز الخروج {0})." -f $proc.ExitCode
            Add-Report ("   {0}" -f $launchError) 'ERROR'
            Add-Report '   السبب غالباً: مضاد الفيروسات حذف الملف، أو الملف ناقص/تالف.' 'INFO'
        } else {
            $launched = $true
            Add-Report '   البرنامج ما زال يعمل لكن لم تظهر نافذة — قد يكون بطيئاً (أول تشغيل يفكّ الملف).' 'WARN'
        }
    } catch {
        $launchError = $_.Exception.Message
        Add-Report ("   فشل التشغيل: {0}" -f $launchError) 'ERROR'
    }
} elseif ($NoLaunch) {
    Add-Report '   (تم تخطي التشغيل)' 'INFO'
} else {
    Add-Report '   لا يوجد برنامج لتشغيله.' 'ERROR'
}
Add-Report ''

# ------------------------------------------------------------ الخلاصة -----
$verdict = 'مدخل الكليك اليمين يعمل.'
if (-not $foundCommands.Count) {
    $verdict = "المدخل غير مسجّل على هذا الجهاز. شغّل Register.cmd داخل مجلد pdf2md (أو أعد تثبيت النسخة)."
} elseif (-not $installedExe) {
    $verdict = "المسار المسجّل لم يعد موجوداً. أعد تسجيل المدخل: Register.cmd داخل مجلد pdf2md."
} elseif ($launchOk) {
    $verdict = "البرنامج يفتح بنجاح. إن كان المدخل لا يستجيب، أعد تسجيله: Register.cmd داخل مجلد pdf2md."
} elseif ($launched) {
    $verdict = "البرنامج اشتغل لكن النافذة لم تظهر. جرّب تشغيل pdf2md.exe مباشرة من مجلد البرنامج؛ إن لم يفتح، راجع قسم 3 (مضاد الفيروسات)."
} elseif (-not $NoLaunch) {
    $verdict = "البرنامج موجود لكنه لا يفتح. السبب: {0} راجع قسم 3 — مضاد الفيروسات قد يحذفه أو يحجبه. أضف استثناءً لمجلد C:\Users\<اسمك>\AppData\Local\Programs\pdf2md ثم أعد المحاولة." -f $launchError
}
Add-Report ("الخلاصة: {0}" -f $verdict) 'RESULT'

try {
    $report | Set-Content -LiteralPath $reportPath -Encoding UTF8
} catch {}
Add-Report ("التقرير الكامل: {0}" -f $reportPath) 'INFO'

Show-Result -Title 'pdf2md — التشخيص' -Body ($verdict + "`n`nالتقرير: " + $reportPath)
Write-Host ''
Write-Host ("Report saved to: {0}" -f $reportPath)
