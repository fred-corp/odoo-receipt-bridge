#!/usr/bin/env python3
# odoo_receipt.py
#
# Fetch orders from an Odoo eCommerce store. Print a receipt for each order
# on an ESC/POS receipt printer (Epson type, for example MUNBYN).
#
# The script needs only the Python standard library. The direct USB transport
# needs the optional 'pyusb' package.
#
# Quick start:
#   1. python odoo_receipt.py init
#   2. Edit the config file. Fill in url, db, user, api_key, and printer.
#   3. python odoo_receipt.py check
#   4. python odoo_receipt.py orders
#   5. python odoo_receipt.py print S00042 --text
#   6. python odoo_receipt.py print S00042
#   7. python odoo_receipt.py print S00042 --internal  # packing list
#   8. python odoo_receipt.py poll --interval 30
#   9. python odoo_receipt.py serve   # local bridge for the button in Odoo
#
# Settings come from three sources. Later sources override earlier ones:
#   1. the config file (default: ~/.config/odoo-receipt/config.json),
#   2. environment variables (ODOO_URL, ODOO_DB, ODOO_USER, ODOO_API_KEY,
#      PRINTER_TRANSPORT, PRINTER_TARGET, ODOO_STATE_PATH),
#   3. command line options.
#
# Put the command line options before the subcommand:
#   python odoo_receipt.py --dry-run poll --once

import argparse
import http.server
import json
import os
import secrets
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

VERSION = "1.0.0"
DEFAULT_CONFIG_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "odoo-receipt", "config.json")
DEFAULT_STATE_PATH = os.path.join(
    os.path.expanduser("~"), ".local", "state", "odoo-receipt", "state.json")

ORDER_FIELDS = ["name", "partner_id", "date_order", "state",
                "amount_untaxed", "amount_tax", "amount_total",
                "currency_id", "order_line"]
LINE_FIELDS = ["product_id", "name", "product_uom_qty", "price_unit",
               "price_subtotal", "price_total", "display_type"]


# --------------------------------------------------------------------------
# Odoo client
# --------------------------------------------------------------------------

class OdooError(Exception):
    pass


class OdooClient:
    """Client for the Odoo external API (JSON-RPC or JSON-2)."""

    def __init__(self, url, db, user, api_key, api="jsonrpc", timeout=30):
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.api_key = api_key
        self.api = api
        self.timeout = timeout
        self.uid = None

    # -- transport ---------------------------------------------------------

    def _http_post(self, path, payload, headers=None):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise OdooError("HTTP error %s from %s" % (exc.code, path)) from None
        except urllib.error.URLError as exc:
            raise OdooError("cannot reach the server: %s" % exc.reason) from None
        return json.loads(body)

    @staticmethod
    def _error_message(reply):
        err = reply.get("error") or {}
        data = err.get("data") or {}
        return data.get("message") or err.get("message") or json.dumps(err)

    def _call_jsonrpc(self, service, method, args):
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": 1,
        }
        reply = self._http_post("/jsonrpc", payload)
        if "error" in reply:
            raise OdooError(self._error_message(reply))
        return reply.get("result")

    def _call_json2(self, model, method, args, kwargs):
        # Experimental. The JSON-2 API serves Odoo 19 and later. It uses an
        # API key in the Authorization header. Verify the body format against
        # the Odoo 19 documentation before you rely on it.
        payload = {"args": args, "kwargs": kwargs}
        headers = {"Authorization": "Bearer " + self.api_key}
        if self.db:
            headers["X-Odoo-Database"] = self.db
        reply = self._http_post("/json/2/%s/%s" % (model, method), payload,
                                headers)
        if "error" in reply:
            raise OdooError(self._error_message(reply))
        return reply.get("result")

    # -- public interface --------------------------------------------------

    def call(self, model, method, args=None, kwargs=None):
        args = args or []
        kwargs = kwargs or {}
        if self.api == "json2":
            return self._call_json2(model, method, args, kwargs)
        if self.uid is None:
            self.authenticate()
        return self._call_jsonrpc(
            "object", "execute_kw",
            [self.db, self.uid, self.api_key, model, method, args, kwargs])

    def authenticate(self):
        """Log in and store the user id. The API key replaces the password."""
        if self.api == "json2":
            # The JSON-2 API has no separate login step. Check the key.
            result = self._call_json2("res.users", "context_get", [], {})
            self.uid = (result or {}).get("uid")
            return self.uid
        uid = self._call_jsonrpc(
            "common", "authenticate", [self.db, self.user, self.api_key, {}])
        if not uid:
            raise OdooError("login failed. Check db, user, and api_key.")
        self.uid = uid
        return uid

    def search_read(self, model, domain, fields, limit=None, order=None):
        kwargs = {"fields": fields}
        if limit:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return self.call(model, "search_read", [domain], kwargs)

    def read(self, model, ids, fields):
        if not ids:
            return []
        return self.call(model, "read", [ids], {"fields": fields})


def make_client(cfg):
    if not cfg.get("url"):
        raise OdooError("set 'url' in the config file, or use --url")
    if not cfg.get("api_key"):
        raise OdooError("set 'api_key' in the config file, or use --api-key")
    if cfg.get("api") == "jsonrpc":
        if not cfg.get("db"):
            raise OdooError("set 'db' in the config file, or use --db")
        if not cfg.get("user"):
            raise OdooError("set 'user' in the config file, or use --user")
    return OdooClient(cfg["url"], cfg.get("db", ""), cfg.get("user", ""),
                      cfg["api_key"], api=cfg.get("api", "jsonrpc"),
                      timeout=cfg.get("timeout", 30))


# --------------------------------------------------------------------------
# Order and product data
# --------------------------------------------------------------------------

def ecommerce_domain(states, only_website):
    """Build the search domain for eCommerce orders.

    Orders from the website carry a value in 'website_id'. Orders without
    that value come from the back end. Confirmed orders have the state
    'sale' or 'done'. Drafts are usually abandoned carts.
    """
    domain = []
    if only_website:
        domain.append(("website_id", "!=", False))
    if states:
        domain.append(("state", "in", list(states)))
    return domain


def fetch_orders(client, domain, limit=20, order="id desc"):
    return client.search_read("sale.order", domain, ORDER_FIELDS,
                              limit=limit, order=order)


def fetch_order(client, ref):
    """Fetch one order by name, for example S00042, or by numeric id."""
    ref = str(ref)
    if ref.isdigit():
        domain = [("id", "=", int(ref))]
    else:
        domain = [("name", "=", ref)]
    orders = client.search_read("sale.order", domain, ORDER_FIELDS, limit=1)
    if not orders:
        raise OdooError("no order found for %r" % ref)
    return orders[0]


def fetch_lines(client, order):
    """Fetch the order lines in the same order as the order shows them."""
    line_ids = order.get("order_line") or []
    if not line_ids:
        return []
    lines = client.read("sale.order.line", line_ids, LINE_FIELDS)
    pos = {line_id: index for index, line_id in enumerate(line_ids)}
    lines.sort(key=lambda line: pos.get(line["id"], 0))
    # Custom attribute values exist on Odoo 17 and later. Read them in a
    # separate call. Older versions reject the field. Ignore that error.
    by_id = {}
    try:
        extra = client.read("sale.order.line", line_ids,
                            ["custom_product_template_attribute_value_ids"])
        by_id = {rec["id"]: rec for rec in extra}
    except OdooError:
        pass
    # No-variant attribute values are chosen on the product page but they do
    # not create a product variant, so they are stored on the order line.
    # Read them in a separate call too, and ignore versions without them.
    nov_by_id = {}
    try:
        nov = client.read("sale.order.line", line_ids,
                          ["no_variant_attribute_value_ids"])
        nov_by_id = {rec["id"]: rec for rec in nov}
    except OdooError:
        pass
    for line in lines:
        rec = by_id.get(line["id"]) or {}
        line["custom_values"] = rec.get(
            "custom_product_template_attribute_value_ids") or []
        line["no_variant_values"] = (
            nov_by_id.get(line["id"]) or {}).get(
                "no_variant_attribute_value_ids") or []
    return lines


