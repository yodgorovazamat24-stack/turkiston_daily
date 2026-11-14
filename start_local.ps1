$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$envFile = Join-Path $projectRoot ".env"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$runtimeDirectory = Join-Path $projectRoot ".runtime"
$port = 8000

if (-not (Test-Path -LiteralPath $envFile)) {
    throw ".env fayli topilmadi."
}
if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual muhiti topilmadi. start_bot.bat orqali paketlarni o'rnating."
}

$configuration = Get-Content -LiteralPath $envFile
$tokenLine = $configuration | Where-Object { $_ -match '^\s*BOT_TOKEN\s*=' } | Select-Object -First 1
$token = if ($tokenLine) { ($tokenLine -split '=', 2)[1].Trim().Trim('"').Trim("'") } else { "" }
if (-not $token -or $token -eq "YOUR_BOT_TOKEN_HERE") {
    throw ".env faylida haqiqiy BOT_TOKEN sozlanmagan."
}

$portLine = $configuration | Where-Object { $_ -match '^\s*PORT\s*=\s*\d+' } | Select-Object -First 1
if ($portLine -and $portLine -match '=\s*(\d+)') {
    $port = [int]$Matches[1]
}

$cloudflared = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe"
if (-not (Test-Path -LiteralPath $cloudflared)) {
    $command = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
    if ($command) {
        $cloudflared = $command.Source
    } else {
        throw "cloudflared topilmadi. Cloudflare cloudflared dasturini o'rnating."
    }
}

$startupDirectory = [Environment]::GetFolderPath("Startup")
if (-not (Test-Path -LiteralPath $startupDirectory)) {
    throw "Windows Startup papkasi topilmadi."
}
$shortcutPath = Join-Path $startupDirectory "Turkiston Daily Bot.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $env:ComSpec
$shortcut.Arguments = '/c ""' + (Join-Path $projectRoot "start_bot.bat") + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.Description = "Windows kirishida Turkiston Daily botini va Mini App tunnelini ishga tushirish"
$shortcut.Save()

New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
$tunnelPidFile = Join-Path $runtimeDirectory "cloudflared.pid"
$botPidFile = Join-Path $runtimeDirectory "bot.pid"
$tunnelOutputLog = Join-Path $runtimeDirectory "cloudflared-output.log"
$tunnelErrorLog = Join-Path $runtimeDirectory "cloudflared-error.log"
$botLog = Join-Path $runtimeDirectory "bot.log"
$botErrorLog = Join-Path $runtimeDirectory "bot-error.log"

foreach ($pidFile in @($tunnelPidFile, $botPidFile)) {
    if (Test-Path -LiteralPath $pidFile) {
        $savedPid = 0
        if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$savedPid)) {
            $savedProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$savedPid" -ErrorAction SilentlyContinue
            if ($savedProcess) {
                $expectedExecutable = if ($pidFile -eq $tunnelPidFile) { $cloudflared } else { $python }
                if ($savedProcess.ExecutablePath -eq $expectedExecutable) {
                    Stop-Process -Id $savedPid -Force -ErrorAction SilentlyContinue
                }
            }
        }
        Remove-Item -LiteralPath $pidFile -Force
    }
}

$oldBotProcesses = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($projectRoot) -and $_.CommandLine -match '\bmain\.py\b' }
foreach ($process in $oldBotProcesses) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2

$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    throw "$port port band. Uni bo'shatib, botni qayta ishga tushiring."
}

Remove-Item -LiteralPath $tunnelOutputLog, $tunnelErrorLog, $botLog, $botErrorLog -Force -ErrorAction SilentlyContinue
$tunnel = Start-Process -FilePath $cloudflared `
    -ArgumentList @("tunnel", "--url", "http://127.0.0.1:$port", "--no-autoupdate") `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $tunnelOutputLog `
    -RedirectStandardError $tunnelErrorLog `
    -WindowStyle Hidden `
    -PassThru
$tunnel.Id | Set-Content -LiteralPath $tunnelPidFile

$publicUrl = $null
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    $tunnel.Refresh()
    if ($tunnel.HasExited) {
        $details = (Get-Content -LiteralPath $tunnelOutputLog -Raw -ErrorAction SilentlyContinue) +
            (Get-Content -LiteralPath $tunnelErrorLog -Raw -ErrorAction SilentlyContinue)
        throw "HTTPS tunnel ishga tushmadi. cloudflared.log faylini tekshiring. $details"
    }
    $details = (Get-Content -LiteralPath $tunnelOutputLog -Raw -ErrorAction SilentlyContinue) +
        (Get-Content -LiteralPath $tunnelErrorLog -Raw -ErrorAction SilentlyContinue)
    $match = [regex]::Match($details, 'https://[a-z0-9-]+\.trycloudflare\.com')
    if ($match.Success) {
        $publicUrl = $match.Value
        break
    }
    Start-Sleep -Seconds 1
}

if (-not $publicUrl) {
    throw "Tunnel manzili 60 soniyada olinmadi. .runtime\cloudflared-error.log faylini tekshiring."
}

$previousBotToken = [Environment]::GetEnvironmentVariable("BOT_TOKEN", "Process")
$previousWebAppUrl = [Environment]::GetEnvironmentVariable("WEBAPP_URL", "Process")
$env:BOT_TOKEN = $token
$env:WEBAPP_URL = $publicUrl
try {
    $bot = Start-Process -FilePath $python `
        -ArgumentList "main.py" `
        -WorkingDirectory $projectRoot `
        -RedirectStandardOutput $botLog `
        -RedirectStandardError $botErrorLog `
        -WindowStyle Hidden `
        -PassThru
} finally {
    if ($null -eq $previousBotToken) {
        Remove-Item Env:BOT_TOKEN -ErrorAction SilentlyContinue
    } else {
        $env:BOT_TOKEN = $previousBotToken
    }
    if ($null -eq $previousWebAppUrl) {
        Remove-Item Env:WEBAPP_URL -ErrorAction SilentlyContinue
    } else {
        $env:WEBAPP_URL = $previousWebAppUrl
    }
}
$bot.Id | Set-Content -LiteralPath $botPidFile
$publicUrl | Set-Content -LiteralPath (Join-Path $runtimeDirectory "miniap-url.txt")

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 1
    $bot.Refresh()
    if ($bot.HasExited) {
        $details = Get-Content -LiteralPath $botErrorLog -Raw -ErrorAction SilentlyContinue
        throw "Bot ishga tushmadi. .runtime\bot-error.log faylini tekshiring. $details"
    }
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        $ready = $true
        break
    }
}

if (-not $ready) {
    throw "Bot $port portda javob bermadi. .runtime\bot-error.log faylini tekshiring."
}

Write-Output "Bot va Mini App ishga tushdi."
Write-Output "Telegram Mini App manzili: $publicUrl"
Write-Output "Telegramda yangi Mini App tugmasini olish uchun /start yuboring."
