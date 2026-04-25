$resp = Invoke-RestMethod -Uri http://localhost:5001/api/login -Method Post -Body '{"email":"admin@sevcs.com", "password":"admin"}' -ContentType "application/json"
$token = $resp.token
$status = Invoke-RestMethod -Uri http://localhost:5001/api/status -Method Get -Headers @{Authorization="Bearer $token"}
$status | ConvertTo-Json -Depth 10
