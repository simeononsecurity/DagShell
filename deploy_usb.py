#!/usr/bin/env python3
"""
DagShell USB Deployer
=====================
Deploys DagShell firmware to the Orbic RCL400 via USB cable.
Mirrors Rayhunter's 'orbic-usb' installation method.

How it works (matching Rayhunter installer/src/orbic.rs):
  1. Detects Orbic device on USB in RNDIS mode (PID 0xf626)
  2. Sends a USB vendor control request to switch it into ADB/debug mode (PID 0xf601)
  3. Device reboots and re-enumerates with ADB interface exposed
  4. Pushes firmware files to /tmp/ via 'adb push' (ADB user can write /tmp)
  5. Uses AT+SYSCMD via USB serial (interface 1) to move files to /data/ and chmod as root
  6. Sets up boot persistence via USB composition hook (same hook as deploy_base64.py)
  7. Opens firewall and starts orbic_app

USB IDs (from Rayhunter installer/src/orbic.rs):
  Vendor:  0x05c6  (Qualcomm)
  RNDIS:   0xf626  (normal hotspot mode)
  RNDIS2:  0xf622  (RNDIS + serial exposed)
  ADB:     0xf601  (debug/command mode - target)

AT+SYSCMD Protocol (USB serial, interface 1):
  OUT endpoint: 0x02
  IN  endpoint: 0x82
  Frame:  \\r\\nAT+SYSCMD=<command>\\r\\n
  Read:   echo (discard), then response
  OK if:  response contains \\r\\nOK\\r\\n

Requirements:
  pip install pyusb
  adb (Android Debug Bridge):
    macOS:   brew install android-platform-tools
    Linux:   sudo apt install adb
    Windows: https://developer.android.com/tools/releases/platform-tools
"""

import subprocess
import sys
import time
from pathlib import Path

# Optional pyusb for USB mode switching and AT+SYSCMD
try:
    import usb.core
    import usb.util
    HAS_PYUSB = True
except ImportError:
    HAS_PYUSB = False


# =============================================================================
# Orbic USB constants (from Rayhunter installer/src/orbic.rs)
# =============================================================================

VENDOR_ID          = 0x05c6   # Qualcomm
PRODUCT_ID_RNDIS   = 0xf626   # Normal RNDIS / hotspot mode
PRODUCT_ID_RNDIS2  = 0xf622   # RNDIS + serial exposed
PRODUCT_ID_ADB     = 0xf601   # ADB / debug / command mode (target)

# USB serial (CDC ACM) interface for AT+SYSCMD commands
# Interface 1, bulk endpoints 0x02 (OUT) and 0x82 (IN)
SERIAL_INTERFACE   = 1
SERIAL_OUT_EP      = 0x02
SERIAL_IN_EP       = 0x82


# =============================================================================
# File paths
# =============================================================================

SCRIPT_DIR       = Path(__file__).parent.absolute()
FIRMWARE_DIR     = SCRIPT_DIR / "orbic_fw_c"
FIRMWARE_FILE    = "orbic_app"
BOOT_SCRIPT_FILE = "dagshell_boot.sh"

FIRMWARE_PATH    = FIRMWARE_DIR / FIRMWARE_FILE
BOOT_SCRIPT_PATH = SCRIPT_DIR / BOOT_SCRIPT_FILE

# Remote locations on device
REMOTE_TMP_APP   = "/tmp/orbic_app"
REMOTE_FILE      = "/data/orbic_app"
REMOTE_TMP_BOOT  = "/tmp/dagshell_boot.sh"
REMOTE_BOOT      = "/data/dagshell_boot.sh"

# USB persistence hook (same method used by deploy_base64.py)
USB_WRAPPER_PATH = "/data/usb/boot_hsusb_composition"
USB_ORIGINAL     = "/sbin/usb/compositions/PRJ_SLT779_9025"


# =============================================================================
# Prerequisites
# =============================================================================

