param([string]$CF, [string]$Python, [string]$Backend = "http://127.0.0.1:8099")

Write-Host "  CF Watchdog avviato — riavvio automatico tunnel" -ForegroundColor Cyan

while ($true) {
    $logFile = [System.IO.Path]::GetTempFileName()
    Write-Host "  Avvio cloudflared..." -ForegroundColor Yellow

    $proc = Start-Process -FilePath $CF `
        -ArgumentList "tunnel --url http://localhost:8099" `
        -RedirectStandardError $logFile `
        -NoNewWindow -PassThru

    # Aspetta URL (max 40s)
    $waited = 0; $url = $null
    while ($waited -lt 40 -and -not $url -and -not $proc.HasExited) {
        Start-Sleep 3; $waited += 3
        $lines = Get-Content $logFile -ErrorAction SilentlyContinue
        foreach ($line in $lines) {
            $m = [regex]::Match($line, 'https://[a-z0-9\-]+\.trycloudflare\.com')
            if ($m.Success) { $url = $m.Value; break }
        }
    }

    if ($url) {
        try {
            $body = "{`"url`":`"$url`"}"
            Invoke-RestMethod -Uri "$Backend/api/tunnel" -Method POST `
                -Body $body -ContentType "application/json" | Out-Null
            Write-Host "  Tunnel attivo: $url" -ForegroundColor Green
        } catch { Write-Host "  Avviso: impossibile registrare URL al backend" -ForegroundColor Yellow }
    } else {
        Write-Host "  Tunnel: URL non trovato entro 40s" -ForegroundColor Red
    }

    # Aspetta che il processo muoia
    $proc.WaitForExit()
    Remove-Item $logFile -ErrorAction SilentlyContinue
    Write-Host "  Tunnel caduto. Riavvio tra 3s..." -ForegroundColor Yellow
    Start-Sleep 3
}
