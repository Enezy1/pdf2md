<#
    Install-ContextMenu.ps1
    ---------------------------------------------------------------------------
    Adds the Windows Explorer right-click entry for pdf2md.

    The entry is called "pdf2md" and it opens the pdf2md desktop app on the
    thing that was clicked:

      * a document  -> the app starts with that file in its list, ready to
        convert it to Markdown.
      * a folder    -> the app starts with that folder as the working folder
        and every PDF and image inside it queued for conversion.
      * empty space in a folder, or on the desktop -> the same, for the
        folder that was clicked.

    How the entry is launched
    -------------------------
    When the path of the program is known, the entry calls the executable
    directly:

        "C:\...\pdf2md.exe" "%1"

    That is deliberate. The earlier version went through a hidden
    PowerShell window, which meant that any policy on that machine (AppLocker,
    WDAC, a restricted execution policy) silently broke the entry, and a
    failure produced no message at all. Calling the program directly removes
    PowerShell from the path and lets Explorer report a launch failure.

    The PowerShell launcher (pdf2md-open.ps1) is still used in the development
    layout, where the program may run from the sources, and it can be forced
    with -UseLauncher.

    Scopes
    -------
      CurrentUser (default)
          Written under HKEY_CURRENT_USER. Only the account that runs this
          script sees the entry, and no administrator rights are needed.

      AllUsers
          Written under HKEY_LOCAL_MACHINE. Every account on the machine sees
          the entry. Needs administrator rights, and the program must live in
          a folder every user can read and execute (the exe is looked up
          automatically, or given with -AppExe).

    Run:
        powershell -NoProfile -ExecutionPolicy Bypass -File Install-ContextMenu.ps1
        powershell -NoProfile -ExecutionPolicy Bypass -File Install-ContextMenu.ps1 -Scope AllUsers
        powershell -NoProfile -ExecutionPolicy Bypass -File Install-ContextMenu.ps1 -Portable

    Optional:
        -Scope "CurrentUser|AllUsers"  Who sees the entry (default CurrentUser).
        -Portable                       Register the copy next to this script,
                                        for the current user, without admin.
        -AppExe "path\to\pdf2md.exe"    Use this executable instead of the
                                        automatic lookup.
        -Extensions "pdf,png"           Limit / extend the file types.
        -Label "pdf2md"                 Use a different menu label.
        -Icon "path\to\icon.ico"        Use a different icon.
        -SkipFolders                    Do not register the folder entries.
        -UseLauncher                    Launch through the PowerShell script
                                        instead of calling the exe directly.
        -Uninstall                      Remove the entries again (both scopes).
