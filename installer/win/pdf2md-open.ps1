<#
    pdf2md-open.ps1
    ---------------------------------------------------------------------------
    Opens the pdf2md desktop app on the file or folder that was clicked in
    Windows Explorer.

    The Explorer right-click entries ("pdf2md") call this script with the
    path of the item that was clicked, and the app opens with exactly that
    target: a folder is loaded as the working folder with its PDF and image
    files queued for conversion, a file is added to the queue and scanned.

    The app is started from its built executable when that executable is at
    least as new as the sources. Otherwise it runs straight from the sources,
    which is the same thing you get by double clicking main.py, so a change in
    the code takes effect without rebuilding.

    Usage:
        powershell -NoProfile -ExecutionPolicy Bypass -File pdf2md-open.ps1 "<full path>"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string] $TargetPath
)

$ErrorActionPreference = 'Stop'

$debugLog = Join-Path $env:TEMP 'pdf2md-open.log'
function Write-Debug {
    param([string] $Text)
    try { Add-Content -LiteralPath $debugLog -Value ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Text) -Encoding UTF8 } catch {}
}
Write-Debug ("start target='{0}'" -f $TargetPath)

# Ø¬Ø°Ø± Ø§Ù„Ù…Ø´Ø±ÙˆØ¹: Ù…Ø¬Ù„Ø¯ Ø§Ù„Ù…Ø³ØªÙˆØ¯Ø¹ (installer\win\..\..) Ø¥Ù† ÙƒØ§Ù†Øª ÙÙŠÙ‡ main.pyØŒ ÙˆØ¥Ù„Ø§ Ø§Ù„Ù…Ø³Ø§Ø± Ø§Ù„Ù…Ø«Ø¨ÙŽÙ‘Øª.
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (Test-Path -LiteralPath (Join-Path $repoRoot 'main.py')) {
    $projectRoot = $repoRoot
} else {
    $projectRoot = 'D:\MARKDOWN\pdf2md'
}
$builtExe    = Join-Path $projectRoot 'dist\pdf2md.exe'
$entryPoint  = Join-Path $projectRoot 'main.py'

function Resolve-Pythonw {
    $cmd = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($cmd) {
        $candidate = Join-Path (Split-Path -Parent $cmd.Source) 'pythonw.exe'
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    $guess = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\pythonw.exe'
    if (Test-Path -LiteralPath $guess) { return $guess }
    return $null
}

try {
    $resolved = (Resolve-Path -LiteralPath $TargetPath -ErrorAction Stop).ProviderPath
} catch {
    exit 1
}

if (-not (Test-Path -LiteralPath $projectRoot)) {
    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show(
        "مجلد المشروع غير موجود:`n$projectRoot",
        'pdf2md',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error)
    exit 1
}

# The newest source file decides whether the built exe is still up to date.
$sourceFiles = @()
$sourceFiles += Get-Item -LiteralPath $entryPoint -ErrorAction SilentlyContinue
$sourceFiles += Get-ChildItem -LiteralPath (Join-Path $projectRoot 'app') -Recurse -Filter '*.py' -File -ErrorAction SilentlyContinue
$newestSource = ($sourceFiles | Measure-Object -Property LastWriteTime -Maximum).Maximum

$useBuiltExe = $false
if (Test-Path -LiteralPath $builtExe) {
    $useBuiltExe = (Get-Item -LiteralPath $builtExe).LastWriteTime -ge $newestSource
}

if ($useBuiltExe) {
    Write-Debug ("using built exe: {0}" -f $builtExe)
    $env:PDF2MD_TARGET = $resolved
    Start-Process -FilePath $builtExe -ArgumentList ('"{0}"' -f $resolved) | Out-Null
    exit 0
}

$pythonw = Resolve-Pythonw
if (-not $pythonw) {
    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show(
        "ما قدرت أشغّل pdf2md: pythonw.exe غير موجود.`nثبّت Python أو ابني التطبيق من pdf2md.spec.",
        'pdf2md',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error)
    exit 1
}

# المسار يمرّ مرتين: كوسائط سطر الأوامر، وكمتغير بيئة يرثه الابن دائماً.
$env:PDF2MD_TARGET = $resolved
Start-Process -FilePath $pythonw `
              -ArgumentList @('"{0}"' -f $entryPoint, '"{0}"' -f $resolved) `
              -WorkingDirectory $projectRoot | Out-Null
Write-Debug ("launched: {0} {1} {2}" -f $pythonw, $entryPoint, $resolved)
exit 0
