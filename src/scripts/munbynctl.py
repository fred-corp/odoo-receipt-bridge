#!/usr/bin/env python3
"""munbynctl.py - configure and test a MUNBYN ESC/POS receipt printer.

The script sends ESC/POS commands and MUNBYN vendor commands to a receipt
printer. It works over LAN, direct USB, a raw device file, CUPS, or a
Windows print queue. It needs no third-party module for the LAN, device,
CUPS, and Windows transports. Install pyusb for direct USB access:

    pip install pyusb

Put the connection option before the command:

    python munbynctl.py --net 192.168.1.87 status 4
    python munbynctl.py --net 192.168.1.87 cut --feed 3
    python munbynctl.py --net 192.168.1.87 density 6
    python munbynctl.py --net 192.168.1.87 test
    python munbynctl.py --dev /dev/usb/lp0 feed 3
    python munbynctl.py --usb 0483:57241 factory-reset
    python munbynctl.py --net 192.168.1.87 qr "https://example.com"
    python munbynctl.py --net 192.168.1.87 image logo.png
    python munbynctl.py --net 192.168.1.87 graytest
    python munbynctl.py tui

The tui command opens a full-screen terminal interface. Up/Down selects an
action, Enter runs it, and Left/Right changes the parameter of the
selected row. The interface uses curses, which is in the Python standard
library. On Windows, run: pip install windows-curses

Graphics: the print head prints one bit per dot, so grayscale content is
rendered with Floyd-Steinberg dithering. QR printing needs the qrcode or
segno package. Image printing needs Pillow. The graytest command builds
the test card itself and needs no package.

With no connection option, the script auto-detects the printer. It tries
the Linux usblp device file first, then a USB printer-class device with
pyusb.

Use --dry-run to print the byte sequence without a printer.

Notes:
- ESC/POS formatting commands (align, bold, spacing) are session state.
  The printer clears them when it receives init or when it powers off.
- MUNBYN vendor commands (density, media width, network settings) are
  stored in the printer and survive a power cycle.
- If the printer does not cut, enable the cutter first. Some models use a
  DIP switch. Other models need the MUNBYN setting tool.
"""

import argparse
import os
import re
import socket
import subprocess
import sys
import textwrap
import time

ESC = b"\x1b"
GS = b"\x1d"
DLE = b"\x10"
EOT = b"\x04"

DEFAULT_NET_PORT = 9100

# ESC t table number -> Python codec (Epson ESC/POS code tables).
CODEPAGES = {
    "pc437": (0, "cp437"),
    "katakana": (1, "cp437"),
    "pc850": (2, "cp850"),
    "pc860": (3, "cp860"),
    "pc863": (4, "cp863"),
    "pc865": (5, "cp865"),
    "pc851": (6, "cp851"),
    "pc853": (7, "cp853"),
    "wpc1252": (16, "cp1252"),
    "pc852": (17, "cp852"),
    "pc858": (18, "cp858"),
}

# MUNBYN print width table for command 1F 1B 1F E1 13 14 n.
# The vendor table lists the value 5 twice. The script maps 64 mm to 6.
MEDIA_WIDTHS = {
    0: "72 mm",
    1: "76 mm",
    2: "80 mm",
    3: "48 mm",
    4: "52 mm",
    5: "56 mm",
    6: "64 mm",
    7: "68 mm",
    8: "54 mm",
}


def die(message):
    sys.exit("Error: " + message)


def byte(value):
    """Return one byte for a value in the range 0-255."""
    value = int(value)
    if not 0 <= value <= 255:
        die("value %d is out of range 0-255" % value)
    return bytes([value])


def word(value):
    """Return a low-high byte pair for a value in the range 0-65535."""
    value = int(value)
    if not 0 <= value <= 65535:
        die("value %d is out of range 0-65535" % value)
    return bytes([value & 0xFF, value >> 8])


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

class Connection:
    """Base class for a printer link. read() returns empty when the link
    is write-only."""

    def send(self, data):
        raise NotImplementedError

    def read(self, size, timeout):
        return b""

    def close(self):
        pass


class NetworkConnection(Connection):
    """Raw TCP channel to a LAN printer. The default port is 9100."""

    def __init__(self, host, port, timeout):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)

    def send(self, data):
        self.sock.sendall(data)

    def read(self, size, timeout):
        self.sock.settimeout(timeout)
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class FileConnection(Connection):
    """Raw device file, for example /dev/usb/lp0 from the Linux usblp
    module."""

    def __init__(self, path):
        self.fh = open(path, "r+b", buffering=0)

    def send(self, data):
        self.fh.write(data)

    def read(self, size, timeout):
        try:
            import select
            ready, _, _ = select.select([self.fh], [], [], timeout)
            if not ready:
                return b""
            return os.read(self.fh.fileno(), size)
        except (OSError, ValueError, ImportError):
            return b""

    def close(self):
        self.fh.close()


class CupsConnection(Connection):
    """CUPS queue. The script sends the payload with lp -o raw, so CUPS
    passes the bytes through without a driver filter."""

    def __init__(self, printer):
        self.printer = printer

    def send(self, data):
        subprocess.run(
            ["lp", "-d", self.printer, "-o", "raw"],
            input=data, check=True,
        )


