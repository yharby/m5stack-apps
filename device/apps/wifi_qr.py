"""Switch UIFlow2's saved Wi-Fi network with the CoreS3 camera.

The camera starts immediately. Hold a standard Wi-Fi QR code in view, for
example ``WIFI:T:WPA;S:My network;P:secret;;``. The app tests the connection
before committing it to the same NVS keys used by UIFlow2 2.5.1 Settings.

The QR password is never drawn or logged. Press the power button at any time
to exit; after a successful connection, the on-screen EXIT button also works.
"""

import time

import camera
import esp32
import image  # noqa: F401 -- registers image methods used by camera.snapshot()
import M5
import machine
import network
from M5 import BtnPWR, Widgets

CONNECT_ATTEMPTS = 2
CONNECT_TIMEOUT_MS = 15000
STATUS_GRACE_MS = 750
SCAN_LOG_INTERVAL_MS = 10000

BG = 0x101820
FG = 0xF2F5F7
DIM = 0xAAB8C2
GREEN = 0x35D07F
RED = 0xFF5A67
BLUE = 0x46A6FF

STATE_SCAN = 0
STATE_CONFIRM = 1
STATE_CONNECTED = 2
STATE_ERROR = 3

state = STATE_SCAN
camera_active = False
scan_frames = 0
scan_log_at = 0
pending_wifi = None


class ExitRequested(Exception):
    pass


def split_unescaped(value, separator):
    """Split on separator while preserving backslash escapes for unescape()."""
    parts = []
    current = []
    escaped = False
    for char in value:
        if escaped:
            current.append("\\")
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == separator:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    parts.append("".join(current))
    return parts


def unescape_wifi(value):
    """Decode the backslash escaping used by the Wi-Fi QR convention."""
    result = []
    escaped = False
    for char in value:
        if escaped:
            result.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            result.append(char)
    if escaped:
        result.append("\\")
    return "".join(result)


def parse_wifi_qr(payload):
    """Return (ssid, password, auth, hidden) or raise ValueError."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if not isinstance(payload, str) or not payload.startswith("WIFI:"):
        raise ValueError("Not a Wi-Fi QR code")

    fields = {}
    for field in split_unescaped(payload[5:], ";"):
        if not field:
            continue
        pair = split_unescaped(field, ":")
        if len(pair) < 2:
            continue
        key = unescape_wifi(pair[0]).upper()
        fields[key] = unescape_wifi(":".join(pair[1:]))

    ssid = fields.get("S", "")
    password = fields.get("P", "")
    auth = fields.get("T", "nopass").upper()
    hidden_value = fields.get("H", "false").lower()

    if not ssid:
        raise ValueError("The QR code has no SSID")
    if len(ssid.encode("utf-8")) > 32:
        raise ValueError("SSID is longer than 32 bytes")
    if any(ord(char) < 32 or ord(char) == 127 for char in ssid):
        raise ValueError("SSID contains control characters")
    if "EAP" in auth:
        raise ValueError("Enterprise Wi-Fi is not supported")
    if auth in ("", "NOPASS"):
        auth = "nopass"
        password = ""
    elif auth not in ("WPA", "WPA2", "WPA3", "SAE", "WPA/WPA2", "WPA2/WPA3", "WEP"):
        raise ValueError("Unsupported Wi-Fi security: %s" % auth)
    else:
        if not password:
            raise ValueError("The secured network has no password")
        if any(ord(char) < 32 or ord(char) == 127 for char in password):
            raise ValueError("Password contains control characters")
        password_length = len(password.encode("utf-8"))
        if password_length > 64:
            raise ValueError("Password is longer than 64 bytes")
        if auth != "WEP" and password_length < 8:
            raise ValueError("WPA password is shorter than 8 bytes")

    if hidden_value not in ("true", "false", "1", "0", "yes", "no"):
        raise ValueError("Hidden flag must be true or false")
    hidden = hidden_value in ("true", "1", "yes")
    return ssid, password, auth, hidden


def tap():
    """Return the tap position once, or None."""
    if M5.Touch.getCount() == 0:
        return None
    if M5.Touch.getDetail(0)[6]:
        return (M5.Touch.getX(), M5.Touch.getY())
    return None


def display_text(value):
    """Remove control characters before drawing untrusted QR text."""
    return "".join(char if ord(char) >= 32 else " " for char in str(value))


def fit_text(value, font, max_width):
    """Fit one line to the LCD, adding an ellipsis when needed."""
    value = display_text(value)
    M5.Lcd.setFont(font)
    try:
        if M5.Lcd.textWidth(value) <= max_width:
            return value
        suffix = "..."
        while value and M5.Lcd.textWidth(value + suffix) > max_width:
            value = value[:-1]
        return value + suffix
    except Exception:
        return value[:28]


def draw_message(title, detail="", color=FG, footer="Tap to retry - Power exits"):
    M5.Lcd.fillScreen(BG)
    title_font = M5.Lcd.FONTS.Montserrat24
    detail_font = M5.Lcd.FONTS.Montserrat18
    footer_font = M5.Lcd.FONTS.Montserrat14

    title = fit_text(title, title_font, 296)
    M5.Lcd.setTextColor(color, BG)
    M5.Lcd.drawCenterString(title, 160, 70)
    if detail:
        detail = fit_text(detail, detail_font, 296)
        M5.Lcd.setTextColor(FG, BG)
        M5.Lcd.drawCenterString(detail, 160, 112)
    if footer:
        footer = fit_text(footer, footer_font, 304)
        M5.Lcd.setTextColor(DIM, BG)
        M5.Lcd.drawCenterString(footer, 160, 207)


def draw_connected(ssid, ip_address):
    draw_message("Connected and saved", ssid, GREEN, "")
    font = M5.Lcd.FONTS.Montserrat14
    ip_text = fit_text("IP %s" % ip_address, font, 296)
    M5.Lcd.setTextColor(DIM, BG)
    M5.Lcd.drawCenterString(ip_text, 160, 150)

    M5.Lcd.fillRoundRect(12, 190, 142, 40, 8, BLUE)
    M5.Lcd.setFont(font)
    M5.Lcd.setTextColor(FG, BLUE)
    M5.Lcd.drawCenterString("SCAN AGAIN", 83, 201)

    M5.Lcd.fillRoundRect(166, 190, 142, 40, 8, GREEN)
    M5.Lcd.setTextColor(BG, GREEN)
    M5.Lcd.drawCenterString("EXIT", 237, 201)


def draw_confirmation(ssid, auth, hidden):
    security = "Open" if auth == "nopass" else auth
    if hidden:
        security += " / hidden"
    draw_message("Switch Wi-Fi?", ssid, BLUE, "")
    font = M5.Lcd.FONTS.Montserrat14
    security = fit_text(security, font, 296)
    M5.Lcd.setTextColor(DIM, BG)
    M5.Lcd.drawCenterString(security, 160, 150)

    M5.Lcd.fillRoundRect(12, 190, 142, 40, 8, 0x45515A)
    M5.Lcd.setFont(font)
    M5.Lcd.setTextColor(FG, 0x45515A)
    M5.Lcd.drawCenterString("CANCEL", 83, 201)

    M5.Lcd.fillRoundRect(166, 190, 142, 40, 8, GREEN)
    M5.Lcd.setTextColor(BG, GREEN)
    M5.Lcd.drawCenterString("CONNECT", 237, 201)


def stop_camera():
    global camera_active

    if not camera_active:
        return
    try:
        camera.deinit()
    except Exception as exc:
        print("wifi_qr: camera cleanup warning: %r" % exc)
    camera_active = False


def start_camera():
    global camera_active, scan_frames, scan_log_at, state

    stop_camera()
    time.sleep_ms(100)
    draw_message("Starting camera...", footer="Power button exits")
    try:
        print("wifi_qr: starting camera")
        camera.init(pixformat=camera.RGB565, framesize=camera.QVGA)
        camera_active = True
        scan_frames = 0
        scan_log_at = time.ticks_ms()
        state = STATE_SCAN
        print("wifi_qr: camera ready, scanning")
    except Exception as exc:
        state = STATE_ERROR
        draw_message("Camera error", str(exc), RED)
        print("wifi_qr: camera error: %r" % exc)


def read_uiflow_credentials():
    """Read UIFlow2's current default without exposing the password in logs."""
    settings = esp32.NVS("uiflow")
    try:
        ssid = settings.get_str("ssid0")
    except OSError:
        ssid = ""
    try:
        password = settings.get_str("pswd0")
    except OSError:
        password = ""
    try:
        net_mode = settings.get_str("net_mode")
    except OSError:
        net_mode = "WIFI"
    return ssid, password, net_mode


def write_uiflow_credentials(ssid, password, net_mode="WIFI"):
    """Commit and verify the exact NVS keys used by UIFlow2 2.5.1."""
    settings = esp32.NVS("uiflow")
    settings.set_str("net_mode", net_mode)
    settings.set_str("ssid0", ssid)
    settings.set_str("pswd0", password)
    settings.commit()
    if settings.get_str("ssid0") != ssid or settings.get_str("pswd0") != password:
        raise OSError("UIFlow2 settings verification failed")


def save_uiflow_credentials(ssid, password):
    write_uiflow_credentials(ssid, password, "WIFI")
    print("wifi_qr: saved %r as UIFlow2's default network" % ssid)


def decoded_ssid(raw_ssid):
    if isinstance(raw_ssid, bytes):
        try:
            return raw_ssid.decode("utf-8")
        except Exception:
            return ""
    return str(raw_ssid)


def scan_for_network(station, target_ssid, hidden):
    """Return whether a non-hidden target is visible, or None if unknown."""
    if hidden:
        print("wifi_qr: hidden network; skipping visibility preflight")
        return None
    print("wifi_qr: checking for target on the CoreS3 2.4 GHz radio")
    try:
        networks = station.scan()
    except Exception as exc:
        print("wifi_qr: visibility scan unavailable: %r" % exc)
        return None

    best = None
    for item in networks:
        if decoded_ssid(item[0]) != target_ssid:
            continue
        if best is None or item[3] > best[0]:
            best = (item[3], item[2])

    if best is None:
        print("wifi_qr: target %r not visible among %d network(s)" % (target_ssid, len(networks)))
        return False
    print("wifi_qr: target visible channel=%s rssi=%s" % (best[1], best[0]))
    return True


def station_status_message(status, target_visible):
    no_ap_statuses = (
        getattr(network, "STAT_NO_AP_FOUND", 201),
        getattr(network, "STAT_NO_AP_FOUND_IN_RSSI_THRESHOLD", -101),
        getattr(network, "STAT_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD", -102),
        getattr(network, "STAT_NO_AP_FOUND_W_COMPATIBLE_SECURITY", -103),
    )
    if status in no_ap_statuses or target_visible is False:
        return "Network not found - use 2.4 GHz"
    messages = {
        getattr(network, "STAT_WRONG_PASSWORD", 202): "Wrong password",
        getattr(network, "STAT_ASSOC_FAIL", 203): "Association failed",
        getattr(network, "STAT_HANDSHAKE_TIMEOUT", 204): "Handshake timed out",
        getattr(network, "STAT_BEACON_TIMEOUT", -104): "Access point stopped responding",
    }
    return messages.get(status, "Connection timed out (status %s)" % status)


def terminal_statuses():
    return (
        getattr(network, "STAT_NO_AP_FOUND", 201),
        getattr(network, "STAT_NO_AP_FOUND_IN_RSSI_THRESHOLD", -101),
        getattr(network, "STAT_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD", -102),
        getattr(network, "STAT_NO_AP_FOUND_W_COMPATIBLE_SECURITY", -103),
        getattr(network, "STAT_WRONG_PASSWORD", 202),
        getattr(network, "STAT_ASSOC_FAIL", 203),
        getattr(network, "STAT_HANDSHAKE_TIMEOUT", 204),
        getattr(network, "STAT_BEACON_TIMEOUT", -104),
    )


