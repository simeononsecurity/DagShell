# USB Cable Deployment (`deploy_usb.py`)

## Overview

`deploy_usb.py` is a new one-command installer that deploys DagShell firmware to the Orbic RCL400 entirely over a USB cable — no existing WiFi connection or network access to the device required. It mirrors the approach used by the [Rayhunter](https://github.com/EFForg/rayhunter) project's `orbic-usb` installer and extends it with DagShell-specific setup, TLS certificate management, firewall configuration, and post-deploy verification.

---

## Why This Matters

Previous DagShell installers (`deploy_base64.py`, `deploy_net.py`) required the user to already be connected to the Orbic's WiFi hotspot or have the device in a known network state. This created a chicken-and-egg problem for fresh devices, brick-recovery scenarios, or cases where the WiFi interface was misconfigured.

`deploy_usb.py` eliminates that dependency: if you can plug in a USB cable and run `python3 deploy_usb.py`, you can get DagShell running.

---

## What It Does

### 1. USB Mode Switch (mirrors Rayhunter `enable_command_mode()`)
Detects the Orbic on USB via PyUSB. If the device is in normal RNDIS hotspot mode (`PID 0xf626`) it sends the vendor control request (`bmRequestType=0x40`, `bRequest=0xa0`) that reboots the device into ADB/debug mode (`PID 0xf601`). This is the identical mechanism Rayhunter uses.

### 2. File Deployment via ADB
Once in ADB mode, all firmware files are pushed to `/tmp/` (always writable by the ADB shell user), then moved to `/data/` with correct permissions via either:
- **AT+SYSCMD** (preferred) — runs as root via the device's `atfwd_daemon` system process, matching Rayhunter's `adb_serial_cmd` approach.
- **ADB shell fallback** — used automatically if the USB serial interface can't be claimed (e.g., macOS permissions). Requires ADB shell to have root (which it does on stock Orbic RCL400).

Files deployed:
| Local | Device |
|---|---|
| `orbic_fw_c/orbic_app` | `/data/orbic_app` |
| `dagshell_boot.sh` | `/data/dagshell_boot.sh` |
| `orbic_fw_c/server.der` | `/data/server.der` |
| `orbic_fw_c/server.key.der` | `/data/server.key.der` |
| `orbic_fw_c/root.der` | `/data/root.der` |

### 3. Firewall & NAT Configuration
Applies iptables rules immediately (not just on next boot):
```sh
iptables -I INPUT -p tcp --dport 8443 -j ACCEPT   # DagShell HTTPS UI
iptables -I INPUT -p tcp --dport 8080 -j ACCEPT   # Rayhunter / HTTP probe
iptables -t nat -A POSTROUTING -o rmnet_data0 -j MASQUERADE
iptables -A FORWARD -i bridge0 -o rmnet_data0 -j ACCEPT
iptables -A FORWARD -i rmnet_data0 -o bridge0 -m state --state RELATED,ESTABLISHED -j ACCEPT
echo 1 > /proc/sys/net/ipv4/ip_forward
```

### 4. Boot Persistence (USB Composition Hook)
Installs a wrapper at `/data/usb/boot_hsusb_composition` that runs `dagshell_boot.sh` on every USB init cycle, then chains to the original composition script. This is the same persistence mechanism used by the existing `deploy_base64.py`.

### 5. Persistent Process Launch
Starting a daemon from an ADB shell is non-trivial on Android: the OS uses cgroups and kills all child processes of an ADB session when the connection drops — `nohup` and `setsid` alone are insufficient.

`deploy_usb.py` solves this using a two-phase approach:
1. Run `dagshell_boot.sh` via `setsid` to bring up iptables and — crucially — the `busybox nc -ll -p 24 -e /bin/sh` listener.
2. Connect back to that netcat shell (forwarded via `adb forward tcp:12024 tcp:24`) and issue the `orbic_app` start command *from within that shell*. Because the nc listener was started from the USB init cgroup (not the ADB cgroup), its children survive the ADB disconnect.

If the nc shell is not yet available, the script falls back to a direct `setsid` launch with output logged to `/data/orbic_app.log`.

### 6. Post-Deploy Verification (mirrors Rayhunter `test_rayhunter()`)
After deployment, the script verifies DagShell is live entirely through ADB — no WiFi needed:
- Process check via `pgrep -f orbic_app`
- Port 8443 check via `netstat`, `nc -z`, and `/proc/net/tcp` (hex `20FB`)
- TLS probe via `busybox wget --no-check-certificate https://127.0.0.1:8443/`
- "Connection reset by peer" is treated as a success (BearSSL rejects BusyBox's cipher suites but the TCP+TLS exchange confirms the server is live)
- Boot log tail from `/data/boot_diag.log`

---

## SSL Certificate Management (`gen_pki.py`)

### Problem Solved
The original `gen_pki.py` generated leaf server certificates with only **365-day validity**. An expired certificate causes BearSSL's `br_ssl_server_init_full_rsa` to silently abort TLS handshakes, making the web UI unreachable even with the firewall open. This was the root cause of HTTPS connectivity failures starting ~1 year after initial deployment.

### Fix
`gen_pki.py` now generates both the Root CA and the leaf server certificate with **3650-day validity (10 years)**. The `datetime.utcnow()` deprecation warnings are also resolved by switching to `datetime.now(datetime.timezone.utc)`.

To regenerate certificates:
```sh
cd orbic_fw_c
python3 gen_pki.py
# Then redeploy: python3 ../deploy_usb.py
```

---

## Quick Start

```sh
# Install dependencies
pip install pyusb

# (macOS) Install ADB
brew install android-platform-tools

# Generate fresh TLS certificates (first time only)
cd orbic_fw_c && python3 gen_pki.py && cd ..

# Deploy — plug in USB cable first
python3 deploy_usb.py

# Or verify an existing install without redeploying
python3 deploy_usb.py --verify-only
```

Once deployed, connect to the Orbic's WiFi hotspot and open:
```
https://192.168.1.1:8443/
```
Accept the self-signed certificate warning (or install `root.der` as a trusted CA on your device).

---

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.8+ | Standard library only for ADB fallback |
| `pyusb` | `pip install pyusb` — needed for USB mode switch & AT+SYSCMD |
| `adb` | Android Debug Bridge in PATH |
| macOS/Linux | `sudo` may be needed to claim USB serial interface on macOS |
| Windows | WSL recommended; native support via `libusb` + Zadig driver |