class UsbConnection(Connection):
    """Direct USB access with pyusb. The constructor finds a device with
    a USB printer-class interface (class 0x07) and claims it."""

    def __init__(self, vid, pid):
        try:
            import usb.core
            import usb.util
        except ImportError as exc:
            raise RuntimeError(
                "direct USB needs pyusb. Run: pip install pyusb") from exc
        self._core = usb.core
        self._util = usb.util
        criteria = {}
        if vid is not None:
            criteria["idVendor"] = vid
        if pid is not None:
            criteria["idProduct"] = pid
        matches = []
        for dev in usb.core.find(find_all=True, **criteria):
            ift = self._find_printer_interface(dev)
            if ift is not None:
                matches.append((dev, ift))
        if not matches:
            raise RuntimeError("no USB printer-class device was found")
        # Prefer a bidirectional interface (protocol 2).
        matches.sort(key=lambda pair: 0 if pair[1].bInterfaceProtocol == 2 else 1)
        self.dev, self.ift = matches[0]
        number = self.ift.bInterfaceNumber
        try:
            if self.dev.is_kernel_driver_active(number):
                self.dev.detach_kernel_driver(number)
        except (AttributeError, NotImplementedError, usb.core.USBError):
            pass
        try:
            usb.util.claim_interface(self.dev, self.ift)
        except usb.core.USBError as exc:
            text = str(exc).lower()
            if "busy" in text or "resource" in text:
                raise RuntimeError(
                    "the device is busy. Linux usblp may own it. "
                    "Use --dev /dev/usb/lp0, or unload the usblp module."
                ) from exc
            raise RuntimeError(
                "USB access was denied. Run with sudo, or add a udev rule "
                "for the printer.") from exc
        util = usb.util
        self.ep_out = util.find_descriptor(
            self.ift,
            custom_match=lambda e: util.endpoint_direction(e.bEndpointAddress) == util.ENDPOINT_OUT)
        self.ep_in = util.find_descriptor(
            self.ift,
            custom_match=lambda e: util.endpoint_direction(e.bEndpointAddress) == util.ENDPOINT_IN)
        if self.ep_out is None:
            raise RuntimeError("the printer interface has no bulk-out endpoint")
        if vid is None:
            sys.stderr.write(
                "Auto: USB device %04X:%04X.\n"
                % (self.dev.idVendor, self.dev.idProduct))

    def _find_printer_interface(self, dev):
        core = self._core
        try:
            cfg = dev.get_active_configuration()
        except (core.USBError, ValueError):
            try:
                dev.set_configuration()
            except (core.USBError, ValueError):
                return None
            try:
                cfg = dev.get_active_configuration()
            except (core.USBError, ValueError):
                return None
        best = None
        for ift in cfg:
            if ift.bInterfaceClass == 0x07:
                if best is None or ift.bInterfaceProtocol == 2:
                    best = ift
        return best

    def send(self, data):
        try:
            self.dev.write(self.ep_out.bEndpointAddress, data, 8000)
        except self._core.USBError as exc:
            raise RuntimeError("USB write failed: %s" % exc) from exc

    def read(self, size, timeout):
        if self.ep_in is None:
            return b""
        try:
            data = self.dev.read(self.ep_in.bEndpointAddress, size, int(timeout * 1000))
        except self._core.USBError:
            return b""
        return bytes(data)

    def close(self):
        try:
            self._util.release_interface(self.dev, self.ift.bInterfaceNumber)
        except Exception:
            pass
        self._util.dispose_resources(self.dev)


