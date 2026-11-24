<#
    Install-AllUsers.ps1
    ---------------------------------------------------------------------------
    Registers the pdf2md right-click entry for every user on this machine.

    The entry is written under HKEY_LOCAL_MACHINE, so it shows up in the
    Explorer right-click menu for all accounts, not only the one that runs
    this script. This needs administrator rights, so the script asks Windows
    for the elevation itself (the blue "User Account Control" dialog) and
    then does the work in the elevated window.

    The program has to live in a folder every user can read and execute. The
    executable is looked up automatically next to the installer scripts; use
    -AppExe when it lives somewhere else.

    Run:
        powershell -NoProfile -ExecutionPolicy Bypass -File Install-AllUsers.ps1
        powershell -NoProfile -ExecutionPolicy Bypass -File Install-AllUsers.ps1 -Uninstall

    Optional:
        -AppExe "path\to\pdf2md.exe"    Use this executable.
        -Extensions "pdf,png"           Limit / extend the file types.
        -Label "pdf2md"                 Use a different menu label.
        -Uninstall                      Remove the entry again (both scopes).
#>
[CmdletBinding()]
param(
    [string] $AppExe = '',
    [string] $Extensions = 'pdf,png,jpg,jpeg,tif,tiff,bmp,webp',
    [string] $Label = 'pdf2md',
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $scriptDir 'Install-ContextMenu.ps1'
if (-not (Test-Path -LiteralPath $target)) {
    throw "Installer not found next to this script: $target"
}

function Test-Administrator {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$argString = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -Scope AllUsers -Extensions "{1}" -Label "{2}"' -f $target, $Extensions, $Label
if ($AppExe) { $argString += ' -AppExe "{0}"' -f $AppExe }
if ($Uninstall) { $argString += ' -Uninstall' }

if (-not (Test-Administrator)) {
    Write-Host ''
    Write-Host 'سأطلب من ويندوز صلاحيات المدير (نافذة UAC) لتسجيل مدخل الكليك اليمين لكل المستخدمين.'
    Write-Host ''
    $powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    try {
        $proc = Start-Process -FilePath $powershell -Verb RunAs -ArgumentList $argString -Wait -PassThru
    } catch {
        Write-Host 'تم رفض طلب الصلاحيات أو فشلت الترقية. لم يتغيّر أي شيء.'
        exit 1
    }
    if ($proc.ExitCode -ne 0) {
        Write-Host "انتهت العملية المرفوعة بالرمز $($proc.ExitCode). راجع نافذة ويندوز التي ظهرت للتفاصيل."
        exit $proc.ExitCode
    }
    Write-Host ''
    Write-Host 'تم. مدخل "pdf2md" الآن ظاهر في كليك اليمين لكل المستخدمين على هذا الجهاز.'
    exit 0
}

# نسخة مرفوعة: نكتب في HKEY_LOCAL_MACHINE.
try {
    & $target -Scope AllUsers -Extensions $Extensions -Label $Label -AppExe $AppExe -Uninstall:$Uninstall
    $code = 0
} catch {
    Write-Host ''
    Write-Host ("فشل التسجيل: {0}" -f $_.Exception.Message)
    $code = 1
}

Write-Host ''
if ($Uninstall) {
    Write-Host 'تم حذف مدخل "pdf2md" من كليك اليمين (للمستخدمين جميعاً).'
} else {
    Write-Host 'تم تسجيل مدخل "pdf2md" في كليك اليمين لكل المستخدمين على هذا الجهاز.'
}
Write-Host 'اضغط Enter لإغلاق النافذة...'
try { $null = Read-Host } catch {}
exit $code