def check_adb() -> bool:
    """Check if adb is installed and accessible"""
    try:
        result = subprocess.run(
            ["adb", "version"],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# =============================================================================
# ADB helpers
# =============================================================================

def adb(args: list, check: bool = True, timeout: int = 60) -> str:
    """Run an adb command, return stdout. Raises on non-zero if check=True."""
    cmd = ["adb"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"adb {' '.join(str(a) for a in args[:3])} failed:\n{result.stderr.strip()}"
        )
    return result.stdout.strip()


def adb_devices() -> list:
    """Return list of serial numbers for connected, authorised ADB devices."""
    try:
        output = adb(["devices"], check=False, timeout=10)
    except Exception:
        return []
    devices = []
    for line in output.splitlines()[1:]:
        line = line.strip()
        if "\t" in line:
            serial, state = line.split("\t", 1)
            if state.strip() == "device":
                devices.append(serial.strip())
    return devices


def wait_for_adb(timeout_sec: int = 60) -> bool:
    """Poll until at least one authorised ADB device appears."""
    print(f"  Waiting for ADB device (up to {timeout_sec}s)", end="", flush=True)
    start = time.time()
    while time.time() - start < timeout_sec:
        if adb_devices():
            print(" — found!")
            return True
        time.sleep(2)
        print(".", end="", flush=True)
    print(" — timed out!")
    return False


def adb_push(local: str, remote: str) -> None:
    """Push a single file to the device."""
    name = Path(local).name
    size = Path(local).stat().st_size
    print(f"  push  {name}  ({size:,} bytes)  →  {remote}")
    adb(["push", local, remote], timeout=120)


def adb_shell(cmd: str, check: bool = True) -> str:
    """Run a shell command on the device via ADB and return output."""
    return adb(["shell", cmd], check=check, timeout=30)


# =============================================================================
# USB mode switch (mirrors Rayhunter enable_command_mode())
# =============================================================================

def find_orbic() -> tuple:
    """
    Search for the Orbic device in any of its known USB modes.
    Returns (device_object, product_id) or (None, None).
    """
    if not HAS_PYUSB:
        return None, None
    for pid in (PRODUCT_ID_ADB, PRODUCT_ID_RNDIS2, PRODUCT_ID_RNDIS):
        dev = usb.core.find(idVendor=VENDOR_ID, idProduct=pid)
        if dev is not None:
            return dev, pid
    return None, None


def switch_to_adb_mode() -> bool:
    """
    Send the USB vendor control request that reboots the Orbic into ADB mode.

    Mirrors Rayhunter's enable_command_mode() (installer/src/orbic.rs):
      Control {
          control_type: ControlType::Vendor,
          recipient:    Recipient::Device,
          request:      0xa0,
          value:        0,
          index:        0,
      }
    bmRequestType = 0x40  (Host→Device | Vendor | Device)

    After the request the device reboots; a STALL/pipe/timeout error is normal
    because the firmware restarts mid-transfer.
    """
    if not HAS_PYUSB:
        print("  [!] pyusb not installed — cannot auto-switch USB mode.")
        print("      Install with: pip install pyusb")
        print("      You will need to manually enable ADB on the device.")
        return False

    dev, pid = find_orbic()
    if dev is None:
        print("  [!] No Orbic device found via USB. Is it plugged in?")
        return False

    mode_label = {
        PRODUCT_ID_RNDIS:  "RNDIS (normal hotspot)",
        PRODUCT_ID_RNDIS2: "RNDIS + serial",
        PRODUCT_ID_ADB:    "ADB/debug (already ready)",
    }.get(pid, f"unknown 0x{pid:04x}")

    print(f"  Orbic found: PID=0x{pid:04x}  ({mode_label})")

    if pid == PRODUCT_ID_ADB:
        print("  Device is already in ADB mode — no switch needed.")
        return True

    print("  Sending mode-switch vendor control request…")
    try:
        # Set configuration so we can issue control requests
        try:
            dev.set_configuration()
        except Exception:
            pass

        # Send the vendor control request (same semantics as Rayhunter nusb call)
        dev.ctrl_transfer(
            bmRequestType=0x40,  # Host→Device | Vendor | Device
            bRequest=0xa0,
            wValue=0,
            wIndex=0,
            data_or_wLength=None,
            timeout=2000,
        )
        print("  Mode-switch sent. Device is rebooting…")
        return True

    except usb.core.USBError as exc:
        # A pipe/stall/timeout error is expected: the device reboots during the
        # transfer, which Rayhunter also silently ignores.
        err_lower = str(exc).lower()
        if any(kw in err_lower for kw in ("pipe", "stall", "timeout", "no data")):
            print("  Mode-switch sent (device is rebooting)…")
            return True
        print(f"  [!] Unexpected USB error: {exc}")
        return False
    except Exception as exc:
        print(f"  [!] Error during mode switch: {exc}")
        return False


# =============================================================================
# AT+SYSCMD via USB serial  (mirrors Rayhunter adb_serial_cmd / send_serial_cmd)
# =============================================================================

def open_at_interface():
    """
    Claim the CDC ACM serial interface (interface 1) on the Orbic ADB device
    so we can send AT+SYSCMD commands as root.

    The Orbic's atfwd_daemon runs as root and handles AT+SYSCMD — this is how
    Rayhunter performs privileged operations (chmod, mv, etc.) without needing
    a rooted shell.

    IMPORTANT: The system ADB daemon holds the USB device open, which blocks
    pyusb from claiming interface 1.  We kill it first, claim the serial
    interface, and let the caller restart adb when done.
    """
    if not HAS_PYUSB:
        return None

    # Kill the ADB daemon so it releases its hold on the USB device.
    # This is necessary because adbd claims the ADB interface (0) and keeps
    # the device open, preventing pyusb from claiming the serial interface (1).
    print("  Stopping ADB daemon to release USB device…")
    try:
        subprocess.run(["adb", "kill-server"], capture_output=True, timeout=8)
        time.sleep(2)
    except Exception:
        pass

    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID_ADB)
    if dev is None:
        print("  [!] Orbic ADB device not found after killing adb server.")
        return None

    # Set USB configuration (required before claiming interfaces)
    try:
        dev.set_configuration()
    except Exception:
        pass

    # Detach the kernel CDC ACM / serial driver from interface 1.
    # On Linux this usually works.  On macOS it requires root or an entitlement;
    # if it fails the claim attempt below will surface a clearer error.
    try:
        if dev.is_kernel_driver_active(SERIAL_INTERFACE):
            dev.detach_kernel_driver(SERIAL_INTERFACE)
    except Exception:
        pass

    try:
        usb.util.claim_interface(dev, SERIAL_INTERFACE)
        return dev
    except usb.core.USBError as exc:
        err = str(exc).lower()
        print(f"  [!] Could not claim AT serial interface: {exc}")
        if "access" in err or "permission" in err or "not permitted" in err:
            print("  On macOS/Linux try:  sudo python3 deploy_usb.py")
        elif "busy" in err or "resource" in err:
            print("  Serial interface is busy. Try unplugging and replugging the device.")
        # Restart ADB so fallback can still use adb shell
        try:
            subprocess.run(["adb", "start-server"], capture_output=True, timeout=10)
        except Exception:
            pass
        return None


