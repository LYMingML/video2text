param(
    [string]$EnvFile = "",
    [int]$Port = 0
)

$ErrorActionPreference = 'Stop'

function Get-RepoRoot {
    if ($PSScriptRoot) {
        return $PSScriptRoot
    }
    return (Get-Location).Path
}

function Read-EnvMap([string]$Path) {
    $result = @{}
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) {
        return $result
    }

    foreach ($raw in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) {
            continue
        }
        $parts = $line.Split('=', 2)
        if ($parts.Count -ne 2) {
            continue
        }
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        if ($key) {
            $result[$key] = $value
        }
    }
    return $result
}

function Resolve-DebugPort([string]$EnvPath, [int]$RequestedPort) {
    if ($RequestedPort -gt 0) {
        return $RequestedPort
    }
    $envMap = Read-EnvMap $EnvPath
    if ($envMap.ContainsKey('BROWSER_DEBUG_PORT')) {
        $value = 0
        if ([int]::TryParse([string]$envMap['BROWSER_DEBUG_PORT'], [ref]$value) -and $value -gt 0) {
            return $value
        }
    }
    return 9222
}

function Get-BrowserConfig {
    return @(
        [pscustomobject]@{
            Name = 'chrome'
            DisplayName = 'Google Chrome'
            ProcessNames = @('chrome')
            ExeCandidates = @(
                "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
                "$env:ProgramFiles(x86)\Google\Chrome\Application\chrome.exe",
                "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
            )
        },
        [pscustomobject]@{
            Name = 'edge'
            DisplayName = 'Microsoft Edge'
            ProcessNames = @('msedge')
            ExeCandidates = @(
                "$env:ProgramFiles(x86)\Microsoft\Edge\Application\msedge.exe",
                "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
                "$env:LocalAppData\Microsoft\Edge\Application\msedge.exe"
            )
        }
    )
}

function Resolve-Executable($Config) {
    foreach ($candidate in $Config.ExeCandidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    return $null
}

function Get-ProcessesByNames([string[]]$Names) {
    $all = @()
    foreach ($name in $Names) {
        try {
            $items = Get-Process -Name $name -ErrorAction Stop
            if ($items) {
                $all += $items
            }
        } catch {
        }
    }
    return @($all)
}

function Get-RunningDurationSeconds($Processes) {
    if (-not $Processes -or $Processes.Count -eq 0) {
        return [double]::PositiveInfinity
    }
    $startTimes = @()
    foreach ($proc in $Processes) {
        try {
            $startTimes += $proc.StartTime
        } catch {
        }
    }
    if (-not $startTimes -or $startTimes.Count -eq 0) {
        return [double]::PositiveInfinity
    }
    $earliest = ($startTimes | Sort-Object | Select-Object -First 1)
    return [math]::Max(0, (New-TimeSpan -Start $earliest -End (Get-Date)).TotalSeconds)
}

function Select-Browser($Configs) {
    $browserStates = foreach ($config in $Configs) {
        $exePath = Resolve-Executable $config
        if (-not $exePath) {
            continue
        }
        $procs = Get-ProcessesByNames $config.ProcessNames
        [pscustomobject]@{
            Config = $config
            ExePath = $exePath
            Processes = $procs
            IsRunning = ($procs.Count -gt 0)
            DurationSeconds = Get-RunningDurationSeconds $procs
        }
    }

    if (-not $browserStates -or $browserStates.Count -eq 0) {
        throw '未找到可用的 Chrome 或 Edge 安装路径。'
    }

    $idle = @($browserStates | Where-Object { -not $_.IsRunning })
    if ($idle.Count -gt 0) {
        return ($idle | Sort-Object { if ($_.Config.Name -eq 'chrome') { 0 } else { 1 } } | Select-Object -First 1)
    }

    return ($browserStates | Sort-Object DurationSeconds | Select-Object -First 1)
}

function Stop-BrowserProcesses($Selection) {
    if (-not $Selection.IsRunning) {
        return
    }
    foreach ($proc in $Selection.Processes) {
        try {
            Stop-Process -Id $proc.Id -Force -ErrorAction Stop
        } catch {
        }
    }
    Start-Sleep -Seconds 2
}

function Start-DebugBrowser($Selection, [int]$DebugPort) {
    $args = @(
        "--remote-debugging-port=$DebugPort",
        '--restore-last-session'
    )
    Start-Process -FilePath $Selection.ExePath -ArgumentList $args | Out-Null
}

$repoRoot = Get-RepoRoot
if (-not $EnvFile) {
    $EnvFile = Join-Path $repoRoot '.env'
}

$debugPort = Resolve-DebugPort -EnvPath $EnvFile -RequestedPort $Port
$selection = Select-Browser (Get-BrowserConfig)

Write-Host "[INFO] 选择浏览器: $($selection.Config.DisplayName)"
Write-Host "[INFO] 调试端口: $debugPort"

if ($selection.IsRunning) {
    Write-Host "[INFO] 当前运行中，准备关闭并以调试模式重启..."
    Stop-BrowserProcesses $selection
} else {
    Write-Host "[INFO] 当前未运行，直接以调试模式启动..."
}

Start-DebugBrowser -Selection $selection -DebugPort $debugPort
Write-Host "[OK] 已启动 $($selection.Config.DisplayName) 调试模式，端口 $debugPort"