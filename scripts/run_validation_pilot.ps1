param(
    [ValidateSet('local2','local4','local6','local8','standalone1','standalone2')]
    [string]$Scenario = 'local8',
    [ValidateSet('compact','timeseries')]
    [string]$Workload = 'compact',
    [string]$RunId = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$python = 'C:\Users\ETU\AppData\Local\Programs\Python\Python312\python.exe'
$out = Join-Path $root 'outputs\validation'
$logs = Join-Path $out 'logs'
$events = Join-Path $out 'spark-events'
New-Item -ItemType Directory -Force -Path $logs,$events | Out-Null

$workerNames = @()
if ($Scenario -in @('local2','local4','local6','local8')) {
    $threads = $Scenario.Substring(5)
    $master = "local[$threads]"; $workers = 0
    $masterCpu = '8'; $masterMem = '8g'
} elseif ($Scenario -eq 'standalone1') {
    $master = 'spark://spark-master:7077'; $workers = 1
    $masterCpu = '2'; $masterMem = '3g'
    $workerCpu = '6'; $workerMem = '5g'
    $env:SPARK_WORKER_CORES = '6'; $env:SPARK_WORKER_MEMORY = '4g'
    $workerNames = @('spark-worker-1')
} else {
    $master = 'spark://spark-master:7077'; $workers = 2
    $masterCpu = '2'; $masterMem = '3g'
    $workerCpu = '3'; $workerMem = '2560m'
    $env:SPARK_WORKER_CORES = '3'; $env:SPARK_WORKER_MEMORY = '2g'
    $workerNames = @('spark-worker-1','spark-worker-2')
}

docker compose stop spark-worker-1 spark-worker-2 spark-master | Out-Host
docker compose create spark-master | Out-Host
docker update --cpus $masterCpu --memory $masterMem --memory-swap $masterMem spark-master | Out-Host
docker compose start spark-master | Out-Host

if ($workers -ge 1) {
    docker compose create --force-recreate @workerNames | Out-Host
    foreach ($workerName in $workerNames) {
        docker update --cpus $workerCpu --memory $workerMem --memory-swap $workerMem $workerName | Out-Host
    }
    docker compose start @workerNames | Out-Host
}

$deadline = (Get-Date).AddSeconds(90)
do {
    Start-Sleep -Seconds 3
    try {
        $stateText = docker compose exec -T spark-master sh -lc 'wget -T 5 -qO- http://localhost:8080/json/'
        $state = $stateText | ConvertFrom-Json
    } catch { $state = $null }
} until (($null -ne $state -and $state.aliveworkers -eq $workers) -or (Get-Date) -gt $deadline)
if ($null -eq $state -or $state.aliveworkers -ne $workers) { throw 'Spark Master/worker readiness failed' }
if ($state.activeapps.Count -ne 0) { throw 'Spark cluster has an active application' }

$containers = @('spark-master') + $workerNames
$cpuTotal = 0.0; $memTotal = 0.0
foreach ($container in $containers) {
    $hostCfg = docker inspect $container --format '{{json .HostConfig}}' | ConvertFrom-Json
    $cpuTotal += $hostCfg.NanoCpus / 1e9
    $memTotal += $hostCfg.Memory / 1GB
}
if ([math]::Abs($cpuTotal - 8) -gt 0.01 -or [math]::Abs($memTotal - 8) -gt 0.05) {
    throw "Resource budget mismatch: CPU=$cpuTotal RAM=$memTotal"
}
if ($workers -ge 1 -and (($state.workers | Measure-Object cores -Sum).Sum -ne 6)) {
    throw 'Spark workers do not advertise six total cores'
}

$runId = if ($RunId) { $RunId } else { "pilot4_${Workload}_${Scenario}" }
$resource = Join-Path $logs "resource_usage_${runId}.csv"
$stopFile = Join-Path $logs "monitor_stop_${runId}.flag"
$submitLog = Join-Path $logs "spark_submit_${runId}.log"
Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
$monitor = Start-Process -FilePath $python -ArgumentList @(
    'scripts/monitor_resource_validation.py','--output',$resource,'--stop-file',$stopFile
) -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs "monitor_${runId}.out") `
  -RedirectStandardError (Join-Path $logs "monitor_${runId}.err")
Start-Sleep -Seconds 2
if ($monitor.HasExited -or -not (Test-Path $resource)) {
    $monitorError = Get-Content (Join-Path $logs "monitor_${runId}.err") -Raw -ErrorAction SilentlyContinue
    throw "Resource monitor failed to start: $monitorError"
}

$driverMem = '2g'
$args = @(
    'compose','exec','-T','-w','/app',
    '-e','MIMIC_DIR=/data/mimic','-e','PYTHONPATH=/app','-e','SPARK_DRIVER_HOST=spark-master',
    '-e',"ETL_FEATURE_SET_OVERRIDE=$Workload",'-e','ETL_OUTPUT_ROOT=/app/outputs/validation',
    '-e','SPARK_EXECUTOR_CORES_OVERRIDE=1','-e','SPARK_EXECUTOR_MEMORY_OVERRIDE=512m',
    '-e','SPARK_DRIVER_MEMORY_OVERRIDE=2g','spark-master','/opt/spark/bin/spark-submit',
    '--master',$master,'--driver-memory',$driverMem,'--executor-memory','512m','--executor-cores','1',
    '--conf','spark.driver.host=spark-master','--conf','spark.driver.bindAddress=0.0.0.0',
    '--conf','spark.eventLog.enabled=true','--conf','spark.eventLog.compress=false',
    '--conf','spark.eventLog.dir=file:///app/outputs/validation/spark-events','--conf','spark.cores.max=6',
    '/app/scripts/spark_etl_mimic.py','--config','/app/config.yaml','--master',$master,
    '--scenario',$Scenario,'--run-id',$runId,'--output-suffix',"_validation_${Workload}_${Scenario}"
)
try {
    $oldErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & docker @args 2>&1 | Out-File -FilePath $submitLog -Encoding utf8
    $sparkExitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldErrorPreference
    if ($sparkExitCode -ne 0) { throw "spark-submit failed with exit code $sparkExitCode" }
} finally {
    New-Item -ItemType File -Force -Path $stopFile | Out-Null
    Wait-Process -Id $monitor.Id -Timeout 20 -ErrorAction SilentlyContinue
}

$timingPath = Join-Path $logs "etl_timing_${Scenario}_${runId}.json"
if (-not (Test-Path $timingPath)) { throw "Missing timing JSON: $timingPath" }
$timing = Get-Content $timingPath -Raw | ConvertFrom-Json
$expected = if ($Workload -eq 'compact') { 58976 } else { 1180395 }
if ($timing.input_format -ne 'parquet' -or $timing.feature_rows -ne $expected) {
    throw "Output validation failed: input=$($timing.input_format), rows=$($timing.feature_rows)"
}
$event = Get-ChildItem $events | Where-Object {$_.Name -notlike '*.inprogress'} |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($null -eq $event -or $event.Length -eq 0) { throw 'Completed Spark event log not found' }

$result = [pscustomobject]@{
    run_id=$runId; scenario=$Scenario; workload=$Workload; cpu_budget=$cpuTotal;
    memory_budget_gib=[math]::Round($memTotal,3); input_format=$timing.input_format;
    feature_rows=$timing.feature_rows; extract_seconds=$timing.extract_seconds;
    transform_seconds=$timing.transform_seconds; load_seconds=$timing.load_seconds;
    total_seconds=$timing.total_seconds; event_log=$event.FullName; resource_log=$resource
}
$result | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $logs "result_${runId}.json")
$result | ConvertTo-Json -Depth 4
