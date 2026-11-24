<#
    pdf2md-open.ps1
    ---------------------------------------------------------------------------
    Opens the pdf2md desktop app on the file or folder that was clicked in
    Windows Explorer.

    The Explorer right-click entries ("pdf2md") call this script with the
    path of the item that was clicked, and the app opens with exactly that
    target: a folder is loaded as the working folder with its PDF and image
    files queued for conversion, a file is added to the queue and scanned.

    The program is located automatically, so the same script works in every
    layout:

      1. %PDF2MD_EXE%                -> explicit override.
      2. pdf2md.exe next to this script        -> portable copy.
      3. pdf2md.exe two levels up              -> installed copy
                                                 (%LOCALAPPDATA%\Programs\pdf2md).
      4. dist\pdf2md.exe inside the project    -> development build.

    When the built executable is at least as new as the sources, it is used.
    Otherwise the app runs straight from the sources, which is the same thing
    you get by double clicking main.py, so a change in the code takes effect
    without rebuilding. On a shared machine the executable is preferred even
    when it is older, because the other users have no Python installed.

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

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Resolve-AppExe {
    # ترتيب البحث: تحديد يدوي، ثم بجوار السكربت، ثم المجلدات الأعلى، ثم
    # مجلد البناء داخل المشروع.
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($env:PDF2MD_EXE) { $candidates.Add($env:PDF2MD_EXE) }

    $dir = $scriptDir
    for ($i = 0; $i -lt 4; $i++) {
        $candidates.Add((Join-Path $dir 'pdf2md.exe'))
        $candidates.Add((Join-Path $dir 'dist\pdf2md.exe'))
        $candidates.Add((Join-Path $dir 'dist_new\pdf2md.exe'))
        $parent = Split-Path -Parent $dir
        if (-not $parent -or $parent -eq $dir) { break }
        $dir = $parent
    }

    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        $item = Get-Item -LiteralPath $candidate
        if ($item.PSIsContainer) { continue }
        return $item.FullName
    }
    return $null
}

function Resolve-ProjectRoot {
    # المجلد الذي يحوي main.py: المشروع نفسه أو نسخة التطوير الثابتة.
    $dir = $scriptDir
    for ($i = 0; $i -lt 4; $i++) {
        if (Test-Path -LiteralPath (Join-Path $dir 'main.py')) { return $dir }
        $parent = Split-Path -Parent $dir
        if (-not $parent -or $parent -eq $dir) { break }
        $dir = $parent
    }
    $devRoot = 'D:\MARKDOWN\pdf2md'
    if (Test-Path -LiteralPath (Join-Path $devRoot 'main.py')) { return $devRoot }
    return $null
}

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

function Show-Error {
    param([string] $Text)
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show(
            $Text,
            'pdf2md',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error)
    } catch {}
}

try {
    $resolved = (Resolve-Path -LiteralPath $TargetPath -ErrorAction Stop).ProviderPath
} catch {
    exit 1
}
Write-Debug ("start target='{0}'" -f $resolved)

$exe        = Resolve-AppExe
$projectRoot = Resolve-ProjectRoot
$entryPoint = $null
if ($projectRoot) { $entryPoint = Join-Path $projectRoot 'main.py' }

Write-Debug ("exe='{0}' projectRoot='{1}'" -f $exe, $projectRoot)

# أقدم مصدر في المشروع يحدد هل الـ exe ما زال مطابقاً للكود.
$newestSource = $null
if ($projectRoot -and $entryPoint -and (Test-Path -LiteralPath $entryPoint)) {
    $sourceFiles = @()
    $sourceFiles += Get-Item -LiteralPath $entryPoint -ErrorAction SilentlyContinue
    $sourceFiles += Get-ChildItem -LiteralPath (Join-Path $projectRoot 'app') -Recurse -Filter '*.py' -File -ErrorAction SilentlyContinue
    $newestSource = ($sourceFiles | Measure-Object -Property LastWriteTime -Maximum).Maximum
}

$useBuiltExe = $false
if ($exe) {
    if (-not $newestSource) {
        $useBuiltExe = $true
    } else {
        $exeTime = (Get-Item -LiteralPath $exe).LastWriteTime
        if ($exeTime -ge $newestSource) {
            $useBuiltExe = $true
        } else {
            # المصدر أحدث من الـ exe: نفضّل التشغيل من المصدر، لكن على جهاز
            # مشترك لا يملك بقية المستخدمين بايثون، فالـ exe الموجود أضمن من
            # رسالة خطأ.
            $useBuiltExe = -not (Resolve-Pythonw)
        }
    }
}

if ($useBuiltExe) {
    Write-Debug ("using built exe: {0}" -f $exe)
    # المسار يمرّ عبر وسائط سطر الأوامر، وكمتغير بيئة احتياط.
    $env:PDF2MD_TARGET = $resolved
    Start-Process -FilePath $exe `
                  -ArgumentList ('"{0}"' -f $resolved) `
                  -WorkingDirectory (Split-Path -Parent $exe) | Out-Null
    exit 0
}

if (-not $projectRoot) {
    Show-Error ("ما قدرت أشغّل pdf2md: لم أجد البرنامج.`n`nالمسار المتوقع:`n{0}" -f $scriptDir)
    exit 1
}

$pythonw = Resolve-Pythonw
if (-not $pythonw) {
    Show-Error "ما قدرت أشغّل pdf2md: pythonw.exe غير موجود.`nثبّت Python أو ابني التطبيق من pdf2md.spec."
    exit 1
}

$env:PDF2MD_TARGET = $resolved
Start-Process -FilePath $pythonw `
              -ArgumentList @('"{0}"' -f $entryPoint, '"{0}"' -f $resolved) `
              -WorkingDirectory $projectRoot | Out-Null
Write-Debug ("launched: {0} {1} {2}" -f $pythonw, $entryPoint, $resolved)
exit 0
