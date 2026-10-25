#Requires -Version 5.1
<#
.SYNOPSIS
Starts the existing KnowFlow development services on Windows.
.EXAMPLE
.\scripts\start-dev.ps1 -DepsOnly
.EXAMPLE
.\scripts\start-dev.ps1
.NOTES
Open Docker Desktop first. Never installs dependencies, pulls models, or runs
migrations. Full mode requires the database revision to match the code head.
Only 127.0.0.1 listeners are accepted. Existing app processes must be stopped
manually: HTTP routes and checkout paths cannot prove their loaded code version.
Close the separate application terminals (Ctrl+C) to stop those servers.
#>
[CmdletBinding()]
param([switch]$DepsOnly)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Frontend = Join-Path $Root 'frontend'
$Python = Join-Path $Root '.venv\Scripts\python.exe'

function Find-Command([string]$Name) {
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue
    if (-not $command) { throw "Required executable not found: $Name. Nothing will be installed automatically." }
    return $command.Source
}

function Get-Listener([int]$Port) {
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq $Port })
    # Examine every binding, including additional addresses owned by the same PID.
    $unexpected = @($listeners | Where-Object { $_.LocalAddress -ne '127.0.0.1' })
    if ($unexpected.Count -gt 0) {
        $addresses = ($unexpected.LocalAddress | Sort-Object -Unique) -join ', '
        throw "Port $Port listens on $addresses; expected only 127.0.0.1. Stop/reconfigure it manually; no process will be replaced."
    }
    $owners = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($owners.Count -gt 1) { throw "Port $Port has multiple listener processes; cannot safely identify the service." }
    if ($owners.Count -eq 0) { return $null }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($owners[0])" -ErrorAction Stop
    if (-not $process) { throw "Cannot identify the listener on port $Port. Retry after checking the process manually." }
    return $process
}

function Read-Http([string]$Uri) {
    try {
        $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 3 -MaximumRedirection 0
        if ($response.StatusCode -eq 200) { return $response }
    } catch { return $null }
    return $null
}

function Wait-Ready([string]$Name, [scriptblock]$Check, [int]$Seconds = 60) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        if (& $Check) { Write-Host "[OK] $Name"; return }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "$Name was not ready within $Seconds seconds. Check its container/application logs."
}

function Get-Container([string]$Name) {
    try { $raw = & $Docker inspect $Name 2>$null } catch { throw "Cannot inspect existing container $Name. No container will be created." }
    if ($LASTEXITCODE -ne 0) { throw "Existing container $Name not found. No container will be created." }
    return @($raw | ConvertFrom-Json)[0]
}

function Start-ExistingContainer([string]$Name, [string]$Service, [int]$Port) {
    $container = Get-Container $Name
    if ($container.Config.Labels.'com.docker.compose.project' -ne 'knowflow' -or
        $container.Config.Labels.'com.docker.compose.service' -ne $Service) {
        throw "Container $Name does not have the expected KnowFlow Compose labels."
    }
    $binding = @($container.HostConfig.PortBindings."$Port/tcp")
    if ($binding.Count -ne 1 -or $binding[0].HostIp -ne '127.0.0.1' -or $binding[0].HostPort -ne "$Port") {
        throw "Container $Name must publish 127.0.0.1:${Port}:${Port}; configuration will not be changed."
    }
    if ($container.State.Paused) { throw "Container $Name is paused. Resolve it manually before continuing." }
    if ($container.State.Running) {
        Write-Host "[SKIP] $Name is already running."
    } else {
        $result = & $Docker start $Name
        if ($LASTEXITCODE -ne 0) { throw "Failed to start $Name. Check Docker and port $Port; no container was recreated." }
        Write-Host "[START] $Name"
    }
}

function Test-Ollama {
    $response = Read-Http 'http://127.0.0.1:11434/api/version'
    if (-not $response) { return $false }
    try { return [bool](($response.Content | ConvertFrom-Json).version) } catch { return $false }
}

