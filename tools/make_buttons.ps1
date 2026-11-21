#Requires -Version 5.1
<#
    make_buttons.ps1
    ---------------------------------------------------------------------------
    Generates the download buttons used at the top of README.md.

    Each button is a PNG with Arabic text drawn by GDI+ (so the Arabic is
    properly shaped - no reshaper needed), and each is linked from the README
    to a "latest" download URL:

        https://github.com/Enezy1/pdf2md/releases/latest/download/<asset>

    That URL always points to the newest release, so the buttons never go
    stale: publish a new tag and the same buttons download the new build.

    Run:
        powershell -NoProfile -ExecutionPolicy Bypass -File tools\make_buttons.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$script:root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$outDir = Join-Path $script:root 'docs\img'
New-Item -ItemType Directory -Path $outDir -Force | Out-Null

$script:fontTitle = [System.Drawing.Font]::new('Segoe UI', 30, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
$script:fontSub = [System.Drawing.Font]::new('Segoe UI', 22, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)

function New-RoundedPath {
    param([double] $X, [double] $Y, [double] $W, [double] $H, [double] $R)
    $p = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $d = $R * 2
    $p.AddArc($X, $Y, $d, $d, 180, 90)
    $p.AddArc(($X + $W - $d), $Y, $d, $d, 270, 90)
    $p.AddArc(($X + $W - $d), ($Y + $H - $d), $d, $d, 0, 90)
    $p.AddArc($X, ($Y + $H - $d), $d, $d, 90, 90)
    $p.CloseFigure()
    return $p
}

function Draw-Button {
    param(
        [string] $File,
        [string] $Title,
        [string] $Subtitle,
        [string] $Top,
        [string] $Bottom,
        [int] $W = 420,
        [int] $H = 128
    )
    $bmp = [System.Drawing.Bitmap]::new($W, $H)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit

    $shadow = New-RoundedPath -X 4 -Y 7 -W ($W - 8) -H ($H - 8) -R 18
    $shadowBrush = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(60, 0, 0, 0))
    $g.FillPath($shadowBrush, $shadow)
    $shadowBrush.Dispose()
    $shadow.Dispose()

    $body = New-RoundedPath -X 4 -Y 4 -W ($W - 8) -H ($H - 12) -R 18
    $grad = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
        [System.Drawing.Point]::new(0, 4), [System.Drawing.Point]::new(0, ($H - 8)),
        [System.Drawing.ColorTranslator]::FromHtml($Top), [System.Drawing.ColorTranslator]::FromHtml($Bottom))
    $g.FillPath($grad, $body)
    $grad.Dispose()

    $gloss = New-RoundedPath -X 8 -Y 7 -W ($W - 16) -H (($H - 12) / 2) -R 14
    $glossBrush = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(26, 255, 255, 255))
    $g.FillPath($glossBrush, $gloss)
    $glossBrush.Dispose()
    $gloss.Dispose()

    $border = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(70, 0, 0, 0), 2)
    $g.DrawPath($border, $body)
    $border.Dispose()
    $body.Dispose()

    $accent = [System.Drawing.Pen]::new([System.Drawing.Color]::FromArgb(200, 255, 255, 255), 3)
    $g.DrawLine($accent, 30, 6, ($W - 30), 6)
    $accent.Dispose()

    # سهم التحميل (مرسوم يدوياً - لا نعتمد على خط رموز)
    $arrowColor = [System.Drawing.Color]::FromArgb(235, 255, 255, 255)
    $ab = [System.Drawing.SolidBrush]::new($arrowColor)
    $ap = [System.Drawing.Pen]::new($arrowColor, 5)
    $ap.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $ax = 52
    $ay = 38
    $g.FillRectangle($ab, ($ax - 8), $ay, 16, 28)
    $g.DrawLines($ap, [System.Drawing.Point[]]@(
            [System.Drawing.Point]::new(($ax - 20), ($ay + 28)),
            [System.Drawing.Point]::new($ax, ($ay + 50)),
            [System.Drawing.Point]::new(($ax + 20), ($ay + 28))))
    $g.DrawLine($ap, ($ax - 24), ($ay + 58), ($ax + 24), ($ay + 58))
    $ab.Dispose()
    $ap.Dispose()

    $sf = [System.Drawing.StringFormat]::new()
    $sf.Alignment = [System.Drawing.StringAlignment]::Center
    $sf.LineAlignment = [System.Drawing.StringAlignment]::Center
    $sf.Trimming = [System.Drawing.StringTrimming]::EllipsisCharacter
    $white = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::White)
    $soft = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(225, 255, 255, 255))

    if ($Subtitle) {
        $r1 = [System.Drawing.RectangleF]::new(96, 24, ($W - 116), 46)
        $g.DrawString($Title, $script:fontTitle, $white, $r1, $sf)
        $r2 = [System.Drawing.RectangleF]::new(96, 68, ($W - 116), 30)
        $g.DrawString($Subtitle, $script:fontSub, $soft, $r2, $sf)
    } else {
        $r1 = [System.Drawing.RectangleF]::new(96, 32, ($W - 116), 62)
        $g.DrawString($Title, $script:fontTitle, $white, $r1, $sf)
    }
    $white.Dispose()
    $soft.Dispose()
    $sf.Dispose()

    $g.Dispose()
    $path = Join-Path $outDir $File
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    Write-Host ("wrote: {0} ({1} bytes)" -f $path, (Get-Item $path).Length)
}

