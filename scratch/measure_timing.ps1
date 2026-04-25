$resp = Invoke-RestMethod -Uri http://localhost:5001/api/login -Method Post -Body '{"email":"admin@sevcs.com", "password":"admin"}' -ContentType "application/json"
$token = $resp.token
1..5 | ForEach-Object {
    $s = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
    $data = Invoke-RestMethod -Uri http://localhost:5001/api/status -Method Get -Headers @{Authorization="Bearer $token"}
    $e = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
    Write-Output "Snapshot $($data.snapshot_sequence) at $e (Latency: $($e - $s)ms)"
    Start-Sleep -Milliseconds 300
}
