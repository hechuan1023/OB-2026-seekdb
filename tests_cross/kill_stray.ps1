$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'"
foreach ($p in $procs) {
    if ($null -ne $p.CommandLine -and $p.CommandLine.Contains('minivectordb')) {
        Write-Output ("KILL " + $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force
    }
}
