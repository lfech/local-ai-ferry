# Open WebUI on your local network

> ⚠️ **Warning**\
> You are responsible for making this application reachable on your network!
> Local AI Ferry was engineered with heavy AI assistance.\
> The test suite covers launcher internals and deployment workflows.
> Source-code review is not planned.
> The documentation was mostly generated with AI.

[Verification coverage and results](verification.md#verification-coverage)

Create the first admin account locally and keep Open WebUI sign-in enabled.
The examples use HTTP and are intended for a trusted local network.

Edit the `config/config.ini` for your deployment. To apply changes, rerun the
serving command you normally use, `loaf run offline` or `loaf run online`, with your
usual flags such as `--cpu`. [Configuration](configuration.md#applying-changes)

## Native Debian

Find your Debian host's IPv4 address on its wired or Wi-Fi interface.

```bash
ip -4 -brief address show scope global
```

Under `[webui]`, use the host's LAN address and a free port. Replace the
example address with your own. A specific address limits the binding to
that address. Use `0.0.0.0` to bind to all IPv4 addresses on the host.

```ini
[webui]
bind_address = 192.168.1.20
port = 3000
auth = true
```

Apply the settings and open `http://192.168.1.20:3000` from another LAN device.
If you manage access with a host firewall, permit the published TCP port
from your LAN using rules that apply to Docker traffic.
[Docker port publishing](https://docs.docker.com/engine/network/port-publishing/) ·
[Docker firewall rules](https://docs.docker.com/engine/network/packet-filtering-firewalls/)

## Debian in Windows WSL 2

In Windows PowerShell, confirm the distribution name and find the Windows
IPv4 address and prefix length for the active Wi-Fi or Ethernet adapter.
The examples use the distribution name `Debian`, the Windows LAN address
`192.168.1.50`, and port `3000`.

```powershell
wsl.exe --list --verbose
Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred |
    Select-Object InterfaceAlias, IPAddress, PrefixLength
```

For either networking mode, set WebUI to listen on the WSL network interface.
Use the same port in Debian and in the PowerShell commands.

```ini
[webui]
bind_address = 0.0.0.0
port = 3000
auth = true
```

### NAT with portproxy

Apply the WebUI settings in Debian. In an elevated PowerShell session under
the Windows account that owns the Debian distribution, set your Windows LAN
address and find the IPv4 address of the WSL NAT interface, normally `eth0`.

```powershell
$Distro = 'Debian'
$WindowsIP = '192.168.1.50'
$WebUIPort = 3000
$Interface = wsl.exe --distribution $Distro --exec ip -4 -o address show dev eth0 scope global
$Match = [regex]::Match(($Interface -join ' '), '\binet\s+([0-9.]+)/')
if (-not $Match.Success) {
    throw "No IPv4 address found on eth0 in $Distro."
}
$WslIP = $Match.Groups[1].Value
```

In the same elevated session, forward the Windows TCP port to Debian.
The firewall rule applies to the Private profile and local subnets. Use
`Get-NetConnectionProfile` to check the profile of your trusted LAN.
[WSL NAT networking](https://learn.microsoft.com/en-us/windows/wsl/networking#default-networking-mode-nat) ·
[Windows Firewall rule reference](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule)

```powershell
netsh.exe interface portproxy add v4tov4 `
    "listenaddress=$WindowsIP" "listenport=$WebUIPort" `
    "connectaddress=$WslIP" "connectport=$WebUIPort"
if ($LASTEXITCODE -ne 0) {
    throw 'Could not create the port proxy.'
}
New-NetFirewallRule -Name 'LocalAIFerry-WebUI-NAT' `
    -DisplayName 'Local AI Ferry WebUI (WSL NAT)' `
    -Direction Inbound -Action Allow -Protocol TCP `
    -LocalAddress $WindowsIP -LocalPort $WebUIPort `
    -RemoteAddress LocalSubnet -Profile Private
```

Open `http://192.168.1.50:3000` from another LAN device. The WSL IPv4
address can change after WSL restarts. Rerun the address lookup above and
update the proxy from the same elevated PowerShell session.
[Windows portproxy reference](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/netsh-interface#portproxy)

```powershell
netsh.exe interface portproxy set v4tov4 `
    "listenaddress=$WindowsIP" "listenport=$WebUIPort" `
    "connectaddress=$WslIP" "connectport=$WebUIPort"
```

### Mirrored networking

Mirrored networking is available on Windows 11 22H2 or later. Use WSL
2.0.9 or later for the Hyper-V firewall setup below. If you previously
configured the NAT proxy, remove it before switching modes.
[Remove the WSL NAT proxy](#remove-the-wsl-nat-proxy) ·
[WSL mirrored networking](https://learn.microsoft.com/en-us/windows/wsl/networking#mirrored-mode-networking)

Open your Windows user's `.wslconfig` in PowerShell. Set `networkingMode=mirrored`
under `[wsl2]`, creating the section if needed.
This setting applies to all WSL 2 distributions.
[WSL configuration settings](https://learn.microsoft.com/en-us/windows/wsl/wsl-config#wslconfig)

```powershell
notepad.exe (Join-Path $env:USERPROFILE '.wslconfig')
```

```ini
[wsl2]
networkingMode=mirrored
```

Save the file, finish active WSL work, then restart WSL. The shutdown stops
all running WSL distributions. Reopen Debian and apply the WebUI settings.

```powershell
wsl.exe --shutdown
wsl.exe --distribution Debian
```

In elevated PowerShell, allow the WebUI TCP port from your LAN's subnet
through the Hyper-V firewall. Replace `192.168.1.0/24` with your LAN's
actual subnet. Open `http://192.168.1.50:3000` from another LAN device.
[Hyper-V firewall for WSL](https://learn.microsoft.com/en-us/windows/security/operating-system-security/network-security/windows-firewall/hyper-v-firewall) ·
[Hyper-V firewall rule reference](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallhypervrule)

```powershell
$WebUIPort = 3000
$LanSubnet = '192.168.1.0/24'
New-NetFirewallHyperVRule -Name 'LocalAIFerry-WebUI-Mirrored' `
    -DisplayName 'Local AI Ferry WebUI (WSL mirrored)' `
    -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' `
    -Direction Inbound -Action Allow -Protocol TCP `
    -LocalPorts $WebUIPort -RemoteAddresses $LanSubnet
```

## Remove local network publishing

Under `[webui]` in your deployment's `config/config.ini`, set
`bind_address = 127.0.0.1`. On native Debian, remove any firewall
exception you added for WebUI. For WSL, also follow the matching steps below.
Rerun your serving command after the changes. Open `http://localhost:3000`
locally, using your configured port.

On Docker Engine releases before 28.0.0, devices on the same network segment
may still reach a port published to loopback.
[Docker port publishing](https://docs.docker.com/engine/network/port-publishing/)

### Remove the WSL NAT proxy

In elevated PowerShell, use the Windows listen address and port you originally
registered. Run `netsh.exe interface portproxy show v4tov4` to look them up.

```powershell
$WindowsIP = '192.168.1.50'
$WebUIPort = 3000
netsh.exe interface portproxy delete v4tov4 `
    "listenaddress=$WindowsIP" "listenport=$WebUIPort"
Remove-NetFirewallRule -Name 'LocalAIFerry-WebUI-NAT'
```

### Remove mirrored access and return to NAT

Remove the Hyper-V rule in elevated PowerShell.
[Remove a Hyper-V firewall rule](https://learn.microsoft.com/en-us/powershell/module/netsecurity/remove-netfirewallhypervrule)

```powershell
Remove-NetFirewallHyperVRule -Name 'LocalAIFerry-WebUI-Mirrored'
```

To return WSL to NAT, run the following in PowerShell as the Windows
account that owns Debian. It changes the `networkingMode=mirrored` line
added during setup. Finish active WSL work before running it.

```powershell
$WslConfig = Join-Path $env:USERPROFILE '.wslconfig'
$Settings = [IO.File]::ReadAllText($WslConfig)
$NatSettings = $Settings -replace '(?m)^networkingMode=mirrored(?=\r?$)', 'networkingMode=nat'
if ($NatSettings -ceq $Settings) {
    throw 'Set networkingMode=nat under [wsl2] in .wslconfig before restarting WSL.'
}
[IO.File]::WriteAllText($WslConfig, $NatSettings)
wsl.exe --shutdown
wsl.exe --distribution Debian
```