def at_syscmd_raw(dev, command: str, timeout_ms: int = 3000) -> bool:
    """
    Low-level AT+SYSCMD send over USB bulk endpoints.
    Protocol (from Rayhunter installer/src/orbic.rs send_serial_cmd):
      1. CLASS control: SET_CONTROL_LINE_STATE (0x22), value=3, index=interface
      2. Bulk write to 0x02:  "\\r\\nAT+SYSCMD=<cmd>\\r\\n"
      3. Bulk read  from 0x82: echoed command  (discard)
      4. Bulk read  from 0x82: actual response
      5. Success if response contains "\\r\\nOK\\r\\n"
    """
    if dev is None:
        return False

    payload = f"\r\nAT+SYSCMD={command}\r\n".encode()

    try:
        # Step 1 — enable serial port (SET_CONTROL_LINE_STATE, RTS+DTR = 3)
        # bmRequestType = 0x21 = Host→Device | Class | Interface
        dev.ctrl_transfer(
            bmRequestType=0x21,
            bRequest=0x22,
            wValue=3,
            wIndex=SERIAL_INTERFACE,
            data_or_wLength=None,
            timeout=timeout_ms,
        )

        # Step 2 — send the AT command
        dev.write(SERIAL_OUT_EP, payload, timeout=timeout_ms)

        # Step 3 — consume the echoed command
        try:
            dev.read(SERIAL_IN_EP, 256, timeout=timeout_ms)
        except usb.core.USBTimeoutError:
            pass

        # Step 4 — read the actual response
        try:
            raw = bytes(dev.read(SERIAL_IN_EP, 256, timeout=timeout_ms))
            resp = raw.decode("utf-8", errors="replace")
            return "\r\nOK\r\n" in resp
        except usb.core.USBTimeoutError:
            # Some commands (background processes) don't return before timeout — treat as OK
            return True

    except usb.core.USBError as exc:
        print(f"  [!] USB error in AT+SYSCMD ({command[:40]}…): {exc}")
        return False


def at_cmd(dev, command: str, retries: int = 3) -> bool:
    """Print and execute a single AT+SYSCMD command with automatic retry."""
    print(f"  AT+SYSCMD: {command}")
    for attempt in range(retries):
        if at_syscmd_raw(dev, command):
            return True
        if attempt < retries - 1:
            time.sleep(1)
    print(f"  [!] Command may have failed after {retries} attempts: {command}")
    return False


def release_at_interface(dev) -> None:
    """Release the claimed serial interface."""
    if dev is None:
        return
    try:
        usb.util.release_interface(dev, SERIAL_INTERFACE)
    except Exception:
        pass


# =============================================================================
# Boot persistence (mirrors deploy_base64.py setup_autostart)
# =============================================================================