def fetch_variants(client, lines):
    """Fetch the variant data for all products on the order lines.

    Returns two items:
    1. {variant_id: {"name": str, "attributes": [str, ...]}}
    2. {ptav_id: record} for the custom values, or an empty dict.
    """
    ids = sorted({line["product_id"][0] for line in lines
                  if line.get("product_id")})
    variants = {}
    if not ids:
        return variants, {}
    prods = client.read("product.product", ids,
                        ["name", "product_template_attribute_value_ids"])
    for prod in prods:
        variants[prod["id"]] = {
            "name": prod.get("name") or prod.get("display_name") or "",
            "attributes": [],
        }
    ptav_ids = sorted({pid for prod in prods
                       for pid in
                       (prod.get("product_template_attribute_value_ids")
                        or [])})
    ptav = {}
    if ptav_ids:
        try:
            ptav = {rec["id"]: rec for rec in client.read(
                "product.template.attribute.value", ptav_ids,
                ["name", "attribute_id"])}
        except OdooError:
            try:
                ptav = {rec["id"]: rec for rec in client.read(
                    "product.template.attribute.value", ptav_ids, ["name"])}
            except OdooError:
                ptav = {}
    for prod in prods:
        entry = variants[prod["id"]]
        for pid in (prod.get("product_template_attribute_value_ids") or []):
            rec = ptav.get(pid)
            if not rec:
                continue
            attr = ""
            if isinstance(rec.get("attribute_id"), (list, tuple)):
                attr = rec["attribute_id"][1] or ""
            value = rec.get("name") or ""
            label = ("%s: %s" % (attr, value)) if attr else str(value)
            if label and label not in entry["attributes"]:
                entry["attributes"].append(label)
    # Fetch the custom values and the no-variant values separately. They
    # are not on the variant.
    custom_ids = sorted({pid for line in lines
                         for pid in ((line.get("custom_values") or [])
                                     + (line.get("no_variant_values") or []))})
    custom_ptav = {}
    if custom_ids:
        recs = []
        try:
            recs = client.read("product.template.attribute.value", custom_ids,
                               ["name", "attribute_id", "custom_value"])
        except OdooError:
            try:
                recs = client.read("product.template.attribute.value",
                                   custom_ids, ["name", "attribute_id"])
            except OdooError:
                recs = []
        custom_ptav = {rec["id"]: rec for rec in recs}
    return variants, custom_ptav


def line_variant_labels(line, variants, custom_ptav):
    """The variant labels of one order line, for the bridge UI."""
    product = line.get("product_id")
    variant = variants.get(product[0]) if product else None
    labels = list((variant or {}).get("attributes") or [])
    for pid in ((line.get("custom_values") or [])
                + (line.get("no_variant_values") or [])):
        rec = custom_ptav.get(pid)
        if not rec:
            continue
        attr = ""
        if isinstance(rec.get("attribute_id"), (list, tuple)):
            attr = rec["attribute_id"][1] or ""
        value = rec.get("custom_value") or rec.get("name") or ""
        label = ("%s: %s" % (attr, value)) if attr else str(value)
        if label and label not in labels:
            labels.append(label)
    return labels


def fetch_currency(client, order):
    info = {"symbol": "", "position": "after"}
    cur = order.get("currency_id")
    if cur:
        try:
            recs = client.read("res.currency", [cur[0]],
                               ["symbol", "position"])
            if recs:
                info["symbol"] = recs[0].get("symbol") or cur[1]
                info["position"] = recs[0].get("position") or "after"
        except OdooError:
            info["symbol"] = cur[1]
    return info


# --------------------------------------------------------------------------
# Receipt model and rendering
# --------------------------------------------------------------------------

class Receipt:
    """A text receipt that renders to plain text or to ESC/POS bytes."""

    def __init__(self, width=48, left_margin=0):
        self.width = width
        self.left_margin = max(0, int(left_margin or 0))
        self.items = []  # list of (text, style)
        self.barcode = None

    def content_width(self, size=1):
        """Width for the given font size, without the left margin."""
        eff = (self.width - self.left_margin) // size
        return max(1, eff)

    def add(self, text="", bold=False, align="left", size=1, hang=""):
        eff = self.content_width(size)
        pieces = textwrap.wrap(str(text), width=eff,
                               subsequent_indent=hang,
                               break_long_words=True) or [""]
        style = {"bold": bool(bold), "align": align, "size": size}
        for piece in pieces:
            self.items.append((piece, style))

    def rule(self):
        self.items.append(("-" * self.content_width(1),
                           {"bold": False, "align": "left", "size": 1}))

    def row(self, left, right="", bold=False, size=1):
        """One line with text on the left and a value on the right."""
        left = str(left)
        right = str(right)
        eff = self.content_width(size)
        if not right:
            self.add(left, bold=bold, size=size)
            return
        if len(left) + 1 + len(right) <= eff:
            pad = " " * (eff - len(left) - len(right))
            self.items.append((left + pad + right,
                               {"bold": bold, "align": "left", "size": size}))
            return
        # The left text is too long. Wrap it. Put the value on the last line
        # when it fits. If not, print the value on its own line.
        pieces = textwrap.wrap(left, width=eff, subsequent_indent="  ") or [""]
        plain = {"bold": False, "align": "left", "size": size}
        for piece in pieces:
            self.items.append((piece, plain))
        last = pieces[-1]
        if len(last) + 1 + len(right) <= eff:
            pad = " " * (eff - len(last) - len(right))
            self.items.append(
                (last + pad + right,
                 {"bold": bold, "align": "left", "size": size}))
        else:
            self.items.append((right.rjust(eff),
                               {"bold": bold, "align": "right",
                                "size": size}))

    def compose(self, text, style):
        """Pad one line to the content width and add the left margin."""
        eff = self.content_width(style["size"])
        if style["align"] == "center":
            line = text.center(eff)
        elif style["align"] == "right":
            line = text.rjust(eff)
        else:
            line = text.ljust(eff)
        # A character in double-width print (size 2) is two columns wide.
        # Print ceil(margin / size) spaces so the margin stays constant.
        spaces = (self.left_margin + style["size"] - 1) // style["size"]
        return " " * spaces + line

    def render_text(self):
        """Render the receipt as plain text. Use it for a screen preview."""
        out = []
        for text, style in self.items:
            out.append(self.compose(text, style).rstrip())
        return "\n".join(out)


ESC = b"\x1b"
GS = b"\x1d"


def escpos_barcode(data, height=50):
    """CODE128 barcode. The data goes after the code set B start code."""
    payload = b"{B" + str(data).upper().encode("ascii", "replace")
    out = b""
    out += GS + b"\x77" + bytes([2])                 # module width
    out += GS + b"\x68" + bytes([height])             # bar height
    out += GS + b"\x48" + bytes([2])                  # text below the bars
    out += GS + b"\x6b" + bytes([73])                 # CODE128
    out += bytes([len(payload)]) + payload
    return out


def escpos_bytes(receipt):
    """Render the receipt as an ESC/POS byte stream.

    The left margin is already part of the composed lines. The printer
    therefore keeps the default left alignment, and the margin is the same
    on every printer, in text and in bytes.
    """
    out = [ESC + b"\x40",            # initialize
           ESC + b"\x32",            # default line spacing
           ESC + b"\x74" + bytes([16]),  # code page WPC1252
           ESC + b"\x61" + bytes([0])]  # left alignment
    current = None
    for text, style in receipt.items:
        key = (style["bold"], style["size"])
        if key != current:
            out.append(ESC + b"\x45" + bytes([1 if style["bold"] else 0]))
            size = 0x11 if style["size"] == 2 else 0x00
            out.append(GS + b"\x21" + bytes([size]))
            current = key
        line = receipt.compose(text, style)
        out.append(line.encode("cp1252", "replace") + b"\x0a")
    if receipt.barcode:
        out.append(ESC + b"\x61" + bytes([1]))       # center the barcode
        out.append(escpos_barcode(receipt.barcode))
        out.append(b"\x0a")
    out.append(ESC + b"\x64" + bytes([3]))           # feed 3 lines
    out.append(GS + b"\x56" + bytes([0x42, 0x00]))    # partial cut
    return b"".join(out)


