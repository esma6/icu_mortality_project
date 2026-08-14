param(
    [int]$Repeats = 12,
    [int]$Seed = 20260720
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$python = 'C:\Users\ETU\AppData\Local\Programs\Python\Python312\python.exe'
$out = Join-Path $root 'outputs\validation'
$logs = Join-Path $out 'logs'
$schedulePath = Join-Path $out 'randomization_schedule.csv'
$manifestPath = Join-Path $out 'validation_manifest.csv'
$matrixLog = Join-Path $logs 'validation_matrix_controller.log'
New-Item -ItemType Directory -Force -Path $logs | Out-Null

if (-not (Test-Path $schedulePath)) {
    & $python scripts/make_validation_schedule.py --output $schedulePath --repeats $Repeats --seed $Seed
    if ($LASTEXITCODE -ne 0) { throw 'Could not create randomization schedule' }
} else {
    $existing = Import-Csv $schedulePath
    $measuredBlocks = ($existing | Where-Object phase -eq 'measured' | Select-Object -ExpandProperty block -Unique).Count
    if ($measuredBlocks -ne $Repeats -or [int]$existing[0].seed -ne $Seed) {
        throw 'Existing schedule does not match requested repeats/seed'
    }
}

$rows = Import-Csv $schedulePath | Sort-Object {[int]$_.global_order}
$manifest = if (Test-Path $manifestPath) { @(Import-Csv $manifestPath) } else { @() }

foreach ($cell in $rows) {
    $prefix = if ($cell.phase -eq 'warmup') { 'w' } else { 'm' }
    $runId = "v2_${prefix}$('{0:d2}' -f [int]$cell.block)_$($cell.workload)_$($cell.scenario)"
    $alreadyDone = $manifest | Where-Object {$_.run_id -eq $runId -and $_.status -eq 'ok'}
    if ($alreadyDone) { continue }

    $stamp = Get-Date -Format o
    "[$stamp] START order=$($cell.global_order) run=$runId" | Tee-Object -FilePath $matrixLog -Append | Out-Host
    try {
        & (Join-Path $root 'scripts\run_validation_pilot.ps1') -Scenario $cell.scenario `
            -Workload $cell.workload -RunId $runId | Out-Host
        $resultPath = Join-Path $logs "result_${runId}.json"
        if (-not (Test-Path $resultPath)) { throw "Missing result JSON $resultPath" }
        $result = Get-Content $resultPath -Raw | ConvertFrom-Json
        $record = [pscustomobject]@{
            global_order=$cell.global_order; phase=$cell.phase; block=$cell.block;
            within_block_order=$cell.within_block_order; seed=$cell.seed;
            workload=$cell.workload; scenario=$cell.scenario; run_id=$runId; status='ok'; error='';
            cpu_budget=$result.cpu_budget; memory_budget_gib=$result.memory_budget_gib;
            input_format=$result.input_format; feature_rows=$result.feature_rows;
            extract_seconds=$result.extract_seconds; transform_seconds=$result.transform_seconds;
            load_seconds=$result.load_seconds; total_seconds=$result.total_seconds;
            event_log=$result.event_log; resource_log=$result.resource_log
        }
    } catch {
        $record = [pscustomobject]@{
            global_order=$cell.global_order; phase=$cell.phase; block=$cell.block;
            within_block_order=$cell.within_block_order; seed=$cell.seed;
            workload=$cell.workload; scenario=$cell.scenario; run_id=$runId; status='failed';
            error=$_.Exception.Message; cpu_budget=''; memory_budget_gib=''; input_format='';
            feature_rows=''; extract_seconds=''; transform_seconds=''; load_seconds='';
            total_seconds=''; event_log=''; resource_log=''
        }
        $manifest = @($manifest | Where-Object run_id -ne $runId) + @($record)
        $manifest | Export-Csv $manifestPath -NoTypeInformation -Encoding utf8
        "[$(Get-Date -Format o)] FAILED run=$runId error=$($record.error)" |
            Tee-Object -FilePath $matrixLog -Append | Out-Host
        throw
    }
    $manifest = @($manifest | Where-Object run_id -ne $runId) + @($record)
    $manifest | Sort-Object {[int]$_.global_order} | Export-Csv $manifestPath -NoTypeInformation -Encoding utf8
    "[$(Get-Date -Format o)] OK run=$runId total=$($record.total_seconds)" |
        Tee-Object -FilePath $matrixLog -Append | Out-Host
}

"[$(Get-Date -Format o)] MATRIX COMPLETE" | Tee-Object -FilePath $matrixLog -Append | Out-Host