#>
[CmdletBinding()]
param(
    [string] $Extensions = 'pdf,png,jpg,jpeg,tif,tiff,bmp,webp',
    [string] $Label = 'pdf2md',
    [string] $Icon = '',
    [string] $AppExe = '',
    [ValidateSet('CurrentUser', 'AllUsers')]
    [string] $Scope = 'CurrentUser',
    [switch] $Portable,
    [switch] $SkipFolders,
    [switch] $UseLauncher,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

$VerbId = 'MCodeConvertToMarkdown'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $scriptRoot 'pdf2md-open.ps1'

if ($Portable) { $Scope = 'CurrentUser' }

function Get-PowerShellHost {
    # pwsh is nicer to hide, but powershell.exe is always present on Windows.
    $pwsh = Get-Command 'pwsh.exe' -ErrorAction SilentlyContinue
    if ($pwsh) { return $pwsh.Source }
    return "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
}

function Test-Administrator {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-AppExe {
    # نفس ترتيب البحث في pdf2md-open.ps1: بجوار السكربت، ثم المجلدات الأعلى
    # (النسخة المثبتة)، ثم مجلد البناء داخل المشروع.
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($AppExe) { $candidates.Add($AppExe) }
    if ($env:PDF2MD_EXE) { $candidates.Add($env:PDF2MD_EXE) }

    $dir = $scriptRoot
    for ($i = 0; $i -lt 3; $i++) {
        $candidates.Add((Join-Path $dir 'pdf2md.exe'))
        $parent = Split-Path -Parent $dir
        if (-not $parent -or $parent -eq $dir) { break }
        $dir = $parent
    }

    $appRoot = Split-Path -Parent (Split-Path -Parent $scriptRoot)
    $candidates.Add((Join-Path $appRoot 'dist\pdf2md.exe'))
    $candidates.Add((Join-Path $appRoot 'dist_new\pdf2md.exe'))

    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        $item = Get-Item -LiteralPath $candidate
        if ($item.PSIsContainer) { continue }
        return $item.FullName
    }
    return $null
}

function Set-ShellVerb {
    param(
        [string] $KeyPath,
        [string] $VerbLabel,
        [string] $VerbIcon,
        [string] $VerbCommand
    )
    if (Test-Path -LiteralPath $KeyPath) {
        Remove-Item -LiteralPath $KeyPath -Recurse -Force
    }
    New-Item -Path $KeyPath -Force | Out-Null
    New-ItemProperty -LiteralPath $KeyPath -Name '(default)' -Value $VerbLabel -PropertyType String -Force | Out-Null
    if ($VerbIcon) {
        New-ItemProperty -LiteralPath $KeyPath -Name 'Icon' -Value $VerbIcon -PropertyType String -Force | Out-Null
    }
    $commandKey = Join-Path $KeyPath 'command'
    New-Item -Path $commandKey -Force | Out-Null
    New-ItemProperty -LiteralPath $commandKey -Name '(default)' -Value $VerbCommand -PropertyType String -Force | Out-Null
}

# ---------------------------------------------------------------- roots ----
$hiveRoot = 'HKCU:\Software\Classes'
$scopeNote = 'this user only'
if ($Scope -eq 'AllUsers') {
    $hiveRoot = 'HKLM:\Software\Classes'
    $scopeNote = 'every user on this machine'
}

if ($Scope -eq 'AllUsers' -and -not (Test-Administrator)) {
    throw ("Scope AllUsers writes to HKEY_LOCAL_MACHINE and needs administrator rights.`n" +
           "Run installer\win\Install-AllUsers.ps1 instead - it asks Windows for the`n" +
           "elevation itself and then does the same job.")
}

$appRoot = Split-Path -Parent (Split-Path -Parent $scriptRoot)
$resolvedExe = Resolve-AppExe

# %1 is the item that was clicked (the file, or the folder icon).
# %V is the folder whose background was clicked (same convention the built-in
# "Open in Terminal" entry uses).
$shell = Get-PowerShellHost
$commandTemplate = '{0} -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{1}" "{2}"'

# تشغيل مباشر للبرنامج بلا PowerShell ولا سكربت وسيط: أقل نقاط فشل، وويندوز
# نفسه يُظهر رسالة إن فشل تشغيل الملف.
$directExe = $null
if ($resolvedExe -and -not $UseLauncher) {
    if ($Portable -or $Scope -eq 'AllUsers' -or $AppExe) {
        $directExe = $resolvedExe
    }
}

if ($directExe) {
    $commandForItem = '"{0}" "{1}"' -f $directExe, '%1'
    $commandForFolder = '"{0}" "{1}"' -f $directExe, '%V'
} else {
    $commandForItem = $commandTemplate -f $shell, $launcher, '%1'
    $commandForFolder = $commandTemplate -f $shell, $launcher, '%V'
}

$folderVerbs = @(
    @{ Path = "$hiveRoot\Directory\shell\$VerbId";             Command = $commandForItem;   Note = 'right-click a folder' }
    @{ Path = "$hiveRoot\Directory\Background\shell\$VerbId"; Command = $commandForFolder; Note = 'empty space inside a folder' }
    @{ Path = "$hiveRoot\DesktopBackground\shell\$VerbId";    Command = $commandForFolder; Note = 'empty space on the desktop' }
)

$extensionList = @(
    $Extensions -split '[,\s]+' |
    ForEach-Object { $_.Trim().TrimStart('.').ToLowerInvariant() } |
    Where-Object { $_ }
)
if ($extensionList.Count -eq 0) { throw 'No extensions given.' }

# ------------------------------------------------------------- uninstall ---
if ($Uninstall) {
    $roots = @('HKCU:\Software\Classes', 'HKLM:\Software\Classes')
    foreach ($ext in $extensionList) {
        foreach ($root in $roots) {
            foreach ($base in @(
                "$root\SystemFileAssociations\.$ext\shell\$VerbId",
                "$root\.$ext\shell\$VerbId")) {
                if (Test-Path -LiteralPath $base) {
                    Remove-Item -LiteralPath $base -Recurse -Force
                    Write-Host "removed: $base"
                }
            }
        }
    }
    foreach ($root in $roots) {
        foreach ($path in @(
            "$root\Directory\shell\$VerbId",
            "$root\Directory\Background\shell\$VerbId",
            "$root\DesktopBackground\shell\$VerbId")) {
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Recurse -Force
                Write-Host "removed: $path"
            }
        }
    }
    Write-Host ''
    Write-Host 'Context menu entry removed (current user and all users).'
    exit 0
}

