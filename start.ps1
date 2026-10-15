param(
    [string]$Python = '',
    [string]$Config = 'config.example.json',
    [int]$Port = 8765
)

# 只选择已有解释器，不修改执行策略或安装任何依赖。
function Resolve-ProjectPython {
    param([string]$Requested)
    $candidates = @()
    if ($Requested) { $candidates += $Requested }
    elseif ($env:RAG_PYTHON) { $candidates += $env:RAG_PYTHON }
    else {
        $candidates += (Join-Path $PSScriptRoot '.venv/Scripts/python.exe')
        foreach ($name in @('python', 'python3')) {
            $command = Get-Command $name -ErrorAction SilentlyContinue
            if ($command) { $candidates += $command.Source }
        }
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $reportText = & $command.Source -B -m zhiwiki doctor 2>$null
        if ($LASTEXITCODE -ne 0) { continue }
        try { $report = ($reportText -join [Environment]::NewLine) | ConvertFrom-Json }
        catch { continue }
        if ($report.server_dependencies_ready) { return $command.Source }
    }
    throw 'No ready interpreter found. Run python -m zhiwiki doctor, then pass -Python with an existing configured interpreter. Nothing was installed.'
}
Push-Location -LiteralPath $PSScriptRoot
try {
    $selectedPython = Resolve-ProjectPython -Requested $Python
    Write-Host "Using existing Python: $selectedPython"
    & $selectedPython -B -m zhiwiki --config $Config serve --port $Port
    if ($LASTEXITCODE -ne 0) { throw "Server exited with code $LASTEXITCODE" }
}
finally {
    Pop-Location
}
