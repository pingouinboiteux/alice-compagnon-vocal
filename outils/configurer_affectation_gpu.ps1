param(
    [switch]$AuditOnly,
    [switch]$Silent,
    [switch]$InstallScheduledTask
)

$ErrorActionPreference = "Stop"

$TargetAdapter = "1002&744C&471E1DA2"
$TargetSubsys = "SUBSYS_471E1DA2"
$AliceSubsys = "SUBSYS_240E1458"
$RegistryPath = "HKCU:\Software\Microsoft\DirectX\UserGpuPreferences"
$Root = Split-Path -Parent $PSScriptRoot
$BackupRoot = Join-Path $Root "sauvegardes"
$LogRoot = Join-Path $Root "logs"
$ReportPath = Join-Path $LogRoot "affectation_gpu.json"
$TaskName = "Alice - Sapphire pour Windows et les jeux"
$ScriptPath = $PSCommandPath

function Write-Status {
    param([string]$Message)
    if (-not $Silent) {
        Write-Host $Message
    }
}

function Get-MergedTokens {
    param(
        [string]$Current,
        [string[]]$RemovePrefixes,
        [string[]]$AppendTokens
    )

    $tokens = @()
    foreach ($token in @($Current -split ";")) {
        if ([string]::IsNullOrWhiteSpace($token)) {
            continue
        }
        $remove = $false
        foreach ($prefix in $RemovePrefixes) {
            if ($token.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                $remove = $true
                break
            }
        }
        if (-not $remove) {
            $tokens += $token
        }
    }
    $tokens += $AppendTokens
    return (($tokens -join ";") + ";")
}

function Add-Executable {
    param(
        [System.Collections.Generic.HashSet[string]]$Set,
        [string]$Path,
        [switch]$AllowMissing
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }
    if (-not [IO.Path]::IsPathRooted($Path)) {
        return
    }
    if (-not $Path.EndsWith(".exe", [StringComparison]::OrdinalIgnoreCase)) {
        return
    }
    if ($Path.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) {
        return
    }
    if ($Path -match "(?i)\\(llama-(server|cli)|memtest_vulkan|pythonw?)\.exe$") {
        return
    }
    if (-not $AllowMissing -and -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    [void]$Set.Add($Path)
}

function Get-ExecutablesToPin {
    $set = [System.Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )

    $staticPaths = @(
        "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe",
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "$env:ProgramFiles\Mozilla Firefox\firefox.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "${env:ProgramFiles(x86)}\Steam\steam.exe",
        "$env:LOCALAPPDATA\Discord\Update.exe",
        "$env:LOCALAPPDATA\Riot Games\Riot Client\RiotClientServices.exe"
    )
    foreach ($path in $staticPaths) {
        Add-Executable -Set $set -Path $path
    }

    $dynamicRoots = @(
        "$env:LOCALAPPDATA\Discord",
        "${env:ProgramFiles(x86)}\Microsoft\EdgeWebView\Application"
    )
    foreach ($dynamicRoot in $dynamicRoots) {
        if (-not (Test-Path -LiteralPath $dynamicRoot -PathType Container)) {
            continue
        }
        Get-ChildItem -LiteralPath $dynamicRoot -Filter "*.exe" -File -Recurse `
            -ErrorAction SilentlyContinue |
            ForEach-Object {
                Add-Executable -Set $set -Path $_.FullName
            }
    }

    $steamRoot = "${env:ProgramFiles(x86)}\Steam\steamapps\common"
    if (Test-Path -LiteralPath $steamRoot -PathType Container) {
        Get-ChildItem -LiteralPath $steamRoot -Filter "*.exe" -File -Recurse `
            -ErrorAction SilentlyContinue |
            Where-Object {
                $_.FullName -notmatch (
                    "(?i)\\(vcredist|redist|directx|easyanticheat|battleye)\\" +
                    "|crashreport|unitycrashhandler|unins|setup|installer"
                )
            } |
            ForEach-Object {
                Add-Executable -Set $set -Path $_.FullName
            }
    }

    try {
        $codex = Get-AppxPackage -Name "OpenAI.Codex" -ErrorAction Stop |
            Select-Object -First 1
        if ($null -ne $codex) {
            Add-Executable -Set $set -Path (
                Join-Path $codex.InstallLocation "app\ChatGPT.exe"
            )
        }
    }
    catch {
        Write-Status "Codex n'est pas inventorie par AppX; aucun blocage."
    }

    Get-Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProcessName -match (
                "(?i)^(brave|chrome|firefox|msedge|msedgewebview2|discord|" +
                "steam|steamwebhelper|chatgpt)$"
            )
        } |
        ForEach-Object {
            try {
                Add-Executable -Set $set -Path $_.Path
            }
            catch {
                # Some protected processes do not expose their path.
            }
        }

    if (Test-Path -LiteralPath $RegistryPath) {
        $registryItem = Get-Item -LiteralPath $RegistryPath
        foreach ($name in $registryItem.GetValueNames()) {
            if ($name.EndsWith(".exe", [StringComparison]::OrdinalIgnoreCase)) {
                Add-Executable -Set $set -Path $name -AllowMissing
            }
        }
    }

    return @($set | Sort-Object)
}

