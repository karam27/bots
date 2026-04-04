$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $root "run_bot_forever.ps1"
$wrapperPidFile = Join-Path $root "bot_wrapper.pid"

if (Test-Path -LiteralPath $wrapperPidFile) {
    $rawPid = (Get-Content -LiteralPath $wrapperPidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($rawPid -match '^\d+$') {
        $proc = Get-Process -Id ([int]$rawPid) -ErrorAction SilentlyContinue
        if ($proc) {
            $proc | Select-Object Id, ProcessName, StartTime
            exit 0
        }
    }

    Remove-Item -LiteralPath $wrapperPidFile -Force -ErrorAction SilentlyContinue
}

Start-Process -FilePath "powershell.exe" `
    -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", $runner
    ) `
    -WorkingDirectory $root `
    -WindowStyle Hidden

Start-Sleep -Seconds 2

$rawPid = (Get-Content -LiteralPath $wrapperPidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
if ($rawPid -match '^\d+$') {
    Get-Process -Id ([int]$rawPid) -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, StartTime
}