def is_non_retryable_status(status):
    return status in (
        getattr(network, "STAT_WRONG_PASSWORD", 202),
        getattr(network, "STAT_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD", -102),
        getattr(network, "STAT_NO_AP_FOUND_W_COMPATIBLE_SECURITY", -103),
    )


def reset_station(station):
    """Clear UIFlow's previous station state before scanning or connecting."""
    try:
        station.disconnect()
    except Exception:
        pass
    station.active(False)
    time.sleep_ms(500)
    station.active(True)
    time.sleep_ms(800)


def wait_for_connection(station, allow_exit=True):
    """Return the last status when connected, terminal, or timed out."""
    deadline = time.ticks_add(time.ticks_ms(), CONNECT_TIMEOUT_MS)
    terminal_at = None
    last_status = None
    terminals = terminal_statuses()

    while not station.isconnected():
        M5.update()
        if allow_exit and BtnPWR.wasClicked():
            raise ExitRequested
        status = station.status()
        if status != last_status:
            print("wifi_qr: station status=%s" % status)
            last_status = status
            terminal_at = time.ticks_ms() if status in terminals else None
        elif (
            terminal_at is not None
            and time.ticks_diff(time.ticks_ms(), terminal_at) >= STATUS_GRACE_MS
        ):
            return status
        if time.ticks_diff(deadline, time.ticks_ms()) < 0:
            return status
        time.sleep_ms(100)
    return station.status()


def restore_previous_network(station, previous):
    """Best-effort rollback after a new network fails validation or saving."""
    previous_ssid, previous_password, _previous_mode = previous
    if not previous_ssid:
        print("wifi_qr: no previous Wi-Fi network to restore")
        return False
    print("wifi_qr: restoring previous network %r" % previous_ssid)
    try:
        reset_station(station)
        if previous_password:
            station.connect(previous_ssid, previous_password)
        else:
            station.connect(previous_ssid)
        wait_for_connection(station, allow_exit=False)
        restored = station.isconnected()
        print("wifi_qr: previous network restored=%s" % restored)
        return restored
    except Exception as exc:
        print("wifi_qr: previous network restore failed: %r" % exc)
        return False


def connect_wifi(ssid, password, auth, hidden):
    """Test the scanned credentials, then make them UIFlow2's default."""
    global state

    stop_camera()
    draw_message("Preparing Wi-Fi...", ssid, BLUE, "Power button exits")
    station = network.WLAN(network.STA_IF)
    previous = read_uiflow_credentials()
    last_status = None
    target_visible = None

    try:
        reset_station(station)
        target_visible = scan_for_network(station, ssid, hidden)

        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            draw_message(
                "Connecting...",
                ssid,
                BLUE,
                "Attempt %d of %d" % (attempt, CONNECT_ATTEMPTS),
            )
            print("wifi_qr: connect attempt %d/%d" % (attempt, CONNECT_ATTEMPTS))
            try:
                if password:
                    station.connect(ssid, password)
                else:
                    station.connect(ssid)
                last_status = wait_for_connection(station)
            except OSError as exc:
                print("wifi_qr: connect call failed: %r" % exc)
                last_status = station.status()

            if station.isconnected():
                break
            if is_non_retryable_status(last_status):
                print("wifi_qr: terminal credential/security failure; not retrying")
                break
            if attempt < CONNECT_ATTEMPTS:
                print("wifi_qr: retrying after status=%s" % last_status)
                reset_station(station)

        if not station.isconnected():
            raise OSError(station_status_message(last_status, target_visible))

        ip_address = station.ifconfig()[0]
        try:
            save_uiflow_credentials(ssid, password)
        except Exception as exc:
            print("wifi_qr: connected but UIFlow2 save failed: %r" % exc)
            try:
                write_uiflow_credentials(*previous)
            except Exception as rollback_exc:
                print("wifi_qr: settings rollback failed: %r" % rollback_exc)
            raise OSError("Connected, but UIFlow2 settings were not saved")

        state = STATE_CONNECTED
        draw_connected(ssid, ip_address)
        print("wifi_qr: connected ssid=%r ip=%s" % (ssid, ip_address))
    except ExitRequested:
        restore_previous_network(station, previous)
        exit_app()
    except Exception as exc:
        restored = restore_previous_network(station, previous)
        state = STATE_ERROR
        footer = "Previous Wi-Fi restored" if restored else "Tap to retry - Power exits"
        draw_message("Connection failed", str(exc), RED, footer)
        print("wifi_qr: connection failed for ssid=%r: %r" % (ssid, exc))


