param(
    [ValidateSet("amd64", "arm64")]
    [string]$Architecture = "amd64"
)

$ErrorActionPreference = "Stop"
$moduleRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$goVersion = & go env GOVERSION
if ($LASTEXITCODE -ne 0 -or $goVersion -notmatch '^go1\.26(\.|$)') {
    throw "Go 1.26 is required; found '$goVersion'."
}

$previousCgo = $env:CGO_ENABLED
$previousGoos = $env:GOOS
$previousGoarch = $env:GOARCH
try {
    $env:CGO_ENABLED = "0"
    $env:GOOS = "windows"
    $env:GOARCH = $Architecture
    Push-Location $moduleRoot
    try {
        & go build -trimpath -ldflags="-s -w" -o web-mcp.exe ./cmd/web-mcp
        if ($LASTEXITCODE -ne 0) {
            throw "web-mcp build failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    $env:CGO_ENABLED = $previousCgo
    $env:GOOS = $previousGoos
    $env:GOARCH = $previousGoarch
}

Write-Host "Built $moduleRoot\web-mcp.exe ($Architecture, CGO_ENABLED=0)."
