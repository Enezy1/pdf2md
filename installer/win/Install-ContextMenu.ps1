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

    Everything is written under HKEY_CURRENT_USER, so no administrator rights
    are needed and nothing outside your user profile is touched.

    Run:
        powershell -NoProfile -ExecutionPolicy Bypass -File Install-ContextMenu.ps1

    Optional:
        -Extensions "pdf,png"   Limit / extend the file types.
        -Label "pdf2md"         Use a different menu label.
        -Icon "path\to\icon.ico"  Use a different icon.
        -SkipFolders            Do not register the folder entries.
        -Uninstall              Remove the entries again.
#>
[CmdletBinding()]
param(
    [string] $Extensions = 'pdf,png,jpg,jpeg,tif,tiff,bmp,webp',
    [string] $Label = 'pdf2md',
    [string] $Icon = '',
    [switch] $SkipFolders,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

$VerbId = 'MCodeConvertToMarkdown'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $scriptRoot 'pdf2md-open.ps1'
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Launcher not found next to this script: $launcher"
}

# The app's own icon, so the menu entry looks exactly like the program.
# Ø¬Ø°Ø± Ø§Ù„ØªØ·Ø¨ÙŠÙ‚: Ù…Ø¬Ù„Ø¯ Ø§Ù„Ù…Ø³ØªÙˆØ¯Ø¹ (installer\win\..\..) Ø¥Ù† ÙƒØ§Ù†Øª ÙÙŠÙ‡ main.pyØŒ ÙˆØ¥Ù„Ø§ Ø§Ù„Ù…Ø³Ø§Ø± Ø§Ù„Ù…Ø«Ø¨ÙŽÙ‘Øª.
$appRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path -LiteralPath (Join-Path $appRoot 'main.py'))) {
    $appRoot = 'D:\MARKDOWN\pdf2md'
}
$appIcon = Join-Path $appRoot 'assets\pdf2md.ico'

function Get-PowerShellHost {
    # pwsh is nicer to hide, but powershell.exe is always present on Windows.
    $pwsh = Get-Command 'pwsh.exe' -ErrorAction SilentlyContinue
    if ($pwsh) { return $pwsh.Source }
    return "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
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

$shell = Get-PowerShellHost

$iconPath = $Icon
if (-not $iconPath) { $iconPath = $appIcon }
if (Test-Path -LiteralPath $iconPath) {
    $iconValue = $iconPath
} else {
    $iconValue = "$appRoot\dist\pdf2md.exe,0"
    Write-Host "icon not found, falling back to the program icon: $iconPath"
}

# %1 is the item that was clicked (the file, or the folder icon).
# %V is the folder whose background was clicked (same convention the built-in
# "Open in Terminal" entry uses).
$commandTemplate = '{0} -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{1}" "{2}"'
$commandForItem = $commandTemplate -f $shell, $launcher, '%1'
$commandForFolder = $commandTemplate -f $shell, $launcher, '%V'

$folderVerbs = @(
    @{ Path = "HKCU:\Software\Classes\Directory\shell\$VerbId";             Command = $commandForItem;   Note = 'right-click a folder' }
    @{ Path = "HKCU:\Software\Classes\Directory\Background\shell\$VerbId"; Command = $commandForFolder; Note = 'empty space inside a folder' }
    @{ Path = "HKCU:\Software\Classes\DesktopBackground\shell\$VerbId";    Command = $commandForFolder; Note = 'empty space on the desktop' }
)

$extensionList = @(
    $Extensions -split '[,\s]+' |
    ForEach-Object { $_.Trim().TrimStart('.').ToLowerInvariant() } |
    Where-Object { $_ }
)
if ($extensionList.Count -eq 0) { throw 'No extensions given.' }

if ($Uninstall) {
    foreach ($ext in $extensionList) {
        foreach ($base in @(
            "HKCU:\Software\Classes\SystemFileAssociations\.$ext\shell\$VerbId",
            "HKCU:\Software\Classes\.$ext\shell\$VerbId")) {
            if (Test-Path -LiteralPath $base) {
                Remove-Item -LiteralPath $base -Recurse -Force
                Write-Host "removed: $base"
            }
        }
    }
    foreach ($verb in $folderVerbs) {
        if (Test-Path -LiteralPath $verb.Path) {
            Remove-Item -LiteralPath $verb.Path -Recurse -Force
            Write-Host "removed: $($verb.Path)"
        }
    }
    Write-Host ''
    Write-Host 'Context menu entry removed.'
    exit 0
}

foreach ($ext in $extensionList) {
    # SystemFileAssociations adds the verb without touching the ProgID, so the
    # file keeps opening with whatever app it uses today.
    $key = "HKCU:\Software\Classes\SystemFileAssociations\.$ext\shell\$VerbId"
    Set-ShellVerb -KeyPath $key -VerbLabel $Label -VerbIcon $iconValue -VerbCommand $commandForItem

    # Older/leftover location: remove it so the entry is not listed twice.
    $legacy = "HKCU:\Software\Classes\.$ext\shell\$VerbId"
    if (Test-Path -LiteralPath $legacy) {
        Remove-Item -LiteralPath $legacy -Recurse -Force
    }
    Write-Host "registered: .$ext"
}

# Types that were registered before but are not in the list get removed, so the
# menu never offers a file that pdf2md cannot open.
$associationsRoot = 'HKCU:\Software\Classes\SystemFileAssociations'
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

Write-Host ''
Write-Host "Menu label      : $Label"
Write-Host "Icon            : $iconValue"
Write-Host "Command         : $commandForItem"
Write-Host "File types      : $($extensionList.Count)"
Write-Host "Launcher        : $launcher"
Write-Host ''
Write-Host 'Done. Right-click a file or a folder -> Show more options -> pdf2md.'