class WindowsPrinterConnection(Connection):
    """Windows print queue in raw mode. It writes through winspool.drv
    with no driver filter."""

    def __init__(self, printer_name):
        if sys.platform != "win32":
            raise RuntimeError("the --win option needs Windows")
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._wintypes = wintypes
        winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
        winspool.OpenPrinterW.argtypes = [
            wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
        winspool.OpenPrinterW.restype = wintypes.BOOL
        winspool.StartDocPrinterW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
        winspool.StartDocPrinterW.restype = wintypes.DWORD
        winspool.StartPagePrinter.argtypes = [wintypes.HANDLE]
        winspool.StartPagePrinter.restype = wintypes.BOOL
        winspool.WritePrinter.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        winspool.WritePrinter.restype = wintypes.BOOL
        winspool.EndPagePrinter.argtypes = [wintypes.HANDLE]
        winspool.EndPagePrinter.restype = wintypes.BOOL
        winspool.EndDocPrinter.argtypes = [wintypes.HANDLE]
        winspool.EndDocPrinter.restype = wintypes.BOOL
        winspool.ClosePrinter.argtypes = [wintypes.HANDLE]
        winspool.ClosePrinter.restype = wintypes.BOOL
        handle = wintypes.HANDLE()
        if not winspool.OpenPrinterW(printer_name, ctypes.byref(handle), None):
            raise RuntimeError(
                "Windows cannot open the printer queue %r" % printer_name)
        self.handle = handle.value
        self.winspool = winspool

    def send(self, data):
        ctypes = self._ctypes
        wintypes = self._wintypes

        class DOC_INFO_1W(ctypes.Structure):
            _fields_ = [
                ("pDocName", wintypes.LPWSTR),
                ("pOutputFile", wintypes.LPWSTR),
                ("pDatatype", wintypes.LPWSTR),
            ]

        doc = DOC_INFO_1W("munbynctl", None, "RAW")
        if not self.winspool.StartDocPrinterW(self.handle, 1, ctypes.byref(doc)):
            raise RuntimeError("StartDocPrinter failed")
        self.winspool.StartPagePrinter(self.handle)
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        written = wintypes.DWORD(0)
        ok = self.winspool.WritePrinter(
            self.handle, ctypes.cast(buf, ctypes.c_void_p), len(data),
            ctypes.byref(written))
        self.winspool.EndPagePrinter(self.handle)
        self.winspool.EndDocPrinter(self.handle)
        if not ok:
            raise RuntimeError("WritePrinter failed")

    def close(self):
        if getattr(self, "handle", None):
            self.winspool.ClosePrinter(self.handle)
            self.handle = None


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------

def cmd_init():
    """ESC @: clear the print buffer and reset the session settings."""
    return ESC + b"@"


def cmd_feed(lines):
    """ESC d n: print the buffer and feed n lines."""
    return ESC + b"d" + byte(lines)


def cmd_feed_dots(dots):
    """ESC J n: print the buffer and feed n dots."""
    return ESC + b"J" + byte(dots)


def cmd_cut(mode="partial", feed=0):
    """GS V: cut the paper. mode is full or partial. feed is the number
    of motion units to feed before the cut.

    The default output, 1D 56 42 00, is the cut command that MUNBYN
    documents for its receipt printers."""
    m = 0 if mode == "full" else 1
    return GS + b"V" + bytes([65 + m]) + byte(feed)


def cmd_spacing(dots=None):
    """ESC 3 n: line spacing n/144 inch. ESC 2: reset to 1/6 inch."""
    if dots is None:
        return ESC + b"2"
    return ESC + b"3" + byte(dots)


def cmd_align(mode):
    """ESC a n: left, center, or right alignment. mode accepts a name
    or a number 0-2."""
    table = {"left": 0, "center": 1, "right": 2, 0: 0, 1: 1, 2: 2}
    if mode not in table:
        die("alignment must be left, center, right, 0, 1, or 2")
    return ESC + b"a" + byte(table[mode])


def cmd_bold(state):
    """ESC E n: turn bold on or off."""
    return ESC + b"E" + byte(1 if state == "on" else 0)


def cmd_underline(state):
    """ESC - n: turn underline off, single, or double."""
    return ESC + b"-" + byte({"off": 0, "single": 1, "double": 2}[state])


def cmd_codepage(table):
    """ESC t n: select the code table for the text that follows."""
    return ESC + b"t" + byte(table)


def cmd_beep(count, duration):
    """ESC B n t: sound the buzzer. Support depends on the model."""
    return ESC + b"B" + byte(count) + byte(duration)


def cmd_drawer(pin, on, off):
    """ESC p m t1 t2: kick the cash drawer. Times are in 2 ms units.
    The defaults match the MUNBYN example 1B 70 00 1E FF."""
    return ESC + b"p" + byte(pin) + byte(on) + byte(off)


def cmd_button(state):
    """ESC c 5 n: enable or disable the FEED button."""
    return ESC + b"c" + b"5" + byte(0 if state == "on" else 1)


def cmd_margin(dots):
    """GS L: left margin in dots."""
    return GS + b"L" + word(dots)


def cmd_print_area(dots):
    """GS W: print area width in dots."""
    return GS + b"W" + word(dots)


def cmd_status(slot):
    """DLE EOT n: request one real-time status byte."""
    return DLE + EOT + byte(slot)


# --- MUNBYN vendor commands, documented for ITPP047 and ITPP068. ---

def cmd_density(level):
    """1F 1B 1F 13 14 n: print density. 1 is light, 8 is dark."""
    if not 1 <= level <= 8:
        die("density must be in the range 1-8")
    return b"\x1f\x1b\x1f\x13\x14" + byte(level)


def cmd_media_width(index):
    """1F 1B 1F E1 13 14 n: print content width. See MEDIA_WIDTHS."""
    if index not in MEDIA_WIDTHS:
        die("width index must be in the range 0-8")
    return b"\x1f\x1b\x1f\xe1\x13\x14" + byte(index)


def cmd_factory_reset():
    """Restore the factory settings."""
    return b"\x1f\x1b\x1f\x11\x11\x00"


def cmd_dhcp_always():
    """Always enable DHCP on the network interface."""
    return b"\x1f\x1b\x10\x13\x14\x00"


def cmd_wifi_receipt(state):
    """Print the Wi-Fi configuration receipt at startup. 0 is on, 1 is
    off."""
    return b"\x1f\x1b\x1f\x28\x13\x14\x00" + byte(0 if state == "on" else 1)


def cmd_wifi_dhcp(state):
    """DHCP on the Wi-Fi interface. 0 is on, 1 is off."""
    return b"\x1f\x1b\x1f\x28\x13\x14\x04" + byte(0 if state == "on" else 1)


def cmd_wifi_reset():
    """Reset the Wi-Fi module."""
    return b"\x1f\x1b\x1f\x27\x13\x14\x52\x00"


# ---------------------------------------------------------------------------
# Graphics: raster images, QR codes, and dithering
# ---------------------------------------------------------------------------

def _pack_bits(bits, row_bytes):
    """Return one packed raster row from a list of 0/1 bits. The first
    bit is the highest bit of the first byte."""
    row = bytearray(row_bytes)
    for i, bit in enumerate(bits[:row_bytes * 8]):
        if bit:
            row[i >> 3] |= 0x80 >> (i & 7)
    return bytes(row)


def _raster_payload(rows, row_bytes):
    """Return a raster bit image payload. The payload sends one GS v 0
    command per line. Clone firmware buffers handle this well."""
    parts = []
    for row in rows:
        parts.append(b"\x1d\x76\x30\x00")
        parts.append(bytes([row_bytes & 0xFF, row_bytes >> 8, 1, 0]))
        parts.append(row)
    return b"".join(parts)


def _dither_rows(gray, width):
    """Return 0/1 bit rows from grayscale rows with Floyd-Steinberg
    dithering. gray is a list of rows with values 0-255."""
    height = len(gray)
    lines = [list(row) for row in gray]
    bits = []
    for y in range(height):
        current = lines[y]
        below = lines[y + 1] if y + 1 < height else None
        row_bits = []
        for x in range(width):
            value = current[x]
            bit = 1 if value >= 128 else 0
            row_bits.append(bit)
            error = value - (255 if bit else 0)
            if x + 1 < width:
                current[x + 1] += error * 7 // 16
            if below is not None:
                if x > 0:
                    below[x - 1] += error * 3 // 16
                below[x] += error * 5 // 16
                if x + 1 < width:
                    below[x + 1] += error // 16
        bits.append(row_bits)
    return bits


def _qr_matrix(text, ecc):
    """Return a QR code matrix as rows of 0/1, quiet zone included. Try
    the qrcode package first, then segno."""
    try:
        import qrcode
    except ImportError:
        pass
    else:
        levels = {"L": qrcode.constants.ERROR_CORRECT_L,
                  "M": qrcode.constants.ERROR_CORRECT_M,
                  "Q": qrcode.constants.ERROR_CORRECT_Q,
                  "H": qrcode.constants.ERROR_CORRECT_H}
        qr = qrcode.QRCode(
            error_correction=levels.get(ecc.upper(),
                                        qrcode.constants.ERROR_CORRECT_M),
            box_size=1, border=4)
        qr.add_data(text)
        qr.make(fit=True)
        return [[1 if v else 0 for v in row] for row in qr.get_matrix()]
    try:
        import segno
    except ImportError:
        raise RuntimeError("QR printing needs the qrcode or segno package. "
                           "Run: pip install qrcode")
    qr = segno.make(text, error=ecc.lower())
    inner = [[1 if v else 0 for v in row] for row in qr.matrix]
    quiet = 4
    size = len(inner)
    blank = [[0] * (size + 2 * quiet) for _ in range(quiet)]
    matrix = [row[:] for row in blank]
    for row in inner:
        matrix.append([0] * quiet + row + [0] * quiet)
    matrix.extend([row[:] for row in blank])
    return matrix


def qr_payload(text, scale=3, ecc="M", paper=512):
    """Return a raster payload with a centered QR code. scale is the
    module size in dots, paper is the printable width in dots."""
    if not 1 <= scale <= 16:
        raise ValueError("QR scale must be in the range 1-16")
    matrix = _qr_matrix(text, ecc)
    total = len(matrix) * scale
    left = max(0, (paper - total) // 2 // 8 * 8)
    row_bytes = (left + total + 7) // 8
    rows = []
    for mrow in matrix:
        bits = [0] * left
        for value in mrow:
            bits.extend([value] * scale)
        bits.extend([0] * (row_bytes * 8 - len(bits)))
        packed = _pack_bits(bits, row_bytes)
        for _ in range(scale):
            rows.append(packed)
    return _raster_payload(rows, row_bytes)


def image_payload(path, width=512, invert=False, fit=False, threshold=None):
    """Return a raster payload for an image file. The image is
    converted to grayscale, scaled to the print width, and dithered to
    one bit per dot with Floyd-Steinberg. fit scales small images up.
    threshold prints line art without dithering. Needs Pillow."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise RuntimeError("image printing needs Pillow. "
                           "Run: pip install pillow")
    img = Image.open(path)
    img = img.convert("L")
    if invert:
        img = ImageOps.invert(img)
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    if img.width > width:
        new_height = max(1, round(img.height * width / img.width))
        img = img.resize((width, new_height), resampling)
    elif fit and img.width < width:
        new_height = max(1, round(img.height * width / img.width))
        img = img.resize((width, new_height), resampling)
    if threshold is not None:
        if not 0 <= threshold <= 255:
            raise ValueError("threshold must be in the range 0-255")
        img = img.point(lambda v: 255 if v >= threshold else 0)
        no_dither = getattr(getattr(Image, "Dither", Image), "NONE")
        img = img.convert("1", dither=no_dither)
    else:
        dither = getattr(getattr(Image, "Dither", Image), "FLOYDSTEINBERG")
        img = img.convert("1", dither=dither)
    w, h = img.size
    row_bytes = (w + 7) // 8
    data = img.tobytes("raw", "1")
    rows = [data[y * row_bytes:y * row_bytes + row_bytes] for y in range(h)]
    return _raster_payload(rows, row_bytes)


def gray_test_payload(width=512):
    """Return a grayscale test card: a smooth dithered gradient, 16
    step bands, and a fine checkerboard. This command needs no
    third-party module."""
    if not 64 <= width <= 1024:
        raise ValueError("test card width must be in the range 64-1024")
    gray = []
    for _ in range(40):
        gray.append([255 * x // max(1, width - 1) for x in range(width)])
    band = max(1, width // 16)
    for _ in range(40):
        gray.append([min(15, x // band) * 17 for x in range(width)])
    for y in range(16):
        gray.append([0 if ((x // 4 + y // 4) % 2 == 0) else 255
                     for x in range(width)])
    row_bytes = (width + 7) // 8
    rows = [_pack_bits(r, row_bytes) for r in _dither_rows(gray, width)]
    return b"".join([
        cmd_align(1),
        b"GRAYSCALE TEST CARD\n",
        cmd_align(0),
        _raster_payload(rows, row_bytes),
        cmd_feed(3),
    ])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_hex(text):
    """Parse a hex sequence. Spaces, commas, and colons are allowed.
    Raise ValueError when the input is not a valid sequence."""
    text = text.replace("0x", "").replace("0X", "")
    digits = re.sub(r"[^0-9A-Fa-f]", "", text)
    if not digits:
        raise ValueError("hex command has no digits")
    if len(digits) % 2:
        raise ValueError("hex command has an odd number of digits")
    return bytes.fromhex(digits)


def parse_net_target(text):
    """Return (host, port) from HOST, HOST:PORT, or [IPv6]:PORT. Raise
    ValueError when the port is not a number."""
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else DEFAULT_NET_PORT
        return host, port
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        try:
            return host, int(port)
        except ValueError:
            raise ValueError("invalid port in %r" % text)
    return text, DEFAULT_NET_PORT


def parse_usb_target(text):
    """Return (vid, pid) from hexadecimal VID:PID. Raise ValueError when
    the format is wrong."""
    parts = text.split(":")
    try:
        vid = int(parts[0], 16)
        pid = int(parts[1], 16) if len(parts) > 1 and parts[1] else None
    except ValueError:
        raise ValueError("use hexadecimal VID:PID, for example 0483:57241")
    return vid, pid


def encode_text(text, codepage, encoding):
    """Return the code-page command plus the encoded text."""
    data = b""
    codec = None
    if codepage:
        key = codepage.lower()
        if key in CODEPAGES:
            table, codec = CODEPAGES[key]
            data += cmd_codepage(table)
        else:
            try:
                table = int(codepage)
            except ValueError:
                die("unknown code page. Use one of: " + " ".join(sorted(CODEPAGES)))
            data += cmd_codepage(table)
            codec = "latin-1"
    if encoding:
        codec = encoding
    if codec is None:
        codec = "cp437"
    try:
        data += text.encode(codec, errors="replace")
    except LookupError:
        sys.stderr.write(
            "Warning: Python has no codec %s. It used cp437.\n" % codec)
        data += text.encode("cp437", errors="replace")
    return data


def build_test_receipt():
    """Return a small test receipt with feed and cut."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S").encode("ascii")
    return b"".join([
        cmd_init(),
        cmd_align("center"),
        cmd_bold("on"),
        b"MUNBYN PRINT TEST\n",
        cmd_bold("off"),
        cmd_align("left"),
        b"ESC/POS channel works.\n",
        b"Date: " + stamp + b"\n",
        cmd_feed(3),
        cmd_cut("partial", 0),
    ])


def decode_status(slot, raw):
    """Decode a DLE EOT response. Bit meanings follow the Epson ESC/POS
    standard. Clone firmware may differ."""
    if not raw:
        return ("No response. The link is write-only, or the printer did "
                "not answer.\nUse --net or --usb for a two-way link.")
    lines = ["Raw response: " + raw.hex(" ")]
    if len(raw) >= 4 and raw[:3] == DLE + EOT + byte(slot):
        status = raw[3]
    else:
        status = raw[-1]
    lines.append("Status byte: %02X" % status)
    if slot == 1:
        lines.append("Printer is %s." % ("offline" if status & 0x20 else "online"))
        if status & 0x40:
            lines.append("FEED button pressed.")
        if status & 0x04:
            lines.append("Drawer kick pin 3 is high.")
        if status & 0x08:
            lines.append("Drawer kick pin 2 is high.")
    elif slot == 2:
        causes = []
        if status & 0x04:
            causes.append("cover open")
        if status & 0x08:
            causes.append("paper feed pressed")
        if status & 0x20:
            causes.append("paper end")
        if status & 0x40:
            causes.append("paper near end")
        lines.append("Offline causes: %s." % (", ".join(causes) if causes else "none"))
    elif slot == 3:
        errors = []
        if status & 0x04:
            errors.append("recoverable error")
        if status & 0x08:
            errors.append("cutter error")
        if status & 0x20:
            errors.append("unrecoverable error")
        if status & 0x40:
            errors.append("auto-recoverable error")
        lines.append("Errors: %s." % (", ".join(errors) if errors else "none"))
    else:
        if status & 0x60:
            lines.append("Paper end.")
        elif status & 0x0C:
            lines.append("Paper near end.")
        else:
            lines.append("Paper present.")
    lines.append("Bit meanings follow the Epson ESC/POS standard. "
                 "Clone firmware may differ.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Terminal user interface (curses)
# ---------------------------------------------------------------------------

def _addstr(win, y, x, text, attr=None):
    """Write text to a curses window. Ignore out-of-range errors."""
    import curses
    try:
        win.addstr(y, x, text, attr if attr is not None else curses.A_NORMAL)
    except curses.error:
        pass


def curses_form(screen, title, fields):
    """Show a modal form. fields is a list of (name, value) pairs.
    Return the new values as a list, or None when the user cancels
    with Esc."""
    import curses
    field_width = 34
    max_width = screen.getmaxyx()[1] - 4
    width = min(max(len(title) + 6, field_width + 18), max_width)
    height = len(fields) + 5
    top = max(0, (screen.getmaxyx()[0] - height) // 2)
    win = curses.newwin(height, width, top, 2)
    values = [value for _, value in fields]
    selected = 0
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    while True:
        win.erase()
        win.border()
        _addstr(win, 1, 2, title[:width - 4])
        for i, (name, _) in enumerate(fields):
            marker = "> " if i == selected else "  "
            _addstr(win, i + 3, 2, (marker + name)[:width - field_width - 5])
            _addstr(win, i + 3, width - field_width - 3,
                    values[i][:field_width].ljust(field_width),
                    curses.A_REVERSE if i == selected else curses.A_NORMAL)
        _addstr(win, height - 2, 2, "Enter: next/finish  Esc: cancel")
        win.refresh()
        key = win.getch()
        if key == 27:
            return None
        if key in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(fields)
        elif key in (curses.KEY_DOWN, ord("j"), 9):
            selected = (selected + 1) % len(fields)
        elif key in (10, 13, curses.KEY_ENTER):
            if selected == len(fields) - 1:
                return values
            selected = (selected + 1) % len(fields)
        elif key in (8, 127, curses.KEY_BACKSPACE):
            values[selected] = values[selected][:-1]
        elif 32 <= key <= 126:
            values[selected] = (values[selected] + chr(key))[:field_width]


def curses_prompt(screen, title, initial=""):
    """Show a one-field form. Return the string, or None on cancel."""
    result = curses_form(screen, title, [(title, initial)])
    return None if result is None else result[0]


class TuiItem:
    """One row of the terminal menu.

    label: callable that takes the item and returns the row text.
    run: callable that takes the item and returns (payload, status_slot)
         or None when the action handled itself.
    cycle: list of text values for a cycling parameter.
    minimum, maximum: bounds for a numeric parameter.
    confirm: ask for a second Enter press before the run.
    """

    def __init__(self, label, run, value=None, minimum=None, maximum=None,
                 cycle=None, confirm=False):
        self.label = label
        self.run = run
        self.value = value
        self.minimum = minimum
        self.maximum = maximum
        self.cycle = cycle
        self.confirm = confirm


class TuiApp:
    """The full-screen terminal interface."""

    def __init__(self, screen, args):
        self.screen = screen
        self.timeout = args.timeout
        self.dry_run = False
        self.log_lines = []
        self.conn = None
        self.conn_text = "not connected"
        self.spec = {
            "net": args.net or "",
            "usb": args.usb or "",
            "dev": args.dev or "",
            "cups": args.cups or "",
            "win": args.win or "",
        }
        self.items = self._build_items()
        self.index = 0
        self.offset = 0
        self.pending_confirm = None
        self.connect()

    # --- menu definition ---

    def _build_items(self):
        """Build the menu rows."""
        items = []

        def add(label, run, value=None, minimum=None, maximum=None,
                cycle=None, confirm=False):
            items.append(TuiItem(label, run, value, minimum, maximum,
                                 cycle, confirm))

        add(lambda it: "Print a test receipt (feed and cut)",
            lambda it: (build_test_receipt(), None))
        add(lambda it: "Print a QR code, module size %d" % it.value,
            lambda it: self._qr_action(it), value=3, minimum=1, maximum=8)
        add(lambda it: "Print an image, dithered, %d dots wide" % it.value,
            lambda it: self._image_action(it),
            value=512, minimum=64, maximum=576)
        add(lambda it: "Print a grayscale test card",
            lambda it: self._gray_action(it))
        add(lambda it: "Initialize the printer (ESC @)",
            lambda it: (cmd_init(), None))
        add(lambda it: "Feed lines (%d)" % it.value,
            lambda it: (cmd_feed(it.value), None),
            value=3, minimum=0, maximum=255)
        add(lambda it: "Feed dots (%d)" % it.value,
            lambda it: (cmd_feed_dots(it.value), None),
            value=24, minimum=0, maximum=255)
        add(lambda it: "Cut, partial, feed %d first" % it.value,
            lambda it: (cmd_cut("partial", it.value), None),
            value=0, minimum=0, maximum=255)
        add(lambda it: "Cut, full, feed %d first" % it.value,
            lambda it: (cmd_cut("full", it.value), None),
            value=0, minimum=0, maximum=255)
        add(lambda it: "Status: paper sensor",
            lambda it: (cmd_status(4), 4))
        add(lambda it: "Status: printer state",
            lambda it: (cmd_status(1), 1))
        add(lambda it: "Status: offline cause",
            lambda it: (cmd_status(2), 2))
        add(lambda it: "Status: error cause",
            lambda it: (cmd_status(3), 3))
        add(lambda it: "Line spacing (%d/144 inch)" % it.value,
            lambda it: (cmd_spacing(it.value), None),
            value=24, minimum=0, maximum=255)
        add(lambda it: "Line spacing: reset",
            lambda it: (cmd_spacing(None), None))
        add(lambda it: "Alignment: %s" % it.cycle[it.value],
            lambda it: (cmd_align(it.cycle[it.value]), None),
            value=0, cycle=["left", "center", "right"])
        add(lambda it: "Bold: %s" % it.cycle[it.value],
            lambda it: (cmd_bold(it.cycle[it.value]), None),
            value=0, cycle=["off", "on"])
        add(lambda it: "Code page: %s" % it.cycle[it.value],
            lambda it: (codepage_payload(it.cycle[it.value]), None),
            value=0, cycle=list(CODEPAGES))
        add(lambda it: "Beep %d time(s) (model-dependent)" % it.value,
            lambda it: (cmd_beep(it.value, 5), None),
            value=1, minimum=1, maximum=9)
        add(lambda it: "Kick the cash drawer",
            lambda it: (cmd_drawer(0, 0x1E, 0xFF), None))
        add(lambda it: "FEED button: %s" % it.cycle[it.value],
            lambda it: (cmd_button(it.cycle[it.value]), None),
            value=0, cycle=["on", "off"])
        add(lambda it: "Left margin (%d dots)" % it.value,
            lambda it: (cmd_margin(it.value), None),
            value=0, minimum=0, maximum=255)
        add(lambda it: "Print area width (%d dots)" % it.value,
            lambda it: (cmd_print_area(it.value), None),
            value=512, minimum=1, maximum=576)
        add(lambda it: "Print density %d of 8 (MUNBYN)" % it.value,
            lambda it: (cmd_density(it.value), None),
            value=5, minimum=1, maximum=8)
        add(lambda it: "Media width: %s (MUNBYN)" % MEDIA_WIDTHS[it.value],
            lambda it: (cmd_media_width(it.value), None),
            value=2, minimum=0, maximum=8)
        add(lambda it: "Factory reset (MUNBYN)",
            lambda it: (cmd_factory_reset(), None), confirm=True)
        add(lambda it: "LAN: always DHCP (MUNBYN)",
            lambda it: (cmd_dhcp_always(), None))
        add(lambda it: "Wi-Fi start receipt: %s (MUNBYN)" % it.cycle[it.value],
            lambda it: (cmd_wifi_receipt(it.cycle[it.value]), None),
            value=0, cycle=["on", "off"])
        add(lambda it: "Wi-Fi DHCP: %s (MUNBYN)" % it.cycle[it.value],
            lambda it: (cmd_wifi_dhcp(it.cycle[it.value]), None),
            value=0, cycle=["on", "off"])
        add(lambda it: "Wi-Fi module reset (MUNBYN)",
            lambda it: (cmd_wifi_reset(), None), confirm=True)
        add(lambda it: "Send a raw hex sequence",
            lambda it: self._hex_action())
        add(lambda it: "Dry run: %s" % it.cycle[it.value],
            lambda it: self._toggle_dry_run(it),
            value=0, cycle=["off", "on"])
        add(lambda it: "Change the connection",
            lambda it: self._change_connection())
        return items

    # --- connection handling ---

    def close_conn(self):
        """Close the current connection, if any."""
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None

    def connect(self):
        """Open the connection that the first non-empty field selects."""
        self.close_conn()
        spec = self.spec
        try:
            if spec["net"]:
                host, port = parse_net_target(spec["net"])
                self.conn = NetworkConnection(host, port, self.timeout)
                self.conn_text = "LAN %s:%d" % (host, port)
            elif spec["usb"]:
                vid, pid = parse_usb_target(spec["usb"])
                self.conn = UsbConnection(vid, pid)
                self.conn_text = "USB %s" % spec["usb"]
            elif spec["dev"]:
                self.conn = FileConnection(spec["dev"])
                self.conn_text = "device %s" % spec["dev"]
            elif spec["cups"]:
                self.conn = CupsConnection(spec["cups"])
                self.conn_text = "CUPS %s" % spec["cups"]
            elif spec["win"]:
                self.conn = WindowsPrinterConnection(spec["win"])
                self.conn_text = "Windows %s" % spec["win"]
            else:
                # Same auto-detect order as open_connection().
                for path in ("/dev/usb/lp0", "/dev/usb/lp1", "/dev/usb/lp2"):
                    if os.path.exists(path):
                        self.conn = FileConnection(path)
                        self.conn_text = "device %s (auto)" % path
                        break
                else:
                    self.conn = UsbConnection(None, None)
                    self.conn_text = "USB (auto)"
            self.log("Connected: %s" % self.conn_text)
        except (RuntimeError, OSError, ValueError) as exc:
            self.conn = None
            self.conn_text = "not connected"
            self.log("Connection failed: %s" % exc)

    # --- output and actions ---

    def log(self, text):
        """Add text to the log panel."""
        _, width = self.screen.getmaxyx()
        for line in str(text).splitlines() or [""]:
            for part in textwrap.wrap(line, width - 4) or [""]:
                self.log_lines.append(part)
        del self.log_lines[:-200]

    def _send(self, payload, label, status_slot=None):
        """Send a payload and log the result."""
        hex_text = payload.hex(" ")
        if len(hex_text) > 48:
            hex_text = hex_text[:48] + " ..."
        if self.dry_run:
            self.log("Dry run %s: %s" % (label, hex_text))
            return
        if self.conn is None:
            self.connect()
        if self.conn is None:
            self.log("No connection. Press c to configure one.")
            return
        try:
            self.conn.send(payload)
            self.log("Sent %s: %s" % (label, hex_text))
            if status_slot is not None:
                raw = self.conn.read(4, max(self.timeout, 3.0))
                for line in decode_status(status_slot, raw).splitlines():
                    self.log(line)
        except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
            self.log("Send failed: %s" % exc)
            self.close_conn()
            self.conn_text = "not connected"

    def _restore_cursor(self):
        """Hide the cursor again after a form."""
        import curses
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    def _hex_action(self):
        """Ask for a hex sequence, then send it."""
        text = curses_prompt(self.screen, "Hex sequence, e.g. 1B 40 0A")
        self._restore_cursor()
        if not text:
            return None
        try:
            payload = parse_hex(text)
        except ValueError as exc:
            self.log(str(exc))
            return None
        self._send(payload, "hex sequence")
        return None

    def _qr_action(self, item):
        """Ask for text, then print a QR code."""
        text = curses_prompt(self.screen, "QR code content")
        self._restore_cursor()
        if not text:
            return None
        try:
            payload = qr_payload(text, item.value, "M", 512)
        except (RuntimeError, OSError, ValueError) as exc:
            self.log(str(exc))
            return None
        self._send(payload, "QR code")
        return None

    def _image_action(self, item):
        """Ask for an image path, then print it dithered."""
        path = curses_prompt(self.screen, "Image file path")
        self._restore_cursor()
        if not path:
            return None
        try:
            payload = image_payload(os.path.expanduser(path), item.value,
                                    False, True)
        except (RuntimeError, OSError, ValueError) as exc:
            self.log(str(exc))
            return None
        self._send(payload, "dithered image")
        return None

    def _gray_action(self, item):
        """Print the grayscale test card."""
        try:
            payload = gray_test_payload()
        except (RuntimeError, ValueError) as exc:
            self.log(str(exc))
            return None
        self._send(payload, "grayscale test card")
        return None

    def _toggle_dry_run(self, item):
        """Turn dry run on or off."""
        item.value = (item.value + 1) % len(item.cycle)
        self.dry_run = item.value == 1
        self.log("Dry run is %s." % ("on" if self.dry_run else "off"))
        return None

    def _change_connection(self):
        """Open the connection form."""
        fields = [
            ("LAN host[:port]", self.spec["net"]),
            ("USB VID:PID (empty = auto)", self.spec["usb"]),
            ("Device file, /dev/usb/lp0", self.spec["dev"]),
            ("CUPS queue name", self.spec["cups"]),
            ("Windows queue name", self.spec["win"]),
        ]
        result = curses_form(
            self.screen,
            "Connection - the first field with text wins", fields)
        self._restore_cursor()
        if result is None:
            return None
        (self.spec["net"], self.spec["usb"], self.spec["dev"],
         self.spec["cups"], self.spec["win"]) = result
        self.connect()
        return None

    # --- drawing and input loop ---

    def _adjust(self, item, delta):
        """Change the parameter of one menu row."""
        if item.cycle is not None:
            item.value = (item.value + delta) % len(item.cycle)
        elif item.minimum is not None:
            item.value = max(item.minimum,
                            min(item.maximum, item.value + delta))

    def _draw(self):
        """Draw the header, the menu, and the log panel."""
        import curses
        screen = self.screen
        screen.erase()
        height, width = screen.getmaxyx()
        log_height = min(8, max(3, height // 4))
        menu_height = max(1, height - log_height - 4)
        _addstr(screen, 0, 0, "munbynctl - MUNBYN receipt printer control")
        _addstr(screen, 1, 0, "Connection: %s   Dry run: %s"
                % (self.conn_text, "on" if self.dry_run else "off"))
        if self.index < self.offset:
            self.offset = self.index
        if self.index >= self.offset + menu_height:
            self.offset = self.index - menu_height + 1
        for row in range(menu_height):
            pos = self.offset + row
            if pos >= len(self.items):
                break
            text = self.items[pos].label(self.items[pos])
            attr = curses.A_REVERSE if pos == self.index else curses.A_NORMAL
            _addstr(screen, row + 3, 1, ("  %s" % text)[:width - 2], attr)
        log_top = height - log_height - 1
        _addstr(screen, log_top, 0, "-" * (width - 1))
        for i, line in enumerate(self.log_lines[-(log_height - 1):]):
            _addstr(screen, log_top + 1 + i, 1, line[:width - 2])
        _addstr(screen, height - 1, 0,
                (" Up/Down select  Enter run  Left/Right value  "
                 "r status  c connect  l clear log  q quit ")[:width - 1],
                curses.A_REVERSE)
        screen.refresh()

    def _activate(self):
        """Run the selected menu row."""
        item = self.items[self.index]
        if item.confirm and self.pending_confirm != self.index:
            self.pending_confirm = self.index
            self.log("Press Enter again to confirm: %s" % item.label(item))
            return
        self.pending_confirm = None
        result = item.run(item)
        if result is None:
            return
        payload, status_slot = result
        self._send(payload, item.label(item), status_slot)

    def run(self):
        """Run the input loop until the user quits."""
        import curses
        self._restore_cursor()
        while True:
            self._draw()
            key = self.screen.getch()
            if key in (ord("q"), 27):
                break
            if key in (curses.KEY_UP, ord("k")):
                self.pending_confirm = None
                self.index = (self.index - 1) % len(self.items)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.pending_confirm = None
                self.index = (self.index + 1) % len(self.items)
            elif key in (curses.KEY_LEFT, ord("-")):
                self.pending_confirm = None
                self._adjust(self.items[self.index], -1)
            elif key in (curses.KEY_RIGHT, ord("+"), ord("=")):
                self.pending_confirm = None
                self._adjust(self.items[self.index], 1)
            elif key in (10, 13, curses.KEY_ENTER):
                self._activate()
            elif key == ord("r"):
                self._send(cmd_status(4), "status 4", 4)
            elif key == ord("l"):
                self.log_lines = []
            elif key == ord("c"):
                self._change_connection()
        self.close_conn()


def tui_main(args):
    """Open the terminal interface."""
    try:
        import curses
    except ImportError:
        die("the TUI needs the curses module. "
            "On Windows, run: pip install windows-curses")

    def entry(screen):
        TuiApp(screen, args).run()

    try:
        curses.wrapper(entry)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------

def codepage_payload(page):
    key = page.lower()
    if key in CODEPAGES:
        return cmd_codepage(CODEPAGES[key][0])
    try:
        return cmd_codepage(int(page))
    except ValueError:
        die("unknown code page. Use one of: " + " ".join(sorted(CODEPAGES)))


def build_payload(args):
    """Return (payload, status_slot) for the parsed arguments."""
    c = args.command
    if c == "init":
        return cmd_init(), None
    if c == "feed":
        return cmd_feed(args.lines), None
    if c == "feeddots":
        return cmd_feed_dots(args.dots), None
    if c == "cut":
        return cmd_cut(args.mode, args.feed), None
    if c == "spacing":
        return cmd_spacing(args.dots), None
    if c == "align":
        return cmd_align(args.mode), None
    if c == "bold":
        return cmd_bold(args.state), None
    if c == "underline":
        return cmd_underline(args.state), None
    if c == "codepage":
        return codepage_payload(args.page), None
    if c == "text":
        payload = encode_text(args.string, args.codepage, args.encoding)
        if not args.no_newline:
            payload += b"\n"
        return payload, None
    if c == "beep":
        return cmd_beep(args.count, args.duration), None
    if c == "drawer":
        return cmd_drawer(args.pin, args.on, args.off), None
    if c == "button":
        return cmd_button(args.state), None
    if c == "margin":
        return cmd_margin(args.dots), None
    if c == "area":
        return cmd_print_area(args.dots), None
    if c == "status":
        return cmd_status(args.slot), args.slot
    if c == "density":
        return cmd_density(args.level), None
    if c == "media-width":
        return cmd_media_width(args.index), None
    if c == "factory-reset":
        return cmd_factory_reset(), None
    if c == "dhcp-always":
        return cmd_dhcp_always(), None
    if c == "wifi-receipt":
        return cmd_wifi_receipt(args.state), None
    if c == "wifi-dhcp":
        return cmd_wifi_dhcp(args.state), None
    if c == "wifi-reset":
        return cmd_wifi_reset(), None
    if c == "qr":
        return qr_payload(args.text, args.scale, args.ecc, args.paper), None
    if c == "image":
        return image_payload(os.path.expanduser(args.path), args.width,
                             args.invert, args.fit, args.threshold), None
    if c == "graytest":
        return gray_test_payload(args.width), None
    if c == "hex":
        try:
            return parse_hex(args.sequence), None
        except ValueError as exc:
            die(str(exc))
    if c == "test":
        return build_test_receipt(), None
    die("unknown command %r" % c)


def open_connection(args):
    """Return the connection that the options select, or an
    auto-detected one."""
    if args.net:
        try:
            host, port = parse_net_target(args.net)
        except ValueError as exc:
            die(str(exc))
        return NetworkConnection(host, port, args.timeout)
    if args.usb:
        try:
            vid, pid = parse_usb_target(args.usb)
        except ValueError as exc:
            die(str(exc))
        return UsbConnection(vid, pid)
    if args.dev:
        return FileConnection(args.dev)
    if args.cups:
        return CupsConnection(args.cups)
    if args.win:
        return WindowsPrinterConnection(args.win)
    # Auto-detect.
    for path in ("/dev/usb/lp0", "/dev/usb/lp1", "/dev/usb/lp2"):
        if os.path.exists(path):
            sys.stderr.write("Auto: using %s. Use --dev to override.\n" % path)
            return FileConnection(path)
    try:
        return UsbConnection(None, None)
    except RuntimeError as exc:
        die("no link was given or found.\n"
            "  --net HOST[:PORT]   LAN printers (default port 9100)\n"
            "  --usb VID:PID       direct USB, needs pyusb\n"
            "  --dev /dev/usb/lp0  Linux usblp device\n"
            "  --cups NAME          CUPS queue, sends with lp -o raw\n"
            "  --win NAME           Windows queue in raw mode\n"
            "Detail: %s" % exc)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="munbynctl",
        description="Configure and test a MUNBYN ESC/POS receipt printer.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    g = parser.add_argument_group("connection (put before the command)")
    g.add_argument("--net", metavar="HOST[:PORT]",
                   help="LAN printer. The port defaults to 9100.")
    g.add_argument("--usb", metavar="VID:PID",
                   help="USB printer by hexadecimal vendor and product id. "
                        "Auto-detects a printer-class device when omitted.")
    g.add_argument("--dev", metavar="PATH",
                   help="raw device file, for example /dev/usb/lp0.")
    g.add_argument("--cups", metavar="PRINTER",
                   help="CUPS queue name. Sends with lp -o raw.")
    g.add_argument("--win", metavar="PRINTER",
                   help="Windows queue name in raw mode.")
    g.add_argument("--timeout", type=float, default=5.0,
                   help="timeout in seconds (default 5).")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the byte sequence and exit.")
    parser.add_argument("--verbose", action="store_true",
                        help="print the bytes that were sent.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    p = sub.add_parser("init", help="Initialize the printer (ESC @).")
    p = sub.add_parser("feed", help="Print the buffer and feed n lines (ESC d).")
    p.add_argument("lines", nargs="?", type=int, default=1,
                   help="number of lines, 0-255 (default 1).")
    p = sub.add_parser("feeddots", help="Print the buffer and feed n dots (ESC J).")
    p.add_argument("dots", nargs="?", type=int, default=24,
                   help="number of dots, 0-255 (default 24).")
    p = sub.add_parser(
        "cut", help="Cut the paper (GS V). Default bytes: 1D 56 42 00.")
    p.add_argument("--mode", choices=["full", "partial"], default="partial",
                   help="cut type (default partial).")
    p.add_argument("--feed", type=int, default=0,
                   help="motion units to feed before the cut (default 0).")
    p = sub.add_parser(
        "spacing", help="Set the line spacing (ESC 3), or reset it (ESC 2).")
    p.add_argument("dots", nargs="?", type=int, default=None,
                   help="n/144 inch per line, 24 is the standard. Omit to reset.")
    p = sub.add_parser("align", help="Set the alignment (ESC a).")
    p.add_argument("mode", choices=["left", "center", "right"])
    p = sub.add_parser("bold", help="Turn bold on or off (ESC E).")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("underline", help="Set the underline (ESC -).")
    p.add_argument("state", choices=["off", "single", "double"])
    p = sub.add_parser("codepage", help="Select the code table (ESC t).")
    p.add_argument("page", help="name from the help text, or a table number.")
    p = sub.add_parser("text", help="Send text, with an optional code page.")
    p.add_argument("string")
    p.add_argument("--codepage", default="pc437",
                   help="pc437 pc850 pc860 pc863 pc865 wpc1252 pc852 pc858 "
                        "or a table number (default pc437).")
    p.add_argument("--encoding", default=None,
                   help="Python codec for the text, for example utf-8.")
    p.add_argument("--no-newline", action="store_true",
                   help="do not append a newline.")
    p = sub.add_parser("beep", help="Sound the buzzer (ESC B, model-dependent).")
    p.add_argument("count", nargs="?", type=int, default=1,
                   help="number of beeps, 1-9 (default 1).")
    p.add_argument("duration", nargs="?", type=int, default=5,
                   help="duration units (default 5).")
    p = sub.add_parser("drawer", help="Kick the cash drawer (ESC p).")
    p.add_argument("--pin", type=int, default=0, help="pin, 0 or 2 (default 0).")
    p.add_argument("--on", type=int, default=0x1E,
                   help="on time in 2 ms units (default 0x1E).")
    p.add_argument("--off", type=int, default=0xFF,
                   help="off time in 2 ms units (default 0xFF).")
    p = sub.add_parser("button", help="Enable or disable the FEED button (ESC c 5).")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("margin", help="Set the left margin in dots (GS L).")
    p.add_argument("dots", type=int)
    p = sub.add_parser("area", help="Set the print area width in dots (GS W).")
    p.add_argument("dots", type=int)
    p = sub.add_parser(
        "status", help="Read one real-time status (DLE EOT). Needs a two-way link.")
    p.add_argument("slot", nargs="?", type=int, choices=[1, 2, 3, 4], default=4,
                   help="1 printer, 2 offline cause, 3 errors, 4 paper (default 4).")
    p = sub.add_parser(
        "density", help="Print density 1 (light) to 8 (dark). MUNBYN command.")
    p.add_argument("level", type=int)
    p = sub.add_parser(
        "media-width", help="Print width. MUNBYN command. "
        "0=72 1=76 2=80 3=48 4=52 5=56 6=64 7=68 8=54 mm.")
    p.add_argument("index", type=int)
    p = sub.add_parser(
        "factory-reset", help="Restore the factory settings. MUNBYN command.")
    p = sub.add_parser(
        "dhcp-always", help="Always use DHCP on the network port. MUNBYN command.")
    p = sub.add_parser(
        "wifi-receipt",
        help="Print the Wi-Fi configuration receipt at startup. MUNBYN command.")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("wifi-dhcp", help="DHCP on the Wi-Fi interface. MUNBYN command.")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("wifi-reset", help="Reset the Wi-Fi module. MUNBYN command.")
    p = sub.add_parser(
        "hex", help="Send an arbitrary hex sequence, for example \"1B 40 0A\".")
    p.add_argument("sequence")
    p = sub.add_parser("test", help="Print a test receipt with feed and cut.")
    p = sub.add_parser("qr", help="Print a QR code (needs qrcode or segno).")
    p.add_argument("text", help="QR content.")
    p.add_argument("--scale", type=int, default=3,
                   help="module size in dots, 1-16 (default 3).")
    p.add_argument("--ecc", choices=["L", "M", "Q", "H"], default="M",
                   help="error correction level (default M).")
    p.add_argument("--paper", type=int, default=512,
                   help="printable paper width in dots, for centering "
                        "(default 512).")
    p = sub.add_parser("image",
                       help="Print an image, dithered from grayscale "
                            "(needs Pillow).")
    p.add_argument("path", help="image file path.")
    p.add_argument("--width", type=int, default=512,
                   help="maximum print width in dots (default 512).")
    p.add_argument("--invert", action="store_true",
                   help="invert black and white.")
    p.add_argument("--fit", action="store_true",
                   help="scale small images up to the print width, "
                        "with interpolation before dithering.")
    p.add_argument("--threshold", type=int, default=None,
                   help="print line art without dithering: pixels darker "
                        "than this gray level print black (0-255).")
    p = sub.add_parser("graytest",
                       help="Print a grayscale (dither) test card. "
                            "Needs no package.")
    p.add_argument("--width", type=int, default=512,
                   help="card width in dots (default 512).")
    p = sub.add_parser("tui",
                       help="Open the full-screen terminal interface (curses).")
    return parser


def main():
    args = build_parser().parse_args()
    if args.command == "tui":
        tui_main(args)
        return
    try:
        payload, status_slot = build_payload(args)
    except (RuntimeError, OSError, ValueError) as exc:
        die(str(exc))
    if args.dry_run:
        print(" ".join("%02X" % b for b in payload))
        return
    conn = open_connection(args)
    try:
        conn.send(payload)
        if args.verbose:
            print("Sent %d bytes: %s" % (len(payload), payload.hex(" ")))
        if status_slot is not None:
            print(decode_status(status_slot, conn.read(4, args.timeout)))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