function Draw-Banner {
    param([string] $File, [int] $W = 900, [int] $H = 180)
    $bmp = [System.Drawing.Bitmap]::new($W, $H)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit

    $grad = [System.Drawing.Drawing2D.LinearGradientBrush]::new(
        [System.Drawing.Point]::new(0, 0), [System.Drawing.Point]::new($W, $H),
        [System.Drawing.Color]::FromArgb(15, 23, 42), [System.Drawing.Color]::FromArgb(30, 64, 175))
    $g.FillRectangle($grad, 0, 0, $W, $H)
    $grad.Dispose()

    $logo = Join-Path $script:root 'assets\logo.png'
    if (Test-Path $logo) {
        $img = [System.Drawing.Image]::FromFile($logo)
        # قصّ الشعار على حدود الرسم الفعلية (هو داخل مربّع شفاف) قبل الرسم
        $minX = $img.Width; $minY = $img.Height; $maxX = -1; $maxY = -1
        for ($y = 0; $y -lt $img.Height; $y++) {
            for ($x = 0; $x -lt $img.Width; $x++) {
                if ($img.GetPixel($x, $y).A -gt 8) {
                    if ($x -lt $minX) { $minX = $x }
                    if ($x -gt $maxX) { $maxX = $x }
                    if ($y -lt $minY) { $minY = $y }
                    if ($y -gt $maxY) { $maxY = $y }
                }
            }
        }
        $rect = [System.Drawing.Rectangle]::new($minX, $minY, ($maxX - $minX + 1), ($maxY - $minY + 1))
        $trimmed = [System.Drawing.Bitmap]::new($img.Width, $img.Height, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $gg = [System.Drawing.Graphics]::FromImage($trimmed)
        $gg.Clear([System.Drawing.Color]::Transparent)
        $gg.DrawImage($img, (New-Object System.Drawing.Rectangle(0, 0, $img.Width, $img.Height)), $rect, [System.Drawing.GraphicsUnit]::Pixel)
        $gg.Dispose()
        $img.Dispose()
        $g.DrawImage($trimmed, 54, 42, 96, 96)
        $trimmed.Dispose()
    }

    $fTitle = [System.Drawing.Font]::new('Segoe UI', 44, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
    $fSub = [System.Drawing.Font]::new('Segoe UI', 21, [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Pixel)
    $white = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::White)
    $soft = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(205, 219, 234, 254))

    $sf = [System.Drawing.StringFormat]::new()
    $sf.Alignment = [System.Drawing.StringAlignment]::Near
    $sf.LineAlignment = [System.Drawing.StringAlignment]::Center
    $g.DrawString('pdf2md', $fTitle, $white, [System.Drawing.RectangleF]::new(176, 40, 600, 62), $sf)
    $g.DrawString('حوّل ملفات PDF إلى Markdown — العربية أولاً', $fSub, $soft, [System.Drawing.RectangleF]::new(176, 104, 660, 34), $sf)
    $sf.Dispose()
    $white.Dispose()
    $soft.Dispose()

    $g.Dispose()
    $path = Join-Path $outDir $File
    $bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    Write-Host ("wrote: {0} ({1} bytes)" -f $path, (Get-Item $path).Length)
}

Draw-Banner -File 'download-banner.png'
Draw-Button -File 'download-latest.png' -Title 'تحميل pdf2md الآن' -Subtitle 'دائماً أحدث نسخة' -Top '#16A34A' -Bottom '#15803D' -W 460
Draw-Button -File 'download-windows.png' -Title 'تحميل لويندوز' -Subtitle 'مثبّت — ثبّته وشغّله' -Top '#2563EB' -Bottom '#1D4ED8' -W 420
Draw-Button -File 'download-macos-arm64.png' -Title 'تحميل لماك' -Subtitle 'Apple Silicon (M1–M4)' -Top '#7C3AED' -Bottom '#6D28D9' -W 460
Draw-Button -File 'download-macos-x64.png' -Title 'تحميل لماك' -Subtitle 'Intel — المعالجات القديمة' -Top '#0891B2' -Bottom '#0E7490' -W 460
Write-Host ''
Write-Host 'buttons ready.'