function Assert-HardwareLayout {
    $adapters = @(Get-CimInstance Win32_VideoController)
    $sapphire = @($adapters | Where-Object { $_.PNPDeviceID -match $TargetSubsys })
    $alice = @($adapters | Where-Object { $_.PNPDeviceID -match $AliceSubsys })

    if ($sapphire.Count -ne 1) {
        throw "La Sapphire attendue n'est pas identifiable de facon unique."
    }
    if ($alice.Count -ne 1) {
        throw "La Gigabyte reservee a Alice n'est pas identifiable de facon unique."
    }
    if ([string]::IsNullOrWhiteSpace($sapphire[0].VideoModeDescription)) {
        throw "La Sapphire ne porte actuellement aucun ecran; aucun reglage applique."
    }
    return [pscustomobject]@{
        sapphire = $sapphire[0].PNPDeviceID
        alice = $alice[0].PNPDeviceID
        resolution = $sapphire[0].VideoModeDescription
    }
}

function New-RegistryBackup {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $directory = Join-Path $BackupRoot "preferences_gpu_$stamp"
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $file = Join-Path $directory "UserGpuPreferences_avant.reg"
    $result = & reg.exe export `
        "HKCU\Software\Microsoft\DirectX\UserGpuPreferences" `
        $file /y 2>&1
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $file)) {
        throw "La sauvegarde des preferences GPU a echoue: $result"
    }
    return $file
}

function Install-RefreshTask {
    $arguments = (
        '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden ' +
        '-File "' + $ScriptPath + '" -Silent'
    )
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Description "Garde Windows, les jeux et Discord sur la Sapphire." `
        -Force | Out-Null
}

$hardware = Assert-HardwareLayout
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $RegistryPath)) {
    New-Item -Path $RegistryPath -Force | Out-Null
}

$registryItem = Get-Item -LiteralPath $RegistryPath
$globalCurrent = [string]$registryItem.GetValue("DirectXUserGlobalSettings", "")
$globalWanted = Get-MergedTokens `
    -Current $globalCurrent `
    -RemovePrefixes @("HighPerfAdapter=") `
    -AppendTokens @("HighPerfAdapter=$TargetAdapter")

$applicationValue = (
    "SpecificAdapter=$TargetAdapter;GpuPreference=1073741824;"
)
$executables = @(Get-ExecutablesToPin)
$changes = [System.Collections.Generic.List[object]]::new()

if ($globalCurrent -ne $globalWanted) {
    $changes.Add([pscustomobject]@{
        type = "global"
        name = "DirectXUserGlobalSettings"
        value = $globalWanted
    })
}

foreach ($executable in $executables) {
    $current = [string]$registryItem.GetValue($executable, "")
    $wanted = Get-MergedTokens `
        -Current $current `
        -RemovePrefixes @("SpecificAdapter=", "GpuPreference=") `
        -AppendTokens @(
            "SpecificAdapter=$TargetAdapter",
            "GpuPreference=1073741824"
        )
    if ($current -ne $wanted) {
        $changes.Add([pscustomobject]@{
            type = "application"
            name = $executable
            value = $wanted
        })
    }
}

$backup = $null
if (-not $AuditOnly -and $changes.Count -gt 0) {
    $backup = New-RegistryBackup
    foreach ($change in $changes) {
        Set-ItemProperty -LiteralPath $RegistryPath -Name $change.name `
            -Value $change.value -Type String
    }
}

if ($InstallScheduledTask -and -not $AuditOnly) {
    Install-RefreshTask
}

$report = [pscustomobject]@{
    timestamp = (Get-Date).ToString("o")
    audit_only = [bool]$AuditOnly
    adapter_sapphire = $TargetAdapter
    adapter_alice = $hardware.alice
    resolution = $hardware.resolution
    applications_inventoriees = $executables.Count
    modifications = $changes.Count
    backup = $backup
    task_installed = [bool]($InstallScheduledTask -and -not $AuditOnly)
    note = (
        "Les applications deja ouvertes doivent etre relancees. " +
        "Un jeu qui choisit lui-meme son adaptateur peut ignorer Windows."
    )
}
$report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ReportPath `
    -Encoding UTF8

Write-Status (
    "Sapphire ciblee: {0} application(s), {1} modification(s)." -f `
        $executables.Count, $changes.Count
)
if ($null -ne $backup) {
    Write-Status "Sauvegarde: $backup"
}
if ($InstallScheduledTask -and -not $AuditOnly) {
    Write-Status "Rappel automatique installe a l'ouverture de session."
}
$report | ConvertTo-Json -Depth 4