def find_wifi_qr(img):
    """Return the first valid Wi-Fi QR tuple found in an image, if any."""
    found_non_wifi = False
    for result in img.find_qrcodes() or ():
        try:
            wifi = parse_wifi_qr(result.payload())
            print("wifi_qr: Wi-Fi QR ssid=%r auth=%r hidden=%s" % (wifi[0], wifi[2], wifi[3]))
            return wifi, False
        except Exception as exc:
            found_non_wifi = True
            print("wifi_qr: ignored QR candidate: %s" % exc)
    return None, found_non_wifi


def scan_frame():
    global scan_frames, scan_log_at

    img = camera.snapshot()
    scan_frames += 1
    now = time.ticks_ms()
    if time.ticks_diff(now, scan_log_at) >= SCAN_LOG_INTERVAL_MS:
        print("wifi_qr: scanning, frames=%d" % scan_frames)
        scan_log_at = now

    wifi, found_non_wifi = find_wifi_qr(img)
    if wifi:
        img.draw_string(8, 8, "Wi-Fi QR found", color=GREEN, scale=1.5)
    elif found_non_wifi:
        img.draw_string(8, 8, "Not a Wi-Fi QR", color=RED, scale=1.5)
    else:
        img.draw_string(8, 8, "Scan a Wi-Fi QR code", color=FG, scale=1.5)
    M5.Lcd.show(img, 0, 0, 320, 240)
    if wifi:
        show_confirmation(wifi)


def show_confirmation(wifi):
    global pending_wifi, state

    stop_camera()
    pending_wifi = wifi
    state = STATE_CONFIRM
    draw_confirmation(wifi[0], wifi[2], wifi[3])
    print("wifi_qr: waiting for CONNECT or CANCEL confirmation")


def exit_app():
    """Leave Wi-Fi associated and restart into UIFlow2's launcher."""
    stop_camera()
    print("wifi_qr: exit requested; restarting into the UIFlow2 launcher")
    try:
        settings = esp32.NVS("uiflow")
        settings.set_u8("boot_option", 1)
        settings.commit()
    except Exception as exc:
        print("wifi_qr: could not select UIFlow2 launcher: %r" % exc)
    draw_message("Exiting", "Restarting UIFlow2...", GREEN, "")
    time.sleep_ms(300)
    machine.reset()


def setup():
    M5.begin()
    Widgets.setRotation(1)
    Widgets.fillScreen(BG)
    start_camera()


def loop():
    global pending_wifi

    M5.update()
    if BtnPWR.wasClicked():
        exit_app()
        return
    if state == STATE_SCAN and camera_active:
        scan_frame()
        return

    position = tap()
    if not position:
        return
    if state == STATE_CONFIRM:
        if position[0] < 160:
            print("wifi_qr: switch cancelled")
            pending_wifi = None
            start_camera()
        else:
            wifi = pending_wifi
            pending_wifi = None
            if wifi is not None:
                connect_wifi(*wifi)
        return
    if state == STATE_CONNECTED and position[0] >= 160:
        exit_app()
        return
    start_camera()


def run():
    global state

    try:
        setup()
        while True:
            try:
                loop()
            except KeyboardInterrupt:
                stop_camera()
                raise
            except Exception as exc:
                stop_camera()
                state = STATE_ERROR
                print("wifi_qr: runtime error: %r" % exc)
                draw_message("App error", str(exc), RED)
            time.sleep_ms(20)
    except KeyboardInterrupt:
        stop_camera()
        raise
    except Exception as exc:
        stop_camera()
        print("wifi_qr: setup error: %r" % exc)
        try:
            draw_message("Setup failed", str(exc), RED, "Power button exits")
        except Exception:
            pass
    finally:
        stop_camera()


run()
