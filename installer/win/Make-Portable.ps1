<#
    Make-Portable.ps1
    ---------------------------------------------------------------------------
    Builds a portable copy of pdf2md: one folder that holds the program and
    two small scripts, with nothing installed on the machine.

    The folder can be copied to a USB stick or to any place on the machine.
    Running Register.cmd inside it adds the Explorer right-click entry for the
    account that runs it (no administrator rights needed), and Unregister.cmd
    removes it again.

    The program itself stays portable: it writes nothing outside the folder
    except its own settings under %APPDATA%\pdf2md. The right-click entry is
    a registry entry, so it only works while the folder stays where it is.

    Run:
        powershell -NoProfile -ExecutionPolicy Bypass -File Make-Portable.ps1

    Optional:
        -SourceExe "path\to\pdf2md.exe"  Take the program from somewhere else.
        -OutputDir  "path\to\folder"     Write the portable copy somewhere else.
#>
[CmdletBinding()]
param(
    [string] $SourceExe = '',
    [string] $OutputDir = ''
)

$ErrorActionPreference = 'Stop'

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptRoot)

function Resolve-SourceExe {
    if ($SourceExe) { return (Get-Item -LiteralPath $SourceExe).FullName }
    foreach ($candidate in @(
        (Join-Path $projectRoot 'dist\pdf2md.exe'),
        (Join-Path $projectRoot 'dist_new\pdf2md.exe'))) {
        if (Test-Path -LiteralPath $candidate) { return (Get-Item -LiteralPath $candidate).FullName }
    }
    throw 'No built executable found. Build it with PyInstaller (pdf2md.spec) first.'
}

$exe = Resolve-SourceExe
if (-not $OutputDir) { $OutputDir = Join-Path $projectRoot 'dist-portable\pdf2md-portable' }
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$OutputDir = (Get-Item -LiteralPath $OutputDir).FullName

Write-Host "Portable folder : $OutputDir"
Write-Host "Executable      : $exe"

Copy-Item -LiteralPath $exe -Destination (Join-Path $OutputDir 'pdf2md.exe') -Force
foreach ($script in @('Install-ContextMenu.ps1', 'pdf2md-open.ps1', 'Install-AllUsers.ps1')) {
    Copy-Item -LiteralPath (Join-Path $scriptRoot $script) -Destination (Join-Path $OutputDir $script) -Force
}
$icon = Join-Path $projectRoot 'assets\pdf2md.ico'
if (Test-Path -LiteralPath $icon) {
    Copy-Item -LiteralPath $icon -Destination (Join-Path $OutputDir 'pdf2md.ico') -Force
}

$registerCmd = @(
    '@echo off',
    'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-ContextMenu.ps1" -Portable',
    'echo.',
    'pause'
) -join "`r`n"
[System.IO.File]::WriteAllText((Join-Path $OutputDir 'Register.cmd'), $registerCmd + "`r`n", (New-Object System.Text.UTF8Encoding($false)))

$unregisterCmd = @(
    '@echo off',
    'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-ContextMenu.ps1" -Portable -Uninstall',
    'echo.',
    'pause'
) -join "`r`n"
[System.IO.File]::WriteAllText((Join-Path $OutputDir 'Unregister.cmd'), $unregisterCmd + "`r`n", (New-Object System.Text.UTF8Encoding($false)))

$readme = @"
pdf2md — نسخة بروتابل
====================

المجلد هذا يحتوي البرنامج كاملاً (pdf2md.exe). انسخه أينما شئت: فلاشة، سطح
المكتب، أو أي مجلد على أي جهاز فيه ويندوز.

لا يوجد أي تثبيت: البرنامج لا يكتب إلا إعداداته في %APPDATA%\pdf2md.
مدخل الكليك اليمين (pdf2md) هو استثناء لأنه مدخل في الريجستري، ويجب أن
يبقى مسار هذا المجلد ثابتاً.

التسجيل (للمستخدم الحالي فقط، بدون صلاحيات مدير)
-------------------------------------------------
انقر مرتين على:  Register.cmd
ثم انقر بالزر الأيمن على أي ملف أو مجلد (أو على مساحة فارغة داخل مجلد)،
ستجد المدخل تحت "عرض المزيد من الخيارات" (Show more options) ثم "pdf2md".

الحذف
-----
انقر مرتين على:  Unregister.cmd

للوصول إلى كل المستخدمين على الجهاز
-----------------------------------
1) ضع المجلد في مكان يقرأه الجميع، مثل C:\Tools\pdf2md
2) انقر بالزر الأيمن على Install-AllUsers.ps1 ثم "Run with PowerShell"
   (أو شغّله من PowerShell مرفوع الصلاحيات).
3) ستظهر نافذة UAC — وافق عليها.

ملاحظة: إذا نقلت أو حذفت هذا المجلد بعد التسجيل، سيبقى مدخل الكليك اليمين
موجوداً لكنه لن يفتح شيئاً. شغّل Unregister.cmd أولاً، أو انقل المجلد إلى
مكانه الجديد وأعد التسجيل.

pdf2md — portable copy
=====================

This folder holds the whole program (pdf2md.exe). Copy it anywhere: a USB
stick, the desktop, any folder on any Windows machine.

Nothing is installed: the program only writes its settings under
%APPDATA%\pdf2md. The right-click entry is the one exception, because it is
a registry entry - the path of this folder has to stay the same.

Register (current user only, no administrator rights)
-----------------------------------------------------
Double click:  Register.cmd
Then right-click any file or folder (or the empty space inside a folder) ->
"Show more options" -> "pdf2md".

Remove
------
Double click:  Unregister.cmd

For every user on the machine
-----------------------------
1) Put the folder somewhere everyone can read, e.g. C:\Tools\pdf2md
2) Right-click Install-AllUsers.ps1 -> "Run with PowerShell"
3) Approve the UAC prompt.
"@
Set-Content -LiteralPath (Join-Path $OutputDir 'README.txt') -Value $readme -Encoding UTF8

Write-Host ''
Get-ChildItem -LiteralPath $OutputDir | Select-Object Name, @{n='MB';e={[math]::Round($_.Length / 1MB, 2)}} | Format-Table -AutoSize
Write-Host 'Done.'
