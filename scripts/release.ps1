param([string]$Action = 'help')
$releaseArguments = @($args)
$ErrorActionPreference = 'Stop'
$releaseRoot = Split-Path -Parent $PSScriptRoot
if (-not $env:CRACKRAG_STATE) { $env:CRACKRAG_STATE = Join-Path $releaseRoot '.release' }
if (-not [IO.Path]::IsPathRooted($env:CRACKRAG_STATE)) { $env:CRACKRAG_STATE = Join-Path (Get-Location) $env:CRACKRAG_STATE }
$env:CRACKRAG_STATE = [IO.Path]::GetFullPath($env:CRACKRAG_STATE)
if (-not $env:CRACKRAG_PROJECT) { $env:CRACKRAG_PROJECT = 'crackrag-release' }
if ($env:CRACKRAG_PROJECT -notmatch '^crackrag-release[a-zA-Z0-9_-]*$') { throw 'Use a dedicated crackrag-release* project name.' }
$env:CRACKRAG_HOST_UID = '10001'
New-Item -ItemType Directory -Force -Path $env:CRACKRAG_STATE | Out-Null
function Invoke-ReleaseCompose {
 $composeArgs = @('compose','--project-directory',(Join-Path $releaseRoot 'deploy'))
 $envPath = Join-Path $env:CRACKRAG_STATE 'compose.env'
 if (Test-Path -LiteralPath $envPath) { $composeArgs += @('--env-file',$envPath) }
 $composeArgs += @('-f',(Join-Path $releaseRoot 'deploy/compose.release.yaml'))
 & docker @composeArgs @args
 if ($LASTEXITCODE -ne 0) { throw "Docker Compose operation failed ($LASTEXITCODE)" }
}
function Invoke-ReleaseAdmin { Invoke-ReleaseCompose --profile tools run --rm --no-deps admin @args }
function Assert-ReleaseReady { if (-not (Test-Path -LiteralPath (Join-Path $env:CRACKRAG_STATE 'compose.env'))) { throw 'Run .\crackrag.ps1 init first.' } }
function Start-Release { Invoke-ReleaseAdmin verify; Invoke-ReleaseAdmin volume-init; Invoke-ReleaseCompose up -d --wait --wait-timeout 1200 postgres redis runtime api }
switch ($Action) {
 'init' {
  if($releaseArguments.Count -eq 0){Invoke-ReleaseCompose build admin}
  elseif($releaseArguments.Count -ne 1 -or $releaseArguments[0] -ne '--no-build'){throw 'Use init [--no-build]'}
  Invoke-ReleaseAdmin verify; Invoke-ReleaseAdmin init; Invoke-ReleaseAdmin volume-init
 }
 'build' { Invoke-ReleaseCompose build admin }
 'up' { Assert-ReleaseReady; Start-Release }
 'mock' { Assert-ReleaseReady; Invoke-ReleaseAdmin mock; Invoke-ReleaseCompose stop api runtime api2 runtime2; Start-Release }
 'opening-new' { Assert-ReleaseReady; Invoke-ReleaseAdmin opening-new @releaseArguments }
 { $_ -in 'models','model' } { Assert-ReleaseReady; Invoke-ReleaseAdmin models @releaseArguments }
 'recovery' {
  Assert-ReleaseReady; $operation=if($releaseArguments.Count){$releaseArguments[0]}else{'status'}
  switch($operation){
   'up' { Invoke-ReleaseAdmin verify; Invoke-ReleaseAdmin volume-init; Invoke-ReleaseCompose --profile recovery up -d --wait --wait-timeout 1200 postgres redis runtime2 api2 }
   'stop' { Invoke-ReleaseCompose --profile recovery stop api2 runtime2 }
   'status' { Invoke-ReleaseCompose --profile recovery ps }
   default { throw 'Use recovery up, stop or status' }
  }
 }
 'demo-recovery' {
  Assert-ReleaseReady
  try {
   Invoke-ReleaseAdmin mock --scenario pause_after_candidates
   Invoke-ReleaseCompose stop api runtime api2 runtime2
   Start-Release
   Invoke-ReleaseCompose --profile recovery up -d --wait --wait-timeout 1200 postgres redis runtime2 api2
   Invoke-ReleaseAdmin demo-start
   # Both targets are explicit services of this dedicated Compose project.
   Invoke-ReleaseCompose kill --signal SIGKILL api runtime
   Invoke-ReleaseCompose restart redis
   Invoke-ReleaseAdmin demo-finish
  } finally {
   Invoke-ReleaseAdmin mock
   Invoke-ReleaseCompose --profile recovery stop api2 runtime2
   Start-Release
  }
 }
 'price' { Assert-ReleaseReady; Invoke-ReleaseAdmin price }
 'live' {
  Assert-ReleaseReady
  if ($releaseArguments.Count -eq 0) { throw 'Use live prepare, enable or pause.' }
  $operation=$releaseArguments[0]; $remaining=@($releaseArguments | Select-Object -Skip 1)
  switch ($operation) {
   'prepare' { Invoke-ReleaseAdmin pause; Invoke-ReleaseCompose stop api runtime api2 runtime2; Invoke-ReleaseAdmin prepare @remaining }
   'enable' { Start-Release; Invoke-ReleaseAdmin enable }
   'pause' { Invoke-ReleaseAdmin pause }
   default { throw 'Use live prepare, enable or pause.' }
  }
 }
 'status' { Assert-ReleaseReady; Invoke-ReleaseCompose ps; Invoke-ReleaseCompose exec -T postgres psql -X -U crackrag -d crackrag -At -c "WITH local AS (  SELECT COALESCE(sum(amount_cny),0) AS known,         COALESCE(sum(reserved_upper_cny) FILTER(WHERE state<>'SETTLED'),0) AS retained,         count(*) AS attempts FROM llm_calls WHERE provider='deepseek' ), opening AS (  SELECT COALESCE(max(known_cny),0) AS known,COALESCE(max(retained_cny),0) AS retained  FROM release_opening_balance ) SELECT jsonb_build_object('opening_known_cny',opening.known,'opening_retained_cny',opening.retained,  'local_known_cny',local.known,'local_unresolved_cny',local.retained,'local_paid_attempts',local.attempts,  'project_occupied_cny',opening.known+opening.retained+local.known+local.retained,  'project_remaining_cny',greatest(0,100-opening.known-opening.retained-local.known-local.retained)) FROM local CROSS JOIN opening; " }
 'logs' { Assert-ReleaseReady; $service=if($releaseArguments.Count){$releaseArguments[0]}else{'api'}; Invoke-ReleaseCompose logs --tail 100 $service }
 'stop' { Assert-ReleaseReady; Invoke-ReleaseAdmin pause; Invoke-ReleaseCompose stop api runtime api2 runtime2 redis postgres }
 'activate' {
  Assert-ReleaseReady; Invoke-ReleaseAdmin pause; Invoke-ReleaseCompose stop api runtime api2 runtime2; Invoke-ReleaseCompose up -d --wait postgres
  $activationName=(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffffffZ')
  $activationDirectory=Join-Path $env:CRACKRAG_STATE 'state/activations'
  New-Item -ItemType Directory -Force -Path $activationDirectory | Out-Null
  $sqlPath=Join-Path $activationDirectory "$activationName.sql"; $auditPath=Join-Path $activationDirectory "$activationName.jsonl"
  if((Test-Path -LiteralPath $sqlPath) -or (Test-Path -LiteralPath $auditPath)){throw 'Activation audit exists'}
  $sql=Invoke-ReleaseAdmin activation-sql
  [IO.File]::WriteAllLines($sqlPath,[string[]]$sql)
  # Pass stdin directly to docker; PowerShell functions do not implicitly relay it.
  $container=(Invoke-ReleaseCompose ps -q postgres).Trim()
  $audit=$sql | & docker exec -i $container psql -X -U crackrag -d crackrag -v ON_ERROR_STOP=1 -At
  [IO.File]::WriteAllLines($auditPath,[string[]]$audit)
  if($LASTEXITCODE -ne 0){throw 'Catalog activation refused; inspect private audit'}
  $audit; Write-Output 'Catalog activated; history retained. Services remain stopped and paid mode paused.'
 }
 'backup' {
  Assert-ReleaseReady; $backupName=if($releaseArguments.Count){$releaseArguments[0]}else{(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')}
  if($backupName -notmatch '^[A-Za-z0-9_-]+$'){throw 'Invalid backup name'}
  $backupDirectory=Join-Path $env:CRACKRAG_STATE "backups/$backupName"
  if(Test-Path -LiteralPath $backupDirectory){throw 'Backup exists'}
  Invoke-ReleaseAdmin pause; Invoke-ReleaseCompose stop api runtime api2 runtime2; Invoke-ReleaseCompose up -d --wait postgres
  New-Item -ItemType Directory -Path $backupDirectory | Out-Null
  Invoke-ReleaseCompose exec -T postgres pg_dump -U crackrag -d crackrag -Fc --no-owner --no-privileges -f /tmp/crackrag-backup.dump
  $container=(Invoke-ReleaseCompose ps -q postgres).Trim()
  & docker cp "${container}:/tmp/crackrag-backup.dump" (Join-Path $backupDirectory 'database.dump')
  if($LASTEXITCODE -ne 0){throw 'Database copy failed'}
  Invoke-ReleaseAdmin backup-files --name $backupName
  Write-Output 'Applications remain stopped. Use up while the session is valid, or mock when expired; paid mode remains paused.'
 }
 'restore' {
  Assert-ReleaseReady; $backupName=if($releaseArguments.Count){$releaseArguments[0]}else{''}
  if($backupName -notmatch '^[A-Za-z0-9_-]+$'){throw 'A backup name is required'}
  Invoke-ReleaseAdmin pause; Invoke-ReleaseCompose stop api runtime api2 runtime2; Invoke-ReleaseCompose up -d --wait postgres redis
  $count=(Invoke-ReleaseCompose exec -T postgres psql -X -U crackrag -d crackrag -At -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';").Trim()
  if($count -ne '0'){throw 'Restore requires a fresh empty database; existing data is never overwritten.'}
  Invoke-ReleaseAdmin volume-init; Invoke-ReleaseAdmin restore-files --name $backupName
  $container=(Invoke-ReleaseCompose ps -q postgres).Trim()
  & docker cp (Join-Path $env:CRACKRAG_STATE "backups/$backupName/database.dump") "${container}:/tmp/crackrag-restore.dump"
  if($LASTEXITCODE -ne 0){throw 'Database copy failed'}
  Invoke-ReleaseCompose exec -T postgres pg_restore -U crackrag -d crackrag --no-owner --no-privileges --exit-on-error /tmp/crackrag-restore.dump
  Write-Output 'Restored paused/mock. Start, verify facts and balances, then explicitly prepare a new live session.'
 }
 default { Write-Output 'Usage: .\crackrag.ps1 init | build | up | mock | opening-new --project-id ID | models [--offline] [--smoke] | price refresh | live prepare/enable/pause | recovery up/stop/status | demo-recovery | activate | status | logs | backup [name] | restore name | stop' }
}