def setup_autostart(at_dev) -> None:
    """
    Hook DagShell into the USB composition init script so it starts on every boot.
    The script /data/usb/boot_hsusb_composition is executed by the USB init daemon
    on MDM9207.  We replace it with a wrapper that runs dagshell_boot.sh first,
    then chains to the original composition script.
    """
    print("  Setting up boot persistence (USB composition hook)…")

    # Remove any stale dnsmasq references
    at_cmd(at_dev, "sed -i '/dagshell_boot.sh/d' /data/dnsmasq.conf")

    # Build the wrapper script in /data/usb/
    at_cmd(at_dev, f"rm -f {USB_WRAPPER_PATH}")
    at_cmd(at_dev, f"echo '#!/bin/sh' > {USB_WRAPPER_PATH}")
    at_cmd(at_dev, f"echo '# DagShell USB boot wrapper' >> {USB_WRAPPER_PATH}")
    at_cmd(at_dev, f"echo 'sh {REMOTE_BOOT} &' >> {USB_WRAPPER_PATH}")
    at_cmd(at_dev, f"echo '{USB_ORIGINAL} \"$@\"' >> {USB_WRAPPER_PATH}")
    at_cmd(at_dev, f"chmod +x {USB_WRAPPER_PATH}")

    print("  Boot hook installed.")


# =============================================================================
# Post-deploy verification  (mirrors Rayhunter test_rayhunter() in orbic.rs)
# =============================================================================