# ----------------------------------------------------------------- icon ----
$iconPath = $Icon
if (-not $iconPath) {
    # النسخة المحمولة تحمل أيقونتها بجوارها، ومجلد المشروع يحملها في assets.
    foreach ($candidate in @(
        (Join-Path $scriptRoot 'pdf2md.ico'),
        (Join-Path $appRoot 'assets\pdf2md.ico'))) {
        if (Test-Path -LiteralPath $candidate) { $iconPath = $candidate; break }
    }
}
if (-not $iconPath) {
    if ($resolvedExe) {
        $iconValue = "$resolvedExe,0"
    } else {
        $iconValue = "$appRoot\dist\pdf2md.exe,0"
        Write-Host "icon and executable not found, falling back to: $iconValue"
    }
} else {
    $iconValue = (Get-Item -LiteralPath $iconPath).FullName
}

# ------------------------------------------------------------ register -----
foreach ($ext in $extensionList) {
    # SystemFileAssociations adds the verb without touching the ProgID, so the
    # file keeps opening with whatever app it uses today.
    $key = "$hiveRoot\SystemFileAssociations\.$ext\shell\$VerbId"
    Set-ShellVerb -KeyPath $key -VerbLabel $Label -VerbIcon $iconValue -VerbCommand $commandForItem

    # Older/leftover location: remove it so the entry is not listed twice.
    $legacy = "$hiveRoot\.$ext\shell\$VerbId"
    if (Test-Path -LiteralPath $legacy) {
        Remove-Item -LiteralPath $legacy -Recurse -Force
    }
    Write-Host "registered: .$ext"
}

# Types that were registered before but are not in the list get removed, so the
# menu never offers a file that pdf2md cannot open.
$associationsRoot = Join-Path $hiveRoot 'SystemFileAssociations'
Get-ChildItem -LiteralPath $associationsRoot -ErrorAction SilentlyContinue | ForEach-Object {
    $old = Join-Path $_.PSPath "shell\$VerbId"
    if (-not (Test-Path -LiteralPath $old)) { return }
    $ext = $_.PSChildName.TrimStart('.')
    if ($extensionList -contains $ext) { return }
    Remove-Item -LiteralPath $old -Recurse -Force
    Write-Host "removed (unsupported type): .$ext"
}

if (-not $SkipFolders) {
    foreach ($verb in $folderVerbs) {
        Set-ShellVerb -KeyPath $verb.Path -VerbLabel $Label -VerbIcon $iconValue -VerbCommand $verb.Command
        Write-Host "registered: $($verb.Path)  ($($verb.Note))"
    }
}

if ($Scope -eq 'AllUsers') {
    # نسخة المستخدم الواحد تُمسح حتى يبقى مدخل واحد مشترك على الجهاز.
    $userRoots = @(
        "HKCU:\Software\Classes\Directory\shell\$VerbId",
        "HKCU:\Software\Classes\Directory\Background\shell\$VerbId",
        "HKCU:\Software\Classes\DesktopBackground\shell\$VerbId")
    foreach ($ext in $extensionList) {
        $userRoots += "HKCU:\Software\Classes\SystemFileAssociations\.$ext\shell\$VerbId"
    }
    foreach ($path in $userRoots) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force
            Write-Host "removed (per-user copy, the shared entry replaces it): $path"
        }
    }
}

# ------------------------------------------------------------- verify -----
# نقرأ ما كتبناه فعلاً من الريجستري: تسجيل صامت نصف مكتوب هو أسوأ من لا تسجيل.
$verifyKey = "$hiveRoot\SystemFileAssociations\.$($extensionList[0])\shell\$VerbId\command"
$written = (Get-ItemProperty -LiteralPath $verifyKey -ErrorAction SilentlyContinue).'(default)'
if (-not $written) {
    throw "Registration did not stick: $verifyKey is empty."
}
if ($written -ne $commandForItem) {
    throw "Registration mismatch in $verifyKey.`nExpected: $commandForItem`nFound:    $written"
}
Write-Host "verified in registry: $written"

Write-Host ''
Write-Host "Menu label      : $Label"
Write-Host "Visible to      : $scopeNote"
Write-Host "Icon            : $iconValue"
Write-Host "Command         : $commandForItem"
Write-Host "File types      : $($extensionList.Count)"
if ($directExe) { Write-Host "Launch          : the program directly (no PowerShell)" }
elseif ($resolvedExe) { Write-Host "Launch          : through pdf2md-open.ps1" }
if ($resolvedExe) { Write-Host "Executable      : $resolvedExe" }
else { Write-Host "Executable      : (not found, the launcher is used instead)" }
Write-Host ''
Write-Host 'Done. Right-click a file or a folder -> Show more options -> pdf2md.'