def format_qty(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if value == int(value):
        return str(int(value))
    return ("%.2f" % value).rstrip("0").rstrip(".")


def format_money(value, currency, decimal_comma=False):
    text = "%.2f" % (float(value or 0.0))
    if decimal_comma:
        text = text.replace(".", ",")
    symbol = (currency or {}).get("symbol") or ""
    position = (currency or {}).get("position") or "after"
    if not symbol:
        return text
    if position == "before":
        return "%s %s" % (symbol, text)
    return "%s %s" % (text, symbol)


def format_date(value, tz_name):
    """Format an Odoo date (UTC) in the local time zone."""
    text = str(value or "")
    if "." in text:
        text = text.split(".", 1)[0]
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
        if tz_name and ZoneInfo:
            dt = dt.astimezone(ZoneInfo(tz_name))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def build_receipt(order, lines, variants, custom_ptav, currency, cfg):
    """Compose the receipt for one order."""
    rc = cfg["receipt"]
    width = rc.get("width") or 48
    comma = bool(rc.get("decimal_comma"))
    receipt = Receipt(width, rc.get("left_margin") or 0)
    # Shop header
    if rc.get("shop_name"):
        receipt.add(rc["shop_name"], bold=True, align="center", size=2)
    for text in rc.get("shop_address_lines") or []:
        receipt.add(text, align="center")
    if rc.get("shop_phone"):
        receipt.add("Tel: " + str(rc["shop_phone"]), align="center")
    if rc.get("shop_name") or rc.get("shop_address_lines"):
        receipt.rule()
    # Order header
    receipt.row("Order", order.get("name") or "")
    receipt.row("Date", format_date(order.get("date_order"),
                                    rc.get("timezone")))
    partner = order.get("partner_id")
    if partner:
        receipt.add("Customer: " + str(partner[1]), hang="  ")
    receipt.rule()
    # Order lines
    for line in lines:
        display_type = line.get("display_type") or False
        if display_type:
            if display_type == "line_note" and rc.get("show_notes"):
                receipt.add(line.get("name") or "", hang="  ")
            continue
        if rc.get("price_mode") == "subtotal":
            price = line.get("price_subtotal") or 0.0
        else:
            price = line.get("price_total") or 0.0
        product = line.get("product_id")
        if product:
            variant = variants.get(product[0]) or {}
            name = variant.get("name") or str(product[1])
            qty = format_qty(line.get("product_uom_qty") or 0)
            receipt.row("%s x %s" % (qty, name),
                        format_money(price, currency, comma))
            for label in variant.get("attributes") or []:
                receipt.add("    " + label, hang="    ")
            for pid in ((line.get("custom_values") or [])
                    + (line.get("no_variant_values") or [])):
                rec = custom_ptav.get(pid)
                if not rec:
                    continue
                attr = ""
                if isinstance(rec.get("attribute_id"), (list, tuple)):
                    attr = rec["attribute_id"][1] or ""
                value = rec.get("custom_value") or rec.get("name") or ""
                label = ("%s: %s" % (attr, value)) if attr else str(value)
                receipt.add("    " + label, hang="    ")
            if rc.get("show_unit_price"):
                receipt.add("    Unit price: " + format_money(
                    line.get("price_unit") or 0.0, currency, comma),
                    hang="    ")
        else:
            # A line without a product. Odoo stores free text in 'name'.
            receipt.row(line.get("name") or "",
                        format_money(price, currency, comma))
    # Totals
    receipt.rule()
    receipt.row("Subtotal", format_money(order.get("amount_untaxed"),
                                          currency, comma))
    receipt.row("Tax", format_money(order.get("amount_tax"),
                                    currency, comma))
    receipt.row("TOTAL", format_money(order.get("amount_total"),
                                       currency, comma), bold=True)
    # Barcode and footer
    if rc.get("show_barcode") and order.get("name"):
        receipt.barcode = order["name"]
    footer = rc.get("footer_lines") or []
    if footer:
        receipt.rule()
        for text in footer:
            receipt.add(text, align="center")
    return receipt


def build_internal_receipt(order, lines, variants, custom_ptav, cfg):
    """Compose the packing list for one order. For internal use.

    The order number is at the top. Each item has a square that the packer
    can tick with a pen. The variant labels sit under the item line.
    """
    rc = cfg["receipt"]
    width = rc.get("width") or 48
    receipt = Receipt(width, rc.get("left_margin") or 0)
    # Header
    receipt.add("PACKING LIST", bold=True, align="center")
    receipt.add(order.get("name") or "", bold=True, align="center", size=2)
    receipt.row("Date", format_date(order.get("date_order"),
                                    rc.get("timezone")))
    partner = order.get("partner_id")
    if partner:
        receipt.add("Customer: " + str(partner[1]), hang="  ")
    receipt.rule()
    # One block per item. A blank line separates the blocks.
    total_qty = 0.0
    first = True
    for line in lines:
        display_type = line.get("display_type") or False
        if display_type:
            if display_type == "line_section":
                if not first:
                    receipt.add("")
                receipt.add(line.get("name") or "", bold=True)
                first = False
            elif display_type == "line_note" and rc.get("show_notes"):
                if not first:
                    receipt.add("")
                receipt.add(line.get("name") or "", hang="  ")
                first = False
            continue
        product = line.get("product_id")
        if not product:
            continue
        if not first:
            receipt.add("")
        first = False
        variant = variants.get(product[0]) or {}
        name = variant.get("name") or str(product[1])
        qty = format_qty(line.get("product_uom_qty") or 0)
        receipt.add("[ ] %s x %s" % (qty, name), bold=True, hang="    ")
        for label in variant.get("attributes") or []:
            receipt.add("    " + label, hang="    ")
        for pid in ((line.get("custom_values") or [])
                    + (line.get("no_variant_values") or [])):
            rec = custom_ptav.get(pid)
            if not rec:
                continue
            attr = ""
            if isinstance(rec.get("attribute_id"), (list, tuple)):
                attr = rec["attribute_id"][1] or ""
            value = rec.get("custom_value") or rec.get("name") or ""
            label = ("%s: %s" % (attr, value)) if attr else str(value)
            receipt.add("    " + label, hang="    ")
        try:
            total_qty += float(line.get("product_uom_qty") or 0)
        except (TypeError, ValueError):
            pass
    # Footer
    receipt.rule()
    receipt.row("Total items", format_qty(total_qty))
    receipt.add("Packed by:")
    receipt.add("")
    if rc.get("show_barcode") and order.get("name"):
        receipt.barcode = order["name"]
    return receipt


# --------------------------------------------------------------------------
# Printers
# --------------------------------------------------------------------------

class PrinterError(Exception):
    pass


class NetPrinter:
    """Raw TCP printer on port 9100. This transport is two-way."""

    def __init__(self, host, port=9100, timeout=5):
        self.host = host
        self.port = port
        self.timeout = timeout

    def send(self, data):
        with socket.create_connection((self.host, self.port),
                                      self.timeout) as sock:
            sock.settimeout(self.timeout)
            sock.sendall(data)

    def query(self, data, expect=4):
        chunks = b""
        try:
            with socket.create_connection((self.host, self.port),
                                          self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(data)
                deadline = time.time() + self.timeout
                while len(chunks) < expect and time.time() < deadline:
                    try:
                        part = sock.recv(expect - len(chunks))
                    except socket.timeout:
                        break
                    if not part:
                        break
                    chunks += part
        except OSError as exc:
            raise PrinterError("cannot reach %s:%s: %s" %
                               (self.host, self.port, exc)) from None
        return chunks


class DevPrinter:
    """Raw device file, for example /dev/usb/lp0 on Linux."""

    def __init__(self, path):
        self.path = path

    def send(self, data):
        try:
            with open(self.path, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            raise PrinterError("cannot write to %s: %s" % (self.path, exc))


class CupsPrinter:
    """CUPS queue in raw mode. CUPS must not filter the byte stream."""

    def __init__(self, queue):
        self.queue = queue

    def send(self, data):
        try:
            proc = subprocess.run(
                ["lp", "-d", self.queue, "-o", "raw"],
                input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30)
        except FileNotFoundError:
            raise PrinterError("the 'lp' command is not available")
        except subprocess.TimeoutExpired:
            raise PrinterError("lp did not finish in time")
        if proc.returncode != 0:
            raise PrinterError("lp failed: %s" %
                               proc.stderr.decode(errors="replace").strip())


class UsbPrinter:
    """Direct USB printer-class device. Needs the optional pyusb package."""

    def __init__(self, target):
        self.target = target or "auto"

    def _open(self):
        try:
            import usb.core
            import usb.util
        except ImportError:
            raise PrinterError(
                "pyusb is not installed. Run: pip install pyusb")
        dev = None
        if ":" in self.target:
            vid_text, pid_text = self.target.split(":", 1)
            try:
                vid = int(vid_text, 16)
                pid = int(pid_text, 16)
            except ValueError:
                raise PrinterError(
                    "bad USB target %r. Use hexadecimal VID:PID." %
                    self.target)
            dev = usb.core.find(idVendor=vid, idProduct=pid)
        else:
            for candidate in usb.core.find(find_all=True):
                try:
                    for config in candidate:
                        for intf in config:
                            if intf.bInterfaceClass == 7:
                                dev = candidate
                                break
                        if dev is not None:
                            break
                except (ValueError, NotImplementedError, usb.core.USBError):
                    continue
                if dev is not None:
                    break
        if dev is None:
            raise PrinterError("no USB printer found")
        try:
            dev.set_configuration()
        except (NotImplementedError, usb.core.USBError):
            pass  # The device may hold a configuration already.
        try:
            config = dev.get_active_configuration()
        except usb.core.USBError as exc:
            raise PrinterError("cannot read the USB configuration: %s" % exc)
        intf = None
        for candidate in config:
            if candidate.bInterfaceClass == 7:
                intf = candidate
                break
        if intf is None:
            raise PrinterError("the device has no printer-class interface")
        out_ep = None
        in_ep = None
        for ep in intf:
            if ep.bEndpointAddress & 0x80:
                in_ep = ep
            else:
                out_ep = ep
        if out_ep is None:
            raise PrinterError("the printer interface has no OUT endpoint")
        return dev, out_ep, in_ep

    def send(self, data):
        dev, out_ep, in_ep = self._open()
        try:
            dev.write(out_ep.bEndpointAddress, data)
        except Exception as exc:
            raise PrinterError("USB write failed: %s" % exc)
        finally:
            import usb.util
            usb.util.dispose_resources(dev)

    def query(self, data, expect=4):
        dev, out_ep, in_ep = self._open()
        try:
            dev.write(out_ep.bEndpointAddress, data)
            if in_ep is None:
                return b""
            try:
                return bytes(dev.read(in_ep.bEndpointAddress, expect, 2000))
            except Exception:
                return b""
        finally:
            import usb.util
            usb.util.dispose_resources(dev)


class WinPrinter:
    """Windows spooler in RAW mode. No extra package is needed."""

    def __init__(self, name):
        self.name = name

    def send(self, data):
        try:
            import ctypes
            from ctypes import wintypes
        except (ImportError, ValueError):
            raise PrinterError(
                "the Windows spooler is not available on this system")
        if not hasattr(ctypes, "WinDLL"):
            raise PrinterError("the Windows spooler is not available here")

        class DOC_INFO_1(ctypes.Structure):
            _fields_ = [("pDocName", ctypes.c_wchar_p),
                        ("pOutputFile", ctypes.c_wchar_p),
                        ("pDatatype", ctypes.c_wchar_p)]

        winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)
        winspool.OpenPrinterW.argtypes = [
            ctypes.c_wchar_p, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
        winspool.OpenPrinterW.restype = wintypes.BOOL
        winspool.StartDocPrinterW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
        winspool.StartDocPrinterW.restype = wintypes.BOOL
        winspool.WritePrinter.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        winspool.WritePrinter.restype = wintypes.BOOL
        winspool.EndPagePrinter.argtypes = [wintypes.HANDLE]
        winspool.EndDocPrinter.argtypes = [wintypes.HANDLE]
        winspool.ClosePrinter.argtypes = [wintypes.HANDLE]

        handle = wintypes.HANDLE()
        if not winspool.OpenPrinterW(self.name, ctypes.byref(handle), None):
            raise PrinterError("cannot open the Windows printer %r" %
                               self.name)
        try:
            doc = DOC_INFO_1("odoo_receipt", None, "RAW")
            if not winspool.StartDocPrinterW(handle, 1, ctypes.byref(doc)):
                raise PrinterError("StartDocPrinter failed")
            winspool.StartPagePrinter(handle)
            written = wintypes.DWORD(0)
            buf = ctypes.create_string_buffer(data, len(data))
            ok = winspool.WritePrinter(handle, buf, len(data),
                                       ctypes.byref(written))
            winspool.EndPagePrinter(handle)
            winspool.EndDocPrinter(handle)
            if not ok:
                raise PrinterError("WritePrinter failed")
        finally:
            winspool.ClosePrinter(handle)


class NullPrinter:
    """Stands in when no printer is configured."""

    def send(self, data):
        raise PrinterError(
            "no printer configured. Set printer.transport and "
            "printer.target in the config file.")


def open_printer(cfg):
    printer_cfg = cfg.get("printer") or {}
    transport = (printer_cfg.get("transport") or "none").lower()
    target = printer_cfg.get("target") or ""
    timeout = printer_cfg.get("timeout") or 5
    if transport == "none":
        return NullPrinter()
    if transport == "net":
        host, _, port = target.partition(":")
        if not host:
            raise PrinterError(
                "printer.target must contain the printer host, "
                "for example 192.168.1.50:9100")
        return NetPrinter(host, int(port or 9100), timeout)
    if transport == "usb":
        return UsbPrinter(target)
    if transport == "dev":
        if not target:
            raise PrinterError(
                "printer.target must be a device path, "
                "for example /dev/usb/lp0")
        return DevPrinter(target)
    if transport == "cups":
        if not target:
            raise PrinterError("printer.target must be a CUPS queue name")
        return CupsPrinter(target)
    if transport == "win":
        if not target:
            raise PrinterError("printer.target must be the Windows printer "
                               "name. Install the Generic / Text Only "
                               "driver and set the port to RAW.")
        return WinPrinter(target)
    raise PrinterError("unknown printer transport: %r" % transport)


# --------------------------------------------------------------------------
# State for the poll command
# --------------------------------------------------------------------------

class StateStore:
    """Store the poll watermark and the printed order names on disk."""

    def __init__(self, path):
        self.path = path
        self.data = {"printed": {}, "watermark": None}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self.data = data
                self.data.setdefault("printed", {})
                self.data.setdefault("watermark", None)
        except (OSError, ValueError):
            pass

    def save(self):
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)

    def mark_printed(self, order, advance=True):
        now = datetime.now(timezone.utc).isoformat()
        self.data["printed"][order["name"]] = now
        if advance:
            watermark = self.data.get("watermark") or 0
            self.data["watermark"] = max(watermark, order["id"])
        printed = self.data.get("printed") or {}
        if len(printed) > 500:
            keep = sorted(printed.items(), key=lambda kv: kv[1])[-500:]
            self.data["printed"] = dict(keep)
        self.save()


# --------------------------------------------------------------------------
# Printing an order
# --------------------------------------------------------------------------

def print_order(client, cfg, printer, order, text_only=False, dry_run=False,
                internal=False):
    """Fetch the order data, build the receipt, and print or show it.

    Set internal to True for the packing list instead of the customer
    receipt.
    """
    lines = fetch_lines(client, order)
    variants, custom_ptav = fetch_variants(client, lines)
    currency = fetch_currency(client, order)
    if internal:
        receipt = build_internal_receipt(order, lines, variants, custom_ptav,
                                         cfg)
    else:
        receipt = build_receipt(order, lines, variants, custom_ptav,
                                currency, cfg)
    if text_only:
        print(receipt.render_text())
        return
    data = escpos_bytes(receipt)
    if dry_run:
        print(receipt.render_text())
        print("")
        print("ESC/POS bytes (%d):" % len(data))
        print(data.hex(" "))
        return
    if printer is None:
        raise PrinterError("no printer configured")
    printer.send(data)


def internal_mode(cfg):
    """Read the internal_receipts setting. One of off, add, or only."""
    mode = (cfg.get("receipt") or {}).get("internal_receipts") or "off"
    return mode if mode in ("off", "add", "only") else "off"


def print_with_mode(client, cfg, printer, order, mode,
                    text_only=False, dry_run=False):
    """Print one order, following the internal_receipts setting.

    off  : the customer receipt only.
    add  : the customer receipt, then the packing list.
    only : the packing list only.
    """
    if mode == "only":
        print_order(client, cfg, printer, order, text_only=text_only,
                    dry_run=dry_run, internal=True)
        return
    print_order(client, cfg, printer, order, text_only=text_only,
                dry_run=dry_run)
    if mode == "add":
        print_order(client, cfg, printer, order, text_only=text_only,
                    dry_run=dry_run, internal=True)


def poll_cycle(client, cfg, state, printer, dry_run=False):
    """Print the receipts for all new orders. Return the number printed."""
    domain = ecommerce_domain(cfg.get("states"), cfg.get("only_website"))
    watermark = state.data.get("watermark")
    if watermark is not None:
        domain.append(("id", ">", watermark))
    orders = client.search_read("sale.order", domain, ORDER_FIELDS,
                                limit=cfg.get("batch", 50), order="id asc")
    if watermark is None:
        # First run. Record the current state. Print nothing. This stops the
        # script from printing the full order history at once.
        top = max([o["id"] for o in orders], default=0)
        state.data["watermark"] = top
        state.save()
        print("First run. The watermark is set to order id %s. "
              "New orders print from now on." % top)
        return 0
    mode = internal_mode(cfg)
    count = 0
    for order in orders:
        if order["name"] in state.data["printed"]:
            continue
        print_with_mode(client, cfg, printer, order, mode, dry_run=dry_run)
        state.mark_printed(order, advance=True)
        print("Printed the receipt for %s" % order["name"])
        count += 1
    return count


# --------------------------------------------------------------------------
# Local bridge for the browser button
# --------------------------------------------------------------------------

BRIDGE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Odoo Receipt Bridge</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body { font-family: sans-serif; max-width: 46rem; margin: 2rem auto;
        padding: 0 1rem; color: #222; }
 h1 { font-size: 1.4rem; }
 h2 { font-size: 1.1rem; margin-top: 2rem; }
 input[type=text] { font-size: 1rem; padding: .35rem; width: 9rem; }
 button { font-size: .95rem; padding: .4rem .9rem; cursor: pointer; }
 table { border-collapse: collapse; width: 100%; }
 th, td { text-align: left; padding: .45rem .6rem; border-bottom:
          1px solid #ddd; }
 th { border-bottom: 2px solid #999; }
 td.num { text-align: right; }
 td.actions { white-space: nowrap; text-align: right; }
 td.actions button { padding: .3rem .6rem; }
 tr.printed .name { color: #888; }
 tr.printed td:first-child::after { content: " ✓"; color: #2a7; }
 tr.detail > td { background: #f6f6f6; }
 tr.detail div.item { padding: .1rem 0; }
 tr.detail div.vlabel { color: #555; font-size: .9rem; }
 tr.detail button { padding: .2rem .5rem; font-size: .85rem; }
 .done { color: #2a7; }
 select { font-size: .95rem; padding: .3rem; }
 #status { min-height: 1.2rem; }
 #refresh { margin-left: .5rem; }
 label.toggle { font-size: .95rem; margin-left: .8rem; }
</style>
</head>
<body>
<h1>Odoo Receipt Bridge</h1>
<p id="status"></p>
<h2>Recent orders</h2>
<p>
<button id="refresh" type="button">Refresh</button>
<label class="toggle">Show
<select id="limit">
<option value="20">20</option>
<option value="50" selected>50</option>
<option value="100">100</option>
<option value="200">200</option>
</select></label>
<label class="toggle">State
<select id="state">
<option value="">From config</option>
<option value="draft">draft</option>
<option value="sent">sent</option>
<option value="sale">sale</option>
<option value="done">done</option>
<option value="cancel">cancel</option>
</select></label>
<label class="toggle"><input type="checkbox" id="website" checked>
Website only</label>
<span id="hint">Loading orders ...</span>
</p>
<table id="orders">
<thead><tr><th>Order</th><th>Date</th><th>State</th><th>Total</th>
<th>Customer</th><th>Printed</th><th></th></tr></thead>
<tbody></tbody>
</table>
<h2>Print by reference</h2>
<p>Type an order name or id. Then press Print.</p>
<input type="text" id="ref" placeholder="S00042">
<label><input type="checkbox" id="internal"> Packing list</label>
<button id="print" type="button">Print</button>
<script>
var token = "__TOKEN__";
var statusEl = document.getElementById("status");
var tbody = document.querySelector("#orders tbody");
var hint = document.getElementById("hint");
function setStatus(text) { statusEl.textContent = text; }
function printRef(ref, internal, rowStatus) {
  var where = rowStatus || statusEl;
  where.textContent = "Printing " + ref + " ...";
  return fetch("/print", {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Print-Token": token},
    body: JSON.stringify({ref: ref, internal: internal})
  }).then(function (r) { return r.json(); }).then(function (data) {
    where.textContent = data.ok ? "Printed " + data.order + "."
                                : "Failed: " + data.error;
    if (data.ok) { loadOrders(); }
  }).catch(function (err) { where.textContent = "Failed: " + err; });
}
var openDetails = {};
function toggleDetail(ref, btn, tr) {
  if (openDetails[ref]) {
    var old = openDetails[ref];
    delete openDetails[ref];
    if (old.row) { old.row.remove(); }
    btn.textContent = "Details";
    return;
  }
  var row = document.createElement("tr");
  row.className = "detail";
  var td = document.createElement("td");
  td.colSpan = 7;
  td.textContent = "Loading " + ref + " ...";
  row.appendChild(td);
  tr.after(row);
  openDetails[ref] = {row: row};
  btn.textContent = "Hide";
  fetch("/order?ref=" + encodeURIComponent(ref),
         {headers: {"X-Print-Token": token}})
    .then(function (r) { return r.json(); })
    .then(function (data) {
      td.replaceChildren();
      if (!data.ok) {
        td.textContent = "Failed: " + data.error;
        return;
      }
      if (!data.items || !data.items.length) {
        td.textContent = "No items on this order.";
        return;
      }
      data.items.forEach(function (item) {
        var box = document.createElement("div");
        box.className = "item";
        var b = document.createElement("b");
        b.textContent = item.qty + " x " + item.product;
        box.appendChild(b);
        (item.variants || []).forEach(function (label) {
          var div = document.createElement("div");
          div.className = "vlabel";
          div.textContent = "\u2022 " + label;
          box.appendChild(div);
        });
        td.appendChild(box);
      });
    })
    .catch(function (err) { td.textContent = "Failed: " + err; });
}
function cell(row, text, cls) {
  var td = document.createElement("td");
  td.textContent = text == null ? "" : text;
  if (cls) { td.className = cls; }
  row.appendChild(td);
  return td;
}
function addOrder(order) {
  var tr = document.createElement("tr");
  if (order.printed) { tr.className = "printed"; }
  var nameCell = cell(tr, order.name);
  nameCell.className = "name";
  cell(tr, (order.date_order || "").slice(0, 16).replace("T", " "));
  cell(tr, order.state);
  cell(tr, order.amount_total == null ? "" : order.amount_total.toFixed(2),
       "num");
  cell(tr, order.partner_id || "");
  var printedCell = cell(tr, order.printed ? "yes" : "no");
  printedCell.className = order.printed ? "done" : "";
  var actions = document.createElement("td");
  actions.className = "actions";
  var msg = document.createElement("span");
  var detailBtn = document.createElement("button");
  detailBtn.type = "button";
  detailBtn.textContent = "Details";
  detailBtn.addEventListener("click", function () {
    toggleDetail(order.name, detailBtn, detailBtn.closest("tr"));
  });
  var receiptBtn = document.createElement("button");
  receiptBtn.type = "button";
  receiptBtn.textContent = "Receipt";
  receiptBtn.addEventListener("click", function () {
    printRef(order.name, false, msg);
  });
  var packingBtn = document.createElement("button");
  packingBtn.type = "button";
  packingBtn.textContent = "Packing list";
  packingBtn.addEventListener("click", function () {
    printRef(order.name, true, msg);
  });
  actions.appendChild(detailBtn);
  actions.appendChild(document.createTextNode(" "));
  actions.appendChild(receiptBtn);
  actions.appendChild(document.createTextNode(" "));
  actions.appendChild(packingBtn);
  actions.appendChild(document.createTextNode(" "));
  actions.appendChild(msg);
  tr.appendChild(actions);
  tbody.appendChild(tr);
}
function loadOrders() {
  hint.textContent = "Loading orders ...";
  tbody.replaceChildren();
  var params = new URLSearchParams();
  params.set("limit", document.getElementById("limit").value);
  var state = document.getElementById("state").value;
  if (state) { params.set("state", state); }
  if (!document.getElementById("website").checked) {
    params.set("website", "0");
  }
  fetch("/orders?" + params.toString(),
         {headers: {"X-Print-Token": token}})
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (!data.ok) {
        hint.textContent = "Failed: " + data.error;
        return;
      }
      hint.textContent = "";
      (data.orders || []).forEach(addOrder);
      if (!data.orders || !data.orders.length) {
        hint.textContent = "No orders found.";
      }
    })
    .catch(function (err) { hint.textContent = "Failed: " + err; });
}
document.getElementById("refresh").addEventListener("click", loadOrders);
document.getElementById("limit").addEventListener("change", loadOrders);
document.getElementById("state").addEventListener("change", loadOrders);
document.getElementById("website").addEventListener("change", loadOrders);
document.getElementById("print").addEventListener("click", function () {
  var ref = document.getElementById("ref").value.trim();
  if (!ref) {
    setStatus("Type an order name or id first.");
    return;
  }
  printRef(ref, document.getElementById("internal").checked, null);
});
loadOrders();
</script>
</body>
</html>
"""


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for the local bridge.

    The browser button or the bridge page sends the order reference. The
    bridge fetches the order from Odoo and prints the receipt.
    """

    server_version = "odoo_receipt/" + VERSION
    bridge_client = None
    bridge_cfg = None
    bridge_printer = None
    bridge_state = None
    bridge_token = ""
    bridge_page = ""

    def log_message(self, fmt, *log_args):
        print("[bridge] " + (fmt % log_args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Print-Token")

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/order":
            if self.headers.get("X-Print-Token") != self.bridge_token:
                self._json(403, {"ok": False,
                                 "error": "bad or missing token"})
                return
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            ref = str((query.get("ref") or [""])[0]).strip()
            if not ref:
                self._json(400, {"ok": False, "error": "no order given"})
                return
            try:
                order = fetch_order(self.bridge_client, ref)
                lines = fetch_lines(self.bridge_client, order)
                variants, custom_ptav = fetch_variants(self.bridge_client,
                                                      lines)
                items = []
                for line in lines:
                    if line.get("display_type"):
                        continue
                    product = line.get("product_id")
                    if not product:
                        continue
                    items.append({
                        "name": line.get("name") or product[1],
                        "product": product[1],
                        "qty": line.get("product_uom_qty") or 0,
                        "variants": line_variant_labels(line, variants,
                                                        custom_ptav),
                    })
            except (OdooError, PrinterError) as exc:
                self._json(500, {"ok": False, "error": str(exc)})
                return
            self._json(200, {"ok": True, "order": order.get("name"),
                             "items": items})
            return
        if path == "/orders":
            if self.headers.get("X-Print-Token") != self.bridge_token:
                self._json(403, {"ok": False,
                                 "error": "bad or missing token"})
                return
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query)
            states = None
            if query.get("state"):
                states = [s for s in query["state"][0].split(",") if s]
            limit = 50
            if query.get("limit"):
                try:
                    limit = max(1, min(int(query["limit"][0]), 500))
                except ValueError:
                    self._json(400, {"ok": False,
                                     "error": "limit must be a number"})
                    return
            only_website = self.bridge_cfg.get("only_website")
            if "website" in query:
                only_website = query["website"][0] not in ("0", "false")
            try:
                domain = ecommerce_domain(states or
                                          self.bridge_cfg.get("states"),
                                          only_website)
                orders = fetch_orders(self.bridge_client, domain, limit=limit)
            except OdooError as exc:
                self._json(500, {"ok": False, "error": str(exc)})
                return
            if self.bridge_state is not None:
                self.bridge_state.load()
                printed = self.bridge_state.data.get("printed") or {}
            else:
                printed = {}
            self._json(200, {
                "ok": True,
                "orders": [{"id": o.get("id"),
                            "name": o.get("name"),
                            "state": o.get("state"),
                            "date_order": o.get("date_order"),
                            "amount_total": o.get("amount_total"),
                            "currency": ((o.get("currency_id") or ["", ""])[1]
                                          if o.get("currency_id") else ""),
                            "partner_id": ((o.get("partner_id") or ["", ""])[1]
                                            if o.get("partner_id") else ""),
                            "printed": o.get("name") in printed}
                           for o in orders]})
            return
        if path == "/":
            body = self.bridge_page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/health":
            self._json(200, {"ok": True, "version": VERSION})
        else:
            self._json(404, {"ok": False, "error": "unknown path"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/print":
            self._json(404, {"ok": False, "error": "unknown path"})
            return
        if self.headers.get("X-Print-Token") != self.bridge_token:
            self._json(403, {"ok": False, "error": "bad or missing token"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(
                self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError:
            self._json(400, {"ok": False, "error": "the body is not JSON"})
            return
        ref = str(payload.get("ref") or payload.get("order") or "").strip()
        if not ref:
            self._json(400, {"ok": False, "error": "no order given"})
            return
        try:
            order = fetch_order(self.bridge_client, ref)
            mode = internal_mode(self.bridge_cfg)
            if "internal" in payload:
                mode = "only" if payload.get("internal") else "off"
            print_with_mode(self.bridge_client, self.bridge_cfg,
                            self.bridge_printer, order, mode)
            if self.bridge_state is not None:
                self.bridge_state.mark_printed(order, advance=False)
        except (OdooError, PrinterError) as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return
        self._json(200, {"ok": True, "order": order["name"]})


def cmd_serve(cfg, args):
    """Run the local HTTP bridge for the button in the Odoo web client.

    A browser userscript inside Odoo sends the order reference to this
    bridge. The bridge listens on 127.0.0.1 only. A token in the request
    header stops other websites from triggering prints.
    """
    bridge = cfg.get("bridge") or {}
    token = str(bridge.get("token") or "").strip()
    if not token:
        # Write a token to the config file. The token stays the same
        # across restarts, so the browser button needs no update.
        token = secrets.token_hex(16)
        cfg.setdefault("bridge", {})
        cfg["bridge"]["token"] = token
        folder = os.path.dirname(args.config)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.config, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=2)
        print("The bridge token was written to the config file.")
    BridgeHandler.bridge_client = make_client(cfg)
    BridgeHandler.bridge_cfg = cfg
    BridgeHandler.bridge_printer = open_printer(cfg)
    BridgeHandler.bridge_state = StateStore(cfg["state_path"])
    BridgeHandler.bridge_token = token
    BridgeHandler.bridge_page = BRIDGE_PAGE.replace("__TOKEN__", token)
    port = args.port or bridge.get("port") or 8765
    bind = args.bind or bridge.get("bind") or "127.0.0.1"
    try:
        httpd = http.server.ThreadingHTTPServer((bind, port),
                                                BridgeHandler)
    except OSError as exc:
        raise PrinterError("cannot bind %s:%s: %s" % (bind, port, exc))
    print("The bridge listens on http://%s:%s" %
          (bind, httpd.server_address[1]))
    print("Bridge token: %s" % token)
    print("Open http://%s:%s/ to print without the browser button." %
          (bind, httpd.server_address[1]))
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    return 0


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_init(cfg, args):
    path = args.config
    if os.path.exists(path):
        print("The config file exists already: %s" % path)
        return 1
    skeleton = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.environ.get("ODOO_STATE_PATH"):
        skeleton["state_path"] = os.environ["ODOO_STATE_PATH"]
    skeleton["url"] = "https://YOURSHOP.odoo.com"
    skeleton["db"] = "YOUR_DATABASE_NAME"
    skeleton["user"] = "you@example.com"
    skeleton["api_key"] = "YOUR_API_KEY"
    skeleton["printer"] = {"transport": "net",
                            "target": "192.168.1.50:9100",
                            "timeout": 5}
    skeleton["receipt"]["shop_name"] = "My Shop"
    skeleton["receipt"]["shop_address_lines"] = ["Street and number",
                                                 "1000 Brussels"]
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(skeleton, handle, indent=2)
    print("Wrote the config file: %s" % path)
    print("1. Edit the file. Fill in db, user, api_key, and the printer.")
    print("2. Run: python odoo_receipt.py check")
    return 0


def cmd_check(cfg, args):
    if cfg.get("api") == "jsonrpc":
        missing = [key for key in ("url", "db", "user", "api_key")
                   if not cfg.get(key)]
        if missing:
            print("Missing settings: %s" % ", ".join(missing))
            return 1
    elif not cfg.get("url") or not cfg.get("api_key"):
        print("Missing settings: url, api_key")
        return 1
    client = make_client(cfg)
    if cfg.get("api") == "jsonrpc":
        try:
            version = client._call_jsonrpc("common", "version", [])
            server = (version or {}).get("server_version")
            print("Server version: %s" % server)
        except OdooError as exc:
            print("The server does not answer: %s" % exc)
            return 1
    uid = client.authenticate()
    print("Login OK. User id: %s" % uid)
    try:
        client.search_read("sale.order", [], ["name"], limit=1)
        print("Read access to sale.order: OK")
    except OdooError as exc:
        print("No read access to sale.order: %s" % exc)
        return 1
    try:
        client.search_read("sale.order", [("website_id", "!=", False)],
                           ["name"], limit=1)
        print("Website field on sale.order: OK")
    except OdooError as exc:
        print("The field 'website_id' is not readable. Install the "
              "eCommerce app, or give the API user more rights. "
              "Details: %s" % exc)
    try:
        client.search_read("sale.order.line", [], ["product_id"], limit=1)
        print("Read access to sale.order.line: OK")
    except OdooError as exc:
        print("No read access to sale.order.line: %s" % exc)
    try:
        client.search_read("product.product", [], ["name"], limit=1)
        print("Read access to product.product: OK")
    except OdooError as exc:
        print("No read access to product.product: %s" % exc)
    printer_cfg = cfg.get("printer") or {}
    transport = printer_cfg.get("transport") or "none"
    print("Printer transport: %s (target: %s)" %
          (transport, printer_cfg.get("target") or "-"))
    if transport == "net":
        try:
            printer = open_printer(cfg)
            printer.query(b"\x10\x04\x01", 4)
            print("Printer connection: OK")
        except PrinterError as exc:
            print("Printer connection failed: %s" % exc)
            return 1
    return 0


def cmd_orders(cfg, args):
    client = make_client(cfg)
    domain = []
    if not args.all:
        domain.append(("website_id", "!=", False))
    if args.states:
        states = [s.strip() for s in args.states.split(",") if s.strip()]
        domain.append(("state", "in", states))
    orders = fetch_orders(client, domain, limit=args.limit)
    if not orders:
        print("No orders found.")
        return 0
    print("%-12s %-17s %-8s %10s  %s" %
          ("Order", "Date", "State", "Total", "Customer"))
    for order in orders:
        date = (order.get("date_order") or "")[:16]
        total = "%.2f" % (order.get("amount_total") or 0.0)
        customer = ""
        if order.get("partner_id"):
            customer = order["partner_id"][1]
        print("%-12s %-17s %-8s %10s  %s" %
              (order["name"], date, order.get("state") or "", total, customer))
    return 0


def cmd_print(cfg, args):
    client = make_client(cfg)
    text_only = bool(getattr(args, "text", False))
    printer = None
    if not text_only and not args.dry_run:
        printer = open_printer(cfg)
    state = None
    if not args.dry_run:
        state = StateStore(cfg["state_path"])
    mode = "only" if getattr(args, "internal", False) else internal_mode(cfg)
    for ref in args.names:
        order = fetch_order(client, ref)
        print_with_mode(client, cfg, printer, order, mode,
                        text_only=text_only, dry_run=args.dry_run)
        # Record the print. The poll command will not print the same order
        # again. The watermark stays unchanged.
        if state is not None and not text_only:
            state.mark_printed(order, advance=False)
    return 0


def cmd_poll(cfg, args):
    client = make_client(cfg)
    printer = None if args.dry_run else open_printer(cfg)
    state = StateStore(cfg["state_path"])
    if args.backfill:
        domain = ecommerce_domain(cfg.get("states"),
                                  cfg.get("only_website"))
        orders = client.search_read("sale.order", domain, ORDER_FIELDS,
                                    limit=args.backfill, order="id desc")
        for order in reversed(orders):
            if order["name"] in state.data["printed"]:
                continue
            print_with_mode(client, cfg, printer, order, internal_mode(cfg),
                            dry_run=args.dry_run)
            state.mark_printed(order, advance=True)
            print("Printed the receipt for %s" % order["name"])
        if args.once:
            return 0
    if args.once:
        poll_cycle(client, cfg, state, printer, dry_run=args.dry_run)
        return 0
    print("Polling every %d seconds. Press Ctrl+C to stop." % args.interval)
    while True:
        try:
            poll_cycle(client, cfg, state, printer, dry_run=args.dry_run)
        except (OdooError, PrinterError) as exc:
            print("Error: %s" % exc, file=sys.stderr)
            print("The poll continues with the next cycle.")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("Stopped.")
            return 0


def cmd_test(cfg, args):
    text_only = bool(getattr(args, "text", False))
    printer = None
    if not text_only and not args.dry_run:
        printer = open_printer(cfg)
    order = {
        "name": "TEST-001",
        "partner_id": (1, "Test Customer"),
        "date_order": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "state": "sale",
        "amount_untaxed": 50.0,
        "amount_tax": 8.5,
        "amount_total": 58.5,
        "currency_id": (1, "EUR"),
        "order_line": [1, 2],
    }
    lines = [
        {"id": 1, "product_id": (11, "�Cell BB"), "name": "�Cell BB",
         "product_uom_qty": 2, "price_unit": 25.0, "price_subtotal": 50.0,
         "price_total": 50.0, "display_type": False, "custom_values": []},
        {"id": 2, "product_id": (12, "�Cell BB"), "name": "�Cell BB",
         "product_uom_qty": 1, "price_unit": 0.0, "price_subtotal": 0.0,
         "price_total": 0.0, "display_type": False, "custom_values": []},
    ]
    variants = {
        11: {"name": "�Cell BB",
             "attributes": ["40-p Header", "Kit"]},
        12: {"name": "�Cell BB",
             "attributes": ["Accessory Header", "Soldered"]},
    }
    currency = {"symbol": "EUR", "position": "after"}
    if getattr(args, "internal", False):
        receipt = build_internal_receipt(order, lines, variants, {}, cfg)
    else:
        receipt = build_receipt(order, lines, variants, {}, currency, cfg)
    if text_only:
        print(receipt.render_text())
        return 0
    data = escpos_bytes(receipt)
    if args.dry_run:
        print(receipt.render_text())
        print("")
        print("ESC/POS bytes (%d):" % len(data))
        print(data.hex(" "))
        return 0
    printer.send(data)
    print("Sent the test receipt to the printer.")
    return 0


def cmd_status(cfg, args):
    printer = open_printer(cfg)
    if not hasattr(printer, "query"):
        print("This transport cannot read the printer status. "
              "Use the 'net' or 'usb' transport.")
        return 1
    probes = [("printer", b"\x10\x04\x01"),
              ("offline cause", b"\x10\x04\x02"),
              ("error", b"\x10\x04\x03"),
              ("paper", b"\x10\x04\x04")]
    for label, command in probes:
        raw = printer.query(command, 4)
        if not raw or len(raw) < 4:
            print("%-13s: no response" % label)
            continue
        status = raw[-1]
        note = ""
        if command[2] == 4:
            if status & 0x60:
                note = "  paper end or missing"
            elif status & 0x0C:
                note = "  paper near end"
            else:
                note = "  paper OK"
        print("%-13s: %s%s" % (label, raw.hex(" "), note))
    print("The raw bytes are authoritative. See the Epson ESC/POS "
          "reference for the bit meanings.")
    return 0


COMMANDS = {
    "init": cmd_init,
    "check": cmd_check,
    "orders": cmd_orders,
    "print": cmd_print,
    "poll": cmd_poll,
    "test": cmd_test,
    "status": cmd_status,
    "serve": cmd_serve,
}


# --------------------------------------------------------------------------
# Config and command line
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "url": "",
    "db": "",
    "user": "",
    "api_key": "",
    "api": "jsonrpc",
    "timeout": 30,
    "only_website": True,
    "states": ["sale", "done"],
    "batch": 50,
    "state_path": DEFAULT_STATE_PATH,
    "printer": {"transport": "none", "target": "", "timeout": 5},
    "bridge": {"port": 8765, "bind": "127.0.0.1", "token": ""},
    "receipt": {
        "width": 48,
        "left_margin": 0,
        "shop_name": "",
        "shop_address_lines": [],
        "shop_phone": "",
        "footer_lines": ["Thank you for your order."],
        "price_mode": "total",
        "show_unit_price": True,
        "show_notes": False,
        "show_barcode": False,
        "internal_receipts": "off",
        "decimal_comma": False,
        "timezone": "",
    },
}


def deep_merge(base, extra):
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                cfg = deep_merge(cfg, json.load(handle))
        except ValueError as exc:
            raise OdooError("the config file %s is not valid JSON: %s" %
                            (path, exc))
    env = {
        "url": os.environ.get("ODOO_URL"),
        "db": os.environ.get("ODOO_DB"),
        "user": os.environ.get("ODOO_USER") or os.environ.get("ODOO_LOGIN"),
        "api_key": (os.environ.get("ODOO_API_KEY")
                    or os.environ.get("ODOO_PASSWORD")),
    }
    for key, value in env.items():
        if value:
            cfg[key] = value
    if os.environ.get("PRINTER_TRANSPORT"):
        cfg["printer"]["transport"] = os.environ["PRINTER_TRANSPORT"]
    if os.environ.get("PRINTER_TARGET"):
        cfg["printer"]["target"] = os.environ["PRINTER_TARGET"]
    if os.environ.get("ODOO_STATE_PATH"):
        cfg["state_path"] = os.environ["ODOO_STATE_PATH"]
    return cfg


def apply_overrides(cfg, args):
    if getattr(args, "url", None):
        cfg["url"] = args.url
    if getattr(args, "db", None):
        cfg["db"] = args.db
    if getattr(args, "user", None):
        cfg["user"] = args.user
    if getattr(args, "api_key", None):
        cfg["api_key"] = args.api_key
    if getattr(args, "api", None):
        cfg["api"] = args.api
    if getattr(args, "transport", None):
        cfg["printer"]["transport"] = args.transport
    if getattr(args, "target", None):
        cfg["printer"]["target"] = args.target
    if getattr(args, "width", None):
        cfg["receipt"]["width"] = args.width
    if getattr(args, "margin", None) is not None:
        cfg["receipt"]["left_margin"] = args.margin


def build_parser():
    parser = argparse.ArgumentParser(
        prog="odoo_receipt",
        description="Fetch orders from an Odoo eCommerce store and print "
                    "receipts with the products and their variants on an "
                    "ESC/POS receipt printer.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help="path to the config file")
    parser.add_argument("--url", help="Odoo base URL, for example "
                                      "https://myshop.odoo.com")
    parser.add_argument("--db", help="database name")
    parser.add_argument("--user", help="login of the API user")
    parser.add_argument("--api-key", help="API key of the API user")
    parser.add_argument("--api", choices=["jsonrpc", "json2"], default=None,
                        help="API flavor. jsonrpc works on Odoo 14 to 20 "
                             "and on Odoo Online today. json2 serves "
                             "Odoo 19 and later (experimental).")
    parser.add_argument("--transport",
                        choices=["net", "usb", "dev", "cups", "win", "none"],
                        help="printer transport")
    parser.add_argument("--target",
                        help="printer target: HOST[:9100], VID:PID, "
                             "/dev/usb/lp0, CUPS queue, or Windows printer "
                             "name")
    parser.add_argument("--width", type=int,
                        help="receipt width in characters. 48 for 80 mm "
                             "paper, 32 for 58 mm paper")
    parser.add_argument("--margin", type=int,
                        help="left margin of the receipt in characters. "
                             "Use it when the print is cropped on the left")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the receipt and the raw bytes. "
                             "Print nothing.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="write a config file with defaults")

    sub.add_parser("check",
                   help="test the Odoo connection and the user rights")

    p = sub.add_parser("orders", help="list the most recent eCommerce orders")
    p.add_argument("--limit", type=int, default=10,
                   help="number of orders to show")
    p.add_argument("--states", help="comma separated states, for example "
                                    "sale,done")
    p.add_argument("--all", action="store_true",
                   help="list orders from all channels, not only the website")

    p = sub.add_parser("print", help="print the receipt for one or more orders")
    p.add_argument("names", nargs="+", metavar="ORDER",
                   help="order name, for example S00042, or a numeric id")
    p.add_argument("--text", action="store_true",
                   help="show the receipt as text. Print nothing.")
    p.add_argument("--internal", action="store_true",
                   help="print the packing list instead of the customer "
                        "receipt")

    p = sub.add_parser("poll", help="watch for new orders and print them")
    p.add_argument("--interval", type=int, default=30,
                   help="seconds between two polls")
    p.add_argument("--once", action="store_true",
                   help="run one poll cycle, then exit")
    p.add_argument("--backfill", type=int, metavar="N",
                   help="print the last N confirmed orders now")

    p = sub.add_parser("test", help="print a test receipt")
    p.add_argument("--text", action="store_true",
                   help="show the test receipt as text. Print nothing.")
    p.add_argument("--internal", action="store_true",
                   help="show or print the packing list instead")

    sub.add_parser("status",
                   help="read the printer status (net and usb transports)")

    p = sub.add_parser("serve",
                       help="run the local bridge for the button in Odoo")
    p.add_argument("--port", type=int, default=None,
                   help="port of the bridge (default 8765)")
    p.add_argument("--bind", default=None,
                   help="address to bind (default 127.0.0.1)")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        cfg = load_config(args.config)
        apply_overrides(cfg, args)
        handler = COMMANDS[args.command]
        return handler(cfg, args) or 0
    except (OdooError, PrinterError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