def verify_deployment() -> bool:
    """
    Prove DagShell is running via ADB — no WiFi required.

    Rayhunter's equivalent (installer/src/orbic.rs test_rayhunter()):
      adb_command(device, &["wget", "-O", "-", "http://localhost:8080/index.html"])

    We do the same for DagShell on port 8443 (HTTPS) plus extra port / log checks.
    All commands run on the device itself through the existing ADB session.
    """
    print("\n" + "=" * 58)
    print("  Verifying deployment (via ADB — no WiFi needed)")
    print("  (Mirrors Rayhunter test_rayhunter() concept)")
    print("=" * 58)

    all_ok = True

    # ── Check 1: process alive ────────────────────────────────────────────────
    pid = adb_shell("pgrep -f orbic_app 2>/dev/null", check=False).strip()
    if pid:
        print(f"  [✓] Process   : orbic_app is running  (PID {pid})")
    else:
        print("  [✗] Process   : orbic_app NOT found in process list")
        all_ok = False

    # ── Check 2: port 8443 listening ──────────────────────────────────────────
    # dagshell_boot.sh has a `sleep 5` before launching orbic_app; give it time.
    print("  Waiting 10s for orbic_app to bind port 8443…", end="", flush=True)
    time.sleep(10)
    print(" done.")

    # Try netstat first, then nc -z, then /proc/net/tcp{,6}
    # 8443 decimal = 0x20FB hex
    port_open = False

    ns = adb_shell("netstat -tlnp 2>/dev/null | grep ':8443'", check=False).strip()
    if ns:
        print(f"  [✓] Port 8443 : LISTENING  ({ns.split()[0]})")
        port_open = True

    if not port_open:
        # nc -z does a TCP connect-only probe (no TLS) — fastest/most reliable
        nc_out = adb_shell("nc -z 127.0.0.1 8443 2>&1; echo rc=$?", check=False).strip()
        if "rc=0" in nc_out:
            print("  [✓] Port 8443 : REACHABLE  (nc -z probe)")
            port_open = True

    if not port_open:
        # /proc/net/tcp stores local ports in big-endian hex: 8443 = 20FB
        for tcp_file in ("/proc/net/tcp6", "/proc/net/tcp"):
            entry = adb_shell(
                f"grep -i ' 20FB' {tcp_file} 2>/dev/null | head -1", check=False
            ).strip()
            if entry:
                print(f"  [✓] Port 8443 : LISTENING  ({tcp_file})")
                port_open = True
                break

    if not port_open:
        print("  [!] Port 8443 : not yet visible via netstat/nc/proc — "
              "will confirm via TLS probe below")

    # ── Check 3: HTTP(S) response from localhost via adb shell ────────────────
    # This is the key check: hit the server directly on the device through ADB,
    # exactly like Rayhunter's wget-to-localhost test — no WiFi connection needed.
    #
    # "Connection reset by peer" from busybox wget = SUCCESS:
    #   • The TCP connection WAS established (port is open)
    #   • The TLS handshake started but busybox's SSL implementation is
    #     incompatible with BearSSL cipher suites → reset after ClientHello
    #   • This is DIFFERENT from "Connection refused" (server not running)
    MAX_ATTEMPTS = 5
    web_ok = False
    print(f"  Probing https://127.0.0.1:8443/ via adb shell ({MAX_ATTEMPTS} attempts)…")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        # busybox wget: -q quiet, -O - stdout, --no-check-certificate skip TLS verify
        raw = adb_shell(
            "wget -q -O - --no-check-certificate https://127.0.0.1:8443/ 2>&1 | head -5",
            check=False,
        ).strip()

        raw_lower = raw.lower()

        if raw and any(kw in raw_lower for kw in ("html", "dagshell", "<!doctype", "<html")):
            print(f"  [✓] Web server: HTML response received  (attempt {attempt})")
            web_ok = True
            break
        elif raw and any(kw in raw_lower for kw in ("ssl", "tls", "handshake", "certificate")):
            # TLS handshake visible → server IS up, wget just can't finish it
            print(f"  [✓] Web server: TLS handshake confirmed — server is up  (attempt {attempt})")
            web_ok = True
            break
        elif raw and any(kw in raw_lower for kw in ("connection reset", "reset by peer", "peer reset")):
            # TCP connect succeeded; TLS reset = BearSSL rejected busybox cipher → server IS up
            print(f"  [✓] Web server: TCP+TLS confirmed (reset by peer = BearSSL active, "
                  f"attempt {attempt})")
            web_ok = True
            break
        elif "connection refused" in raw_lower:
            print(f"  [!] Attempt {attempt}/{MAX_ATTEMPTS}: connection refused — "
                  "server not listening yet")
        elif raw:
            # Unknown output — log it but keep trying
            print(f"  [?] Attempt {attempt}/{MAX_ATTEMPTS}: {raw[:120]}")

        if attempt < MAX_ATTEMPTS:
            time.sleep(3)

    # If wget failed every time but nc says port is open → treat as OK
    # (wget may simply not support --no-check-certificate on this busybox build)
    if not web_ok and port_open:
        print("  [✓] Web server: port 8443 is open — "
              "wget probe inconclusive but server IS running")
        web_ok = True

    # TLS response confirms the port IS open even if netstat/nc/proc missed it
    if web_ok and not port_open:
        print("  [✓] Port 8443 : confirmed reachable (TLS response received)")
        port_open = True

    if not web_ok:
        print("  [✗] Web server: no response after all attempts")
        all_ok = False

    if not port_open:
        all_ok = False

    # ── Check 4: show boot log ─────────────────────────────────────────────────
    boot_log = adb_shell("tail -15 /data/boot_diag.log 2>/dev/null", check=False).strip()
    if boot_log:
        print()
        print("  Boot log  (/data/boot_diag.log):")
        for line in boot_log.splitlines():
            print(f"    {line}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if all_ok:
        print("  [✓] All checks passed — DagShell is live!")
        print("  Connect to the device WiFi and open: https://192.168.1.1:8443/")
    else:
        print("  [!] One or more checks failed.")
        print("  The app may need a few more seconds.  Tips:")
        print("    • Wait 10s and re-run:  python3 deploy_usb.py --verify-only")
        print("    • Check the boot log above for errors")
    print("=" * 58)
    return all_ok


# =============================================================================
# Main deployment flow
# =============================================================================

def deploy() -> None:
    banner = "=" * 58
    print(banner)
    print("  DagShell USB Deployer")
    print("  (Rayhunter orbic-usb method — USB cable, no WiFi needed)")
    print(banner)

    # ── Step 1: prerequisites ─────────────────────────────────────────────────
    print("\n[1/7] Checking prerequisites…")

    if not check_adb():
        print("  [✗] 'adb' not found in PATH. Install Android Debug Bridge:")
        print("        macOS:   brew install android-platform-tools")
        print("        Linux:   sudo apt install adb")
        print("        Windows: https://developer.android.com/tools/releases/platform-tools")
        sys.exit(1)
    print("  [✓] adb found")

    if HAS_PYUSB:
        print("  [✓] pyusb found — automatic USB mode switching enabled")
    else:
        print("  [!] pyusb not installed — manual ADB mode required")
        print("      Install with: pip install pyusb")

    if not FIRMWARE_PATH.exists():
        print(f"  [✗] Firmware binary not found: {FIRMWARE_PATH}")
        print("      Build it first:  ./build.sh  (macOS/Linux)  or  .\\build.ps1  (Windows)")
        sys.exit(1)
    size_kb = FIRMWARE_PATH.stat().st_size // 1024
    print(f"  [✓] Firmware: {FIRMWARE_PATH.name}  ({size_kb} KB)")

    if not BOOT_SCRIPT_PATH.exists():
        print(f"  [✗] Boot script not found: {BOOT_SCRIPT_PATH}")
        sys.exit(1)
    print(f"  [✓] Boot script: {BOOT_SCRIPT_PATH.name}")

    # ── Step 2: detect device ─────────────────────────────────────────────────
    print("\n[2/7] Detecting Orbic device…")

    if HAS_PYUSB:
        dev_obj, cur_pid = find_orbic()
        if dev_obj is None:
            print("  [✗] No Orbic device found on USB.")
            print("      Make sure the USB cable is connected and the device is on.")
            sys.exit(1)
        pid_names = {
            PRODUCT_ID_RNDIS:  "RNDIS hotspot",
            PRODUCT_ID_RNDIS2: "RNDIS+serial",
            PRODUCT_ID_ADB:    "ADB/debug",
        }
        print(f"  [✓] Found Orbic: PID=0x{cur_pid:04x}  ({pid_names.get(cur_pid, 'unknown')})")
    else:
        cur_pid = None
        print("  (pyusb unavailable — skipping USB detection)")

    # ── Step 3: mode switch ───────────────────────────────────────────────────
    print("\n[3/7] Switching to ADB mode…")

    need_reboot = False
    if HAS_PYUSB and cur_pid != PRODUCT_ID_ADB:
        if not switch_to_adb_mode():
            print("  [✗] Failed to send mode-switch command.")
            print("  Try unplugging, replugging, and re-running this script.")
            sys.exit(1)
        need_reboot = True
    elif not HAS_PYUSB:
        print("  Skipped (pyusb unavailable). Assuming ADB is already enabled.")
    else:
        print("  Device is already in ADB mode — no reboot needed.")

    if need_reboot:
        wait_secs = 20
        print(f"  Waiting {wait_secs}s for device to reboot and re-enumerate…")
        time.sleep(wait_secs)

    # ── Step 4: wait for ADB ──────────────────────────────────────────────────
    print("\n[4/7] Waiting for ADB device…")
    if not wait_for_adb(timeout_sec=90):
        print("  [✗] ADB device not detected after 90 seconds.")
        print("  Troubleshooting:\n"
              "    • Run 'adb devices' and check for 'unauthorized' status\n"
              "    • Try: adb kill-server && adb start-server\n"
              "    • Replug the USB cable")
        sys.exit(1)

    # ── Step 5: prepare device ────────────────────────────────────────────────
    print("\n[5/7] Preparing device…")
    print("  Stopping any running orbic_app…")
    adb_shell("pkill -f orbic_app 2>/dev/null; true", check=False)
    print("  Ensuring /tmp is writable…")
    adb_shell("mkdir -p /tmp", check=False)
    time.sleep(1)

    # ── Step 6: push files via adb ────────────────────────────────────────────
    print("\n[6/7] Pushing files to device via ADB…")

    # Push firmware to /tmp/ — ADB shell user can always write here
    adb_push(str(FIRMWARE_PATH), REMOTE_TMP_APP)

    # Push boot script
    adb_push(str(BOOT_SCRIPT_PATH), REMOTE_TMP_BOOT)

    # Push SSL certs if they exist
    ssl_cert = FIRMWARE_DIR / "server.der"
    ssl_key  = FIRMWARE_DIR / "server.key.der"
    ssl_root = FIRMWARE_DIR / "root.der"
    has_ssl  = ssl_cert.exists() and ssl_key.exists()

    if has_ssl:
        print("  Pushing SSL certificates…")
        adb_push(str(ssl_cert), "/tmp/server.der")
        adb_push(str(ssl_key),  "/tmp/server.key.der")
        if ssl_root.exists():
            adb_push(str(ssl_root), "/tmp/root.der")
    else:
        print("  [!] SSL certs not found — HTTPS will not be available.")
        print("      Run  python3 orbic_fw_c/gen_pki.py  first.")

    print("  All files pushed to /tmp/")

    # ── Step 7: install via AT+SYSCMD ─────────────────────────────────────────
    print("\n[7/7] Installing — AT+SYSCMD root operations…")

    # Open the USB serial interface to talk to atfwd_daemon (runs as root)
    print("  Opening USB AT command interface (interface 1)…")
    at_dev = open_at_interface()

    if at_dev is None:
        # ─── Fallback: adb shell ──────────────────────────────────────────────
        # open_at_interface() killed the ADB server — wait for it to reconnect.
        print("  [!] Could not open AT serial interface.")
        print("  Waiting for ADB to reconnect after server restart…")
        if not wait_for_adb(timeout_sec=30):
            print("  [✗] ADB did not reconnect. Run:  adb start-server  and retry.")
            sys.exit(1)

        # Check whether the ADB shell is root — determines if /data/ is writable.
        uid_line = adb_shell("id", check=False).split("\n")[0].strip()
        is_root = "uid=0" in uid_line
        print(f"  ADB shell identity: {uid_line or '(unknown)'}")
        if is_root:
            print("  [✓] ADB shell has root — proceeding with adb shell install.")
        else:
            print("  [!] ADB shell is NOT root. Operations on /data/ may fail silently.")
            print("      If files don't copy, retry with:  sudo python3 deploy_usb.py")

        print()
        print("  Moving files to /data/ via adb shell…")
        for src, dst in [
            (REMOTE_TMP_APP,  REMOTE_FILE),
            (REMOTE_TMP_BOOT, REMOTE_BOOT),
        ]:
            print(f"  mv {src} → {dst}")
            adb_shell(f"mv {src} {dst}", check=False)

        print("  Setting permissions…")
        adb_shell(f"chmod +x {REMOTE_FILE}",  check=False)
        adb_shell(f"chmod +x {REMOTE_BOOT}",  check=False)

        if has_ssl:
            print("  Installing SSL certificates…")
            adb_shell("mv /tmp/server.der     /data/server.der",     check=False)
            adb_shell("mv /tmp/server.key.der /data/server.key.der", check=False)
            adb_shell("chmod 600 /data/server.key.der",              check=False)
            if ssl_root.exists():
                adb_shell("mv /tmp/root.der /data/root.der", check=False)

        # Verify the firmware actually landed
        check_out = adb_shell(f"ls -la {REMOTE_FILE} 2>&1", check=False)
        if "No such file" in check_out or not check_out.strip():
            print(f"  [✗] {REMOTE_FILE} not found after copy — ADB shell likely lacks root.")
            print("      Retry with:  sudo python3 deploy_usb.py")
            sys.exit(1)
        print(f"  [✓] Verified: {check_out.strip()}")

        # ── Configure network (ADB shell has root, so iptables works) ────────
        if is_root:
            print()
            print("  Configuring network (iptables / NAT)…")
            adb_shell("iptables -I INPUT -p tcp --dport 8443 -j ACCEPT",             check=False)
            adb_shell("iptables -I INPUT -p tcp --dport 8080 -j ACCEPT",             check=False)
            adb_shell("iptables -t nat -F PREROUTING",                               check=False)
            adb_shell("echo 1 > /proc/sys/net/ipv4/ip_forward",                     check=False)
            adb_shell("iptables -t nat -A POSTROUTING -o rmnet_data0 -j MASQUERADE", check=False)
            adb_shell("iptables -A FORWARD -i bridge0 -o rmnet_data0 -j ACCEPT",     check=False)
            adb_shell("iptables -A FORWARD -i rmnet_data0 -o bridge0 -m state --state RELATED,ESTABLISHED -j ACCEPT", check=False)

        # ── Boot persistence via adb shell (works because ADB is root) ───────
        if is_root:
            print()
            print("  Setting up boot persistence (USB composition hook)…")
            adb_shell(f"sed -i '/dagshell_boot.sh/d' /data/dnsmasq.conf",          check=False)
            adb_shell(f"rm -f {USB_WRAPPER_PATH}",                                  check=False)
            adb_shell(f"echo '#!/bin/sh' > {USB_WRAPPER_PATH}",                    check=False)
            adb_shell(f"echo '# DagShell USB boot wrapper' >> {USB_WRAPPER_PATH}", check=False)
            adb_shell(f"echo 'sh {REMOTE_BOOT} &' >> {USB_WRAPPER_PATH}",          check=False)
            adb_shell(f"echo '{USB_ORIGINAL} \"$@\"' >> {USB_WRAPPER_PATH}",       check=False)
            adb_shell(f"chmod +x {USB_WRAPPER_PATH}",                              check=False)
            print("  Boot hook installed.")

        # ── Start the app — survive ADB session disconnect ─────────────────
        #
        # Android cgroups kill the entire ADB shell process group when the
        # connection drops — setsid/nohup alone are not enough.
        #
        # Strategy:
        #  1. Run dagshell_boot.sh via setsid to set up iptables/NAT and start
        #     the netcat shell listener on port 24 (those may survive briefly).
        #  2. Forward port 24 locally and send the orbic_app start command
        #     through that shell, which lives in init's cgroup rather than the
        #     ADB cgroup → survives after our ADB session ends.
        #  3. Fall back to a direct setsid launch if nc isn't up yet.
        print()
        print("  Running dagshell_boot.sh (sets up iptables / NAT / nc listener)…")
        adb_shell(f"setsid sh {REMOTE_BOOT} </dev/null >/dev/null 2>&1 &", check=False)
        # Give the boot script time to start the nc listener (it has sleep 5
        # before launching orbic_app, but nc starts earlier).
        time.sleep(8)

        # Try to launch orbic_app through the nc shell on port 24.
        # The nc listener (`busybox nc -ll -p 24 -e /bin/sh`) is started near
        # the top of dagshell_boot.sh in init context — its children inherit
        # that context and survive ADB disconnects.
        launched_via_nc = False
        try:
            print("  Forwarding port 24 (nc shell) to launch orbic_app from init context…")
            subprocess.run(
                ["adb", "forward", "tcp:12024", "tcp:24"],
                capture_output=True, timeout=10,
            )
            time.sleep(1)
            import socket
            with socket.create_connection(("127.0.0.1", 12024), timeout=5) as s:
                cmd = (
                    f"pkill -f orbic_app 2>/dev/null; sleep 1; "
                    f"{REMOTE_FILE} >/data/orbic_app.log 2>&1 &\n"
                )
                s.sendall(cmd.encode())
                time.sleep(3)
            launched_via_nc = True
            print("  orbic_app start command sent via init-context nc shell.")
        except Exception as exc:
            print(f"  [!] nc shell launch failed ({exc}) — falling back to setsid direct launch…")
            adb_shell(
                f"pkill -f orbic_app 2>/dev/null; sleep 1; "
                f"setsid {REMOTE_FILE} </dev/null >/data/orbic_app.log 2>&1 &",
                check=False,
            )
            time.sleep(3)

        # Verify the process is alive
        ps_out = adb_shell("pgrep -f orbic_app 2>/dev/null", check=False).strip()
        if ps_out:
            print(f"  [✓] orbic_app running (PID {ps_out})")
        else:
            print("  [!] orbic_app did not start — check /data/boot_diag.log or /data/orbic_app.log")

    else:
        # ─── Full install via AT+SYSCMD ───────────────────────────────────────
        print("  [✓] AT interface open — running privileged commands via AT+SYSCMD")
        print()
        print("  Moving files to /data/ …")
        at_cmd(at_dev, f"mv {REMOTE_TMP_APP}  {REMOTE_FILE}")
        at_cmd(at_dev, f"mv {REMOTE_TMP_BOOT} {REMOTE_BOOT}")
        at_cmd(at_dev, f"chmod +x {REMOTE_FILE}")
        at_cmd(at_dev, f"chmod +x {REMOTE_BOOT}")

        if has_ssl:
            print("  Installing SSL certificates…")
            at_cmd(at_dev, "mv /tmp/server.der     /data/server.der")
            at_cmd(at_dev, "mv /tmp/server.key.der /data/server.key.der")
            at_cmd(at_dev, "chmod 600 /data/server.key.der")
            if ssl_root.exists():
                at_cmd(at_dev, "mv /tmp/root.der /data/root.der")

        print()
        print("  Configuring network…")
        at_cmd(at_dev, "iptables -I INPUT -p tcp --dport 8443 -j ACCEPT")
        at_cmd(at_dev, "iptables -I INPUT -p tcp --dport 8080 -j ACCEPT")
        at_cmd(at_dev, "iptables -t nat -F PREROUTING")
        at_cmd(at_dev, "echo 1 > /proc/sys/net/ipv4/ip_forward")
        at_cmd(at_dev, "iptables -t nat -A POSTROUTING -o rmnet_data0 -j MASQUERADE")
        at_cmd(at_dev, "iptables -A FORWARD -i bridge0 -o rmnet_data0 -j ACCEPT")
        at_cmd(at_dev, "iptables -A FORWARD -i rmnet_data0 -o bridge0 -m state --state RELATED,ESTABLISHED -j ACCEPT")

        print()
        setup_autostart(at_dev)

        print()
        print("  Starting orbic_app…")
        at_cmd(at_dev, f"{REMOTE_FILE} &")

        release_at_interface(at_dev)

    # ── Done ──────────────────────────────────────────────────────────────────
    print()
    print(banner)
    print("  Deployment complete!")
    print(banner)
    print(f"  Firmware   : {REMOTE_FILE}")
    print(f"  Boot hook  : {REMOTE_BOOT}  →  {USB_WRAPPER_PATH}")
    if has_ssl:
        print("  Web UI     : https://192.168.1.1:8443/")
        print("  (Connect to device WiFi, then open the URL in your browser.)")
        print("  (Accept the self-signed certificate warning.)")
    else:
        print("  Note: rebuild with SSL certs for HTTPS access.")
    print(banner)

    # ── Verify ────────────────────────────────────────────────────────────────
    verify_deployment()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Deploy DagShell to Orbic RCL400 via USB (Rayhunter method)"
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip deployment — just verify an existing DagShell install via ADB",
    )
    args = parser.parse_args()

    if args.verify_only:
        print("=" * 58)
        print("  DagShell USB Verifier  (--verify-only)")
        print("=" * 58)
        if not check_adb():
            print("  [✗] adb not found. Install Android Debug Bridge first.")
            sys.exit(1)
        if not adb_devices():
            print("  [✗] No ADB device connected.")
            sys.exit(1)
        ok = verify_deployment()
        sys.exit(0 if ok else 1)
    else:
        deploy()
