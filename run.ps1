$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = $env:PURCHASES_PYTHON
if (-not $taskPython) {
    $taskBundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $taskBundled) { $taskPython = $taskBundled }
    else {
        $taskCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($taskCommand) { $taskPython = $taskCommand.Source }
    }
}
if (-not $taskPython) { throw 'Нужен Python 3.12+. Укажите путь в PURCHASES_PYTHON.' }
Push-Location -LiteralPath $taskRoot
try { & $taskPython -X utf8 -m procurement @args; $taskExitCode = $LASTEXITCODE }
finally { Pop-Location }
exit $taskExitCode
