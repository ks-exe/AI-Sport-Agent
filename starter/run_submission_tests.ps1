$ErrorActionPreference = "Stop"

$env:UV_CACHE_DIR = Join-Path $PSScriptRoot ".uv-cache"
$LogPath = Join-Path $PSScriptRoot "submission-test-log.txt"
Set-Content -Path $LogPath -Value "AI Sport Agent submission test log`nGenerated: $(Get-Date -Format o)`n"

function Invoke-AgentTest {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Label,

        [Parameter(Mandatory = $true)]
        [hashtable] $Payload
    )

    $json = $Payload | ConvertTo-Json -Compress
    Add-Content -Path $LogPath -Value "`n===== $Label ====="
    Add-Content -Path $LogPath -Value "uv run agentcore invoke '$json'"
    uv run agentcore invoke $json 2>&1 | Tee-Object -FilePath $LogPath -Append
}

Invoke-AgentTest "Deployment smoke test" @{
    prompt = "Reply with a short confirmation that the agent is running."
    customer_id = "CUST-001"
    session_id = "submission-smoke"
}

Invoke-AgentTest "Gateway API target - order lookup" @{
    prompt = "Use the order lookup tool to tell me the status of order ORD-001."
    customer_id = "CUST-001"
    session_id = "submission-api-order"
}

Invoke-AgentTest "Gateway Lambda target - refund processing" @{
    prompt = "Use the refund tool to process a refund for ORD-002 because the item arrived damaged."
    customer_id = "CUST-001"
    session_id = "submission-lambda-refund"
}

Invoke-AgentTest "Knowledge Base RAG" @{
    prompt = "Using the knowledge base, what benefits do Platinum members receive?"
    customer_id = "CUST-001"
    session_id = "submission-kb"
}

Invoke-AgentTest "Memory session A - store preference" @{
    prompt = "My name is Maya and I prefer email updates. Please remember that."
    customer_id = "CUST-001"
    session_id = "submission-memory-a"
}

Start-Sleep -Seconds 35

Invoke-AgentTest "Memory session B - recall preference" @{
    prompt = "What is my name and preferred update channel?"
    customer_id = "CUST-001"
    session_id = "submission-memory-b"
}

Invoke-AgentTest "Code Interpreter - loyalty discount" @{
    prompt = "Calculate my loyalty discount as a Gold member with 4250 points on a 150 dollar order."
    customer_id = "CUST-001"
    session_id = "submission-discount"
}

Invoke-AgentTest "Browser tool - live page" @{
    prompt = "Use the browser tool to navigate to https://www.udacity.com and tell me the page title."
    customer_id = "CUST-001"
    session_id = "submission-browser"
}

Write-Host "Submission test log written to $LogPath"
