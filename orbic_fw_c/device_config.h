/*
 * DagShell Device Configuration - Orbic RCL400
 * Device-specific paths and constants for Orbic
 */

#ifndef DEVICE_CONFIG_H
#define DEVICE_CONFIG_H

/* Device identification */
#define DEVICE_NAME "Orbic RCL400"
#define DEVICE_VERSION "v2.1"
#define FIRMWARE_NAME "DagShell Orbic"

/* Port configuration */
#define WEB_PORT 8443 /* HTTPS WebUI port */

/* Device paths - Orbic specific */
#define MODEM_PORT "/dev/smd8"
#define GPS_PORT "/dev/smd8" /* GPS via AT commands on same port */

/* Data storage */
#define DATA_DIR "/data"
#define LOG_FILE DATA_DIR "/dagshell.log"
#define CONFIG_FILE DATA_DIR "/config.json"
#define WARDRIVE_DIR DATA_DIR "/wardrive"

/* Certificate paths */
#define CERT_FILE DATA_DIR "/server.der"
#define ROOT_CERT_FILE DATA_DIR "/root.der"
#define KEY_FILE DATA_DIR "/server.key.der"

/* Network */
#define DEFAULT_GATEWAY "192.168.1.1" /* Orbic default IP */
#define WIFI_INTERFACE "wlan0"

/* Feature flags */
#define HAS_MODEM 1
#define HAS_GPS 1
#define HAS_SMS 1
#define HAS_WIFI_SCAN 1
#define HAS_DEAUTH 1

#endif /* DEVICE_CONFIG_H */
