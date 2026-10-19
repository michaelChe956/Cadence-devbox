$netshCmd = "netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=13306 connectaddress=10.129.12.75 connectport=3306"
Write-Host "PROBE-C-OK len=$($netshCmd.Length)"