function Test-AppListener([int]$Port, [string]$Kind, [System.Diagnostics.Process]$Terminal = $null) {
    $process = Get-Listener $Port
    if (-not $process) { return $false }
    $line = $process.CommandLine
    if ($Kind -eq 'FastAPI') {
        $known = $process.Name -match '^python(w)?\.exe$' -and
            $line -match ([regex]::Escape($Root.TrimEnd('\')) + '\\') -and
            $line -match '\buvicorn\b' -and $line -match 'project\.api\.app:create_app'
    } else {
        $known = $process.Name -eq 'node.exe' -and
            $line -match [regex]::Escape((Join-Path $Frontend 'node_modules\next\'))
    }
    if (-not $known) {
        throw "Port $Port is owned by PID $($process.ProcessId), which cannot be verified as this checkout's $Kind. Resolve it manually."
    }
    if (-not $Terminal -or $Terminal.HasExited) {
        throw "Port $Port already has a $Kind process. Its loaded code version cannot be verified. Stop it manually, then rerun this script to start current code."
    }
    # Only descendants of the terminal created during THIS invocation are accepted.
    $ancestor = $process
    for ($depth = 0; $depth -lt 12 -and $ancestor; $depth++) {
        if ($ancestor.ParentProcessId -eq $Terminal.Id) { return $true }
        $ancestor = Get-CimInstance Win32_Process -Filter "ProcessId = $($ancestor.ParentProcessId)" -ErrorAction Stop
    }
    throw "Port $Port is not owned by the $Kind terminal started by this invocation. Stop it manually; its loaded code version cannot be verified."
}

function Get-AuthContract {
    # Parse declarations only; do not import/start the app or use a fixed refresh flag.
    $authContractCheck = @'
import ast, json
from pathlib import Path
tree = ast.parse(Path('project/api/auth_routes.py').read_text(encoding='utf-8-sig'))
routers = [n.value for n in tree.body if isinstance(n, ast.Assign)
           and any(isinstance(t, ast.Name) and t.id == 'router' for t in n.targets)]
assert len(routers) == 1 and isinstance(routers[0], ast.Call), 'Unsupported auth router declaration'
prefix = next(ast.literal_eval(k.value) for k in routers[0].keywords if k.arg == 'prefix')
assert prefix == '/auth', 'Unsupported auth router prefix'
methods = {'get', 'post', 'put', 'patch', 'delete', 'options', 'head'}
routes = set()
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for d in node.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and isinstance(d.func.value, ast.Name) and d.func.value.id == 'router'
                    and d.func.attr in methods):
                path = ast.literal_eval(d.args[0])
                assert isinstance(path, str) and path.startswith('/'), 'Unsupported auth route'
                routes.add(d.func.attr.upper() + ' ' + prefix + path)
assert {'POST /auth/login', 'POST /auth/register', 'GET /auth/me'} <= routes, 'Missing base auth routes'
print(json.dumps(sorted(routes)))
'@
    $output = $authContractCheck | & $Python -X utf8 -
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the current auth route contract. Application startup stopped.' }
    $contract = $output | ConvertFrom-Json
    return $contract
}

function Test-Api([string[]]$AuthContract) {
    $health = Read-Http 'http://127.0.0.1:8000/health'
    $schema = Read-Http 'http://127.0.0.1:8000/openapi.json'
    if (-not $health -or -not $schema) { return $false }
    try {
        $status = ($health.Content | ConvertFrom-Json).status
        $paths = ($schema.Content | ConvertFrom-Json).paths
    } catch { return $false }
    if ($status -ne 'ok' -or $paths.PSObject.Properties.Name -notcontains '/knowledge-bases') { return $false }
    $actual = @(foreach ($path in $paths.PSObject.Properties) {
        if ($path.Name.StartsWith('/auth/')) {
            foreach ($method in @('get', 'post', 'put', 'patch', 'delete', 'options', 'head')) {
                if ($path.Value.PSObject.Properties.Name -contains $method) { $method.ToUpperInvariant() + ' ' + $path.Name }
            }
        }
    })
    if (-not $AuthContract -or -not $actual -or @(Compare-Object -ReferenceObject $AuthContract -DifferenceObject $actual).Count -ne 0) {
        throw 'API auth routes differ from current source. Stop the API manually and rerun this script; its loaded code cannot be accepted.'
    }
    return $true
}

function Test-Web {
    $response = Read-Http 'http://127.0.0.1:3000/login'
    return $response -and $response.Content -match '<title>KnowFlow'
}

function Quote-PS([string]$Value) { return "'" + $Value.Replace("'", "''") + "'" }

function Start-LogTerminal([string]$Title, [string]$Directory, [string]$Command) {
    # EncodedCommand avoids nested quoting problems, including paths with spaces.
    $body = @"
`$Host.UI.RawUI.WindowTitle = $(Quote-PS $Title)
Set-Location -LiteralPath $(Quote-PS $Directory)
$Command
Write-Host ("Server exited (code {0}). See the log above." -f `$LASTEXITCODE)
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($body))
    $shell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $terminal = Start-Process -FilePath $shell -ArgumentList @('-NoProfile', '-NoExit', '-EncodedCommand', $encoded) -WindowStyle Normal -PassThru
    Write-Host "[START] $Title in a separate terminal."
    return $terminal
}

Push-Location -LiteralPath $Root
try {
    $Docker = Find-Command 'docker.exe'
    try {
        $version = & $Docker info --format '{{.ServerVersion}}' 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $version) { throw 'Engine unavailable' }
    } catch { throw 'Docker Engine is not running. Open Docker Desktop manually, wait until it is ready, then retry.' }

    Start-ExistingContainer 'knowflow-postgres-1' 'postgres' 5432
    Start-ExistingContainer 'knowflow-qdrant-1' 'qdrant' 6333
    Wait-Ready 'PostgreSQL healthy' { (Get-Container 'knowflow-postgres-1').State.Health.Status -eq 'healthy' }
    Wait-Ready 'Qdrant HTTP 200 and identity' {
        $health = Read-Http 'http://127.0.0.1:6333/healthz'
        $info = Read-Http 'http://127.0.0.1:6333/'
        if (-not $health -or -not $info) { return $false }
        try { return ($info.Content | ConvertFrom-Json).title -eq 'qdrant - vector search engine' } catch { return $false }
    }

    $Ollama = Find-Command 'ollama.exe'
    $listener = Get-Listener 11434
    if ($listener) {
        if ($listener.ExecutablePath -ine $Ollama) { throw 'Port 11434 belongs to an unverified process; Ollama will not be started.' }
        Write-Host '[SKIP] Ollama is already listening.'
    } else {
        Start-Process -FilePath $Ollama -ArgumentList 'serve' -WindowStyle Hidden | Out-Null
        Write-Host '[START] Local Ollama service (no model download).'
    }
    Wait-Ready 'Ollama API' { Test-Ollama }
    if ((Get-Listener 11434).ExecutablePath -ine $Ollama) { throw 'Ollama listener identity changed; stopping startup.' }
    $tags = Read-Http 'http://127.0.0.1:11434/api/tags'
    if (-not $tags) { throw 'Cannot list local Ollama models.' }
    $models = @(($tags.Content | ConvertFrom-Json).models | ForEach-Object { $_.name })
    if ($models -cnotcontains 'granite4.1:8b') { throw 'Required model granite4.1:8b is missing. No model will be downloaded; prepare it manually.' }
    Write-Host '[OK] Local model granite4.1:8b'

    if ($DepsOnly) {
        Write-Host '[OK] Dependencies ready. Application servers were not started.' -ForegroundColor Green
        exit 0
    }

    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Project Python not found: $Python" }
    # Read-only transaction: inspect version metadata without running env.py or DDL.
    $revisionCheck = @'
import sys
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from project.db.business_database import get_database_url

engine = None
url = None
try:
    expected = tuple(ScriptDirectory.from_config(Config('alembic.ini')).get_heads())
    if len(expected) != 1:
        raise RuntimeError('Expected exactly one Alembic code head')
    url = get_database_url()
    engine = create_engine(url, hide_parameters=True, connect_args={
        'connect_timeout': 5,
        'options': '-c default_transaction_read_only=on -c statement_timeout=5000',
    })
    with engine.connect() as connection:
        assert connection.scalar(text('SHOW transaction_read_only')) == 'on'
        current = tuple(MigrationContext.configure(connection).get_current_heads())
    print('Database revision: ' + (','.join(current) or '(unversioned)'))
    print('Code Alembic head: ' + ','.join(expected))
    if set(current) != set(expected):
        print('Migration mismatch. Review/apply the migration separately before starting apps.')
        sys.exit(2)
except Exception as error:
    detail = str(getattr(error, 'orig', error))
    if url is not None and url.password:
        detail = detail.replace(url.password, '[redacted]')
    print('Database version check failed (' + type(error).__name__ + '): ' + detail)
    sys.exit(1)
finally:
    if engine is not None:
        engine.dispose()
'@
    $revisionOutput = $revisionCheck | & $Python -X utf8 -
    $revisionExit = $LASTEXITCODE
    $revisionOutput | ForEach-Object { Write-Host $_ }
    if ($revisionExit -ne 0) { throw 'Database version check did not pass. FastAPI and Next.js were not started. No migration was run.' }
    Write-Host '[OK] Database revision matches code head.'

    $authContract = @(Get-AuthContract)
    $Node = Find-Command 'node.exe'
    $Next = Join-Path $Frontend 'node_modules\next\dist\bin\next'
    if (-not (Test-Path -LiteralPath $Next -PathType Leaf)) { throw 'Local Next.js dependency is missing. Nothing will be installed automatically.' }
    if (-not (Test-Path -LiteralPath (Join-Path $Frontend '.env.local') -PathType Leaf)) { throw 'frontend/.env.local is missing. Configure the BFF before starting it.' }
    foreach ($dependency in @('typescript\lib\typescript.js', '@types\react\index.d.ts', '@types\node\index.d.ts')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Frontend "node_modules\$dependency") -PathType Leaf)) {
            throw "Local dependency $dependency is missing. Nothing will be installed automatically."
        }
    }
    # Fail before Next can attempt its native-binding download fallback.
    $architecture = & $Node -p 'process.arch'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot determine the local Node.js architecture.' }
    $native = Join-Path $Frontend "node_modules\@next\swc-win32-$architecture-msvc\next-swc.win32-$architecture-msvc.node"
    if (-not (Test-Path -LiteralPath $native -PathType Leaf)) { throw 'Local Next.js native binding is missing. Nothing will be downloaded.' }
    & $Node -e 'require(process.argv[1])' $native
    if ($LASTEXITCODE -ne 0) { throw 'Local Next.js native binding cannot load. Nothing will be downloaded.' }
    # A same-checkout process may still have old code in memory. Do not reuse it.
    $null = Test-AppListener 8000 'FastAPI'
    $null = Test-AppListener 3000 'Next.js'
    $apiTerminal = Start-LogTerminal 'KnowFlow FastAPI :8000' $Root "& $(Quote-PS $Python) -m uvicorn project.api.app:create_app --factory --app-dir $(Quote-PS $Root) --host 127.0.0.1 --port 8000"
    Wait-Ready 'KnowFlow FastAPI process, HTTP and current routes' {
        (Test-AppListener 8000 'FastAPI' $apiTerminal) -and (Test-Api $authContract)
    } 120
    # CI disables Next's automatic installation of missing TypeScript packages.
    $webTerminal = Start-LogTerminal 'KnowFlow Next.js :3000' $Frontend "`$env:CI = '1'`n& $(Quote-PS $Node) $(Quote-PS $Next) dev --hostname 127.0.0.1 --port 3000"
    Wait-Ready 'KnowFlow Next.js process and login HTTP 200' {
        (Test-AppListener 3000 'Next.js' $webTerminal) -and (Test-Web)
    } 120
    Write-Host '[OK] KnowFlow ready: http://localhost:3000' -ForegroundColor Green
} catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    Pop-Location
}
