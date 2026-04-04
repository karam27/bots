$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv312\Scripts\python.exe"
$bot = Join-Path $root "bot.py"
$lockFile = Join-Path $root "bot.lock"
$wrapperPidFile = Join-Path $root "bot_wrapper.pid"
$outLog = Join-Path $root "bot.out.log"
$errLog = Join-Path $root "bot.err.log"

function Write-ManagerLog {
    param(
        [string]$Message,
        [string]$Path = $outLog
    )

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $Path -Value "[$timestamp] $Message"
}

function Clear-StaleLock {
    if (-not (Test-Path -LiteralPath $lockFile)) {
        return
    }

    $rawPid = (Get-Content -LiteralPath $lockFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    $isStale = $true

    if ($rawPid -match '^\d+$') {
        $proc = Get-Process -Id ([int]$rawPid) -ErrorAction SilentlyContinue
        if ($proc) {
            $isStale = $false
            Write-ManagerLog "bot.lock is active for PID $rawPid. Wrapper will not start a duplicate instance." $errLog
        }
    }

    if ($isStale) {
        Remove-Item -LiteralPath $lockFile -Force -ErrorAction SilentlyContinue
        Write-ManagerLog "Removed stale bot.lock before startup."
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python executable not found: $python"
}

if (-not (Test-Path -LiteralPath $bot)) {
    throw "Bot script not found: $bot"
}

Set-Location -LiteralPath $root
Set-Content -LiteralPath $wrapperPidFile -Value $PID
Write-ManagerLog "Bot wrapper started."

while ($true) {
    Clear-StaleLock

    if (Test-Path -LiteralPath $lockFile) {
        Start-Sleep -Seconds 15
        continue
    }

    Write-ManagerLog "Launching bot.py"
    & $python $bot 1>> $outLog 2>> $errLog
    $exitCode = $LASTEXITCODE

    Write-ManagerLog "bot.py exited with code $exitCode" $errLog
    Start-Sleep -Seconds 5
}
