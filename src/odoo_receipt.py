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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth
import web_pages

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
    # No-variant attribute values are chosen on the product page but they do
    # not create a product variant, so they are stored on the order line.
    # Read them in a separate call too, and ignore versions without them.
    extra_fields = []
    for field in ("custom_product_template_attribute_value_ids",
                  "product_custom_attribute_value_ids",
                  "no_variant_attribute_value_ids",
                  "product_no_variant_attribute_value_ids"):
        try:
            extra = client.read("sale.order.line", line_ids, [field])
        except OdooError:
            continue
        extra_fields.append((field, {rec["id"]: rec for rec in extra}))
    for line in lines:
        custom_values = []
        no_variant_values = []
        for field, by_id in extra_fields:
            rec = by_id.get(line["id"]) or {}
            if field in ("custom_product_template_attribute_value_ids",
                         "product_custom_attribute_value_ids"):
                # Odoo 17 and later: a one2many to records that point to
                # the attribute value. Follow the pointer.
                ids = rec.get(field) or []
                if field == "product_custom_attribute_value_ids" and ids:
                    recs = client.read("product.attribute.custom.value",
                                       ids,
                                       ["custom_product_template_"
                                        "attribute_value_id"])
                    ids = [r.get("custom_product_template_attribute_value_id")
                           for r in recs]
                custom_values += [i for i in ids if i]
            else:
                no_variant_values += rec.get(field) or []
        line["custom_values"] = custom_values
        line["no_variant_values"] = no_variant_values
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
                        ["name", "display_name", "product_tmpl_id",
                         "product_template_attribute_value_ids"])
    for prod in prods:
        variants[prod["id"]] = {
            "name": prod.get("name") or prod.get("display_name") or "",
            "template_name": "",
            "attributes": [],
        }
    tmpl_ids = sorted({prod["product_tmpl_id"][0] for prod in prods
                       if prod.get("product_tmpl_id")})
    if tmpl_ids:
        try:
            tmpls = {t["id"]: t for t in client.read(
                "product.template", tmpl_ids, ["name"])}
            for prod in prods:
                tmpl = tmpls.get(prod.get("product_tmpl_id", [0])[0]) or {}
                variants[prod["id"]]["template_name"] = tmpl.get("name") or ""
        except OdooError:
            pass
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
            name = (variant.get("template_name")
                    or variant.get("name") or str(product[1]))
            qty = format_qty(line.get("product_uom_qty") or 0)
            receipt.row("%s x %s" % (qty, name),
                        format_money(price, currency, comma))
            for label in variant.get("attributes") or []:
                receipt.add("    " + label, hang="    ")
            seen_labels = set(variant.get("attributes") or [])
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
                if label in seen_labels:
                    continue
                seen_labels.add(label)
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
        name = (variant.get("template_name")
                    or variant.get("name") or str(product[1]))
        qty = format_qty(line.get("product_uom_qty") or 0)
        receipt.add("[ ] %s x %s" % (qty, name), bold=True, hang="    ")
        for label in variant.get("attributes") or []:
            receipt.add("    " + label, hang="    ")
        seen_labels = set(variant.get("attributes") or [])
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
            if label in seen_labels:
                continue
            seen_labels.add(label)
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

    def probe(self, data=b"\x10\x04\x01", expect=4):
        return self.query(data, expect)

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
                self.data.setdefault("printed_internal", {})
                self.data.setdefault("watermark", None)
        except (OSError, ValueError):
            pass
        self.data.setdefault("printed", {})
        self.data.setdefault("printed_internal", {})
        self.data.setdefault("watermark", None)

    def save(self):
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)

    def _prune(self, names):
        if len(names) > 500:
            keep = sorted(names.items(), key=lambda kv: kv[1])[-500:]
            return dict(keep)
        return names

    def mark_printed(self, order, advance=True, kinds=("receipt",)):
        """Record the prints of one order. kinds is a subset of
        ("receipt", "internal")."""
        now = datetime.now(timezone.utc).isoformat()
        name = order["name"]
        if "receipt" in kinds:
            self.data["printed"][name] = now
        if "internal" in kinds:
            self.data["printed_internal"][name] = now
        if advance:
            watermark = self.data.get("watermark") or 0
            self.data["watermark"] = max(watermark, order["id"])
        self.data["printed"] = self._prune(self.data.get("printed") or {})
        self.data["printed_internal"] = self._prune(
            self.data.get("printed_internal") or {})
        self.save()

    def set_printed(self, name, kind, printed):
        """Set or clear the printed mark of one order by hand, from the
        bridge page. kind is "receipt" or "internal"."""
        key = "printed" if kind == "receipt" else "printed_internal"
        names = self.data.setdefault(key, {})
        if printed:
            names[name] = datetime.now(timezone.utc).isoformat()
        else:
            names.pop(name, None)
        self.data[key] = self._prune(names)
        self.save()

    def is_printed(self, name, kind="receipt"):
        key = "printed" if kind == "receipt" else "printed_internal"
        return name in (self.data.get(key) or {})


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


def mode_kinds(mode):
    """The print kinds that a print mode produces."""
    if mode == "only":
        return ("internal",)
    if mode == "add":
        return ("receipt", "internal")
    return ("receipt",)


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
        state.mark_printed(order, advance=True, kinds=mode_kinds(mode))
        print("Printed the receipt for %s" % order["name"])
        count += 1
    return count


# --------------------------------------------------------------------------
# Web interface: login, roles, settings, quickstart, orders
# --------------------------------------------------------------------------

def detect_printers(cfg):
    """Find ESC/POS printers on the LAN. The net transport is checked with
    an ESC/POS status query, so a printer replies only if it speaks the
    protocol."""
    found = []
    transport = ((cfg.get("printer") or {}).get("transport") or "").lower()
    timeout = (cfg.get("printer") or {}).get("timeout") or 5
    hosts = set()
    target = (cfg.get("printer") or {}).get("target") or ""
    host = target.partition(":")[0]
    if host:
        hosts.add(host)
    address = None
    try:
        probe_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe_sock.connect(("8.8.8.8", 80))
        address = probe_sock.getsockname()[0]
        probe_sock.close()
    except OSError:
        pass
    if address:
        network = address.rsplit(".", 1)[0]
        hosts.add(network + ".1")
        for last in (50, 87, 100, 107, 108, 109, 120, 168):
            hosts.add(network + "." + str(last))
    for name in ("printer", "receipt", "posprinter", "epson", "munbyn"):
        try:
            for info in socket.getaddrinfo(name, 9100):
                hosts.add(info[4][0])
        except socket.gaierror:
            pass
    for probe in hosts:
        printer = None
        net = None
        try:
            net = NetPrinter(probe, 9100, timeout)
            reply = net.probe()
            if reply and len(reply) >= 4:
                printer = net
        except PrinterError:
            continue
        finally:
            if printer is None and net is not None:
                pass
        if printer is not None:
            found.append({"transport": "net",
                          "target": "%s:9100" % probe,
                          "name": "ESC/POS on %s" % probe})
    return found


def save_config(cfg, path):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, indent=2)


def test_odoo(cfg):
    client = make_client(cfg)
    if cfg.get("api") == "jsonrpc":
        client._call_jsonrpc("common", "version", [])
    uid = client.authenticate()
    client.search_read("sale.order", [], ["name"], limit=1)
    return uid


def test_printer(cfg):
    printer = open_printer(cfg)
    try:
        query = getattr(printer, "query", None)
        if query:
            printer.query(b"\x10\x04\x01", 4)
        else:
            printer.send(b"\n\n\n")
    finally:
        close = getattr(printer, "close", None)
        if close:
            close()


def printer_from_settings(cfg, payload):
    transport = str(payload.get("transport") or "none").lower()
    target = str(payload.get("target") or "").strip()
    try:
        timeout = max(1, int(payload.get("timeout") or 5))
    except (TypeError, ValueError):
        timeout = 5
    if transport not in ("net", "usb", "dev", "cups", "win", "none"):
        raise OdooError("unknown transport: %r" % transport)
    if transport in ("net", "dev", "cups", "usb", "win") and not target:
        raise OdooError("the transport needs a target")
    cfg["printer"] = {"transport": transport, "target": target,
                      "timeout": timeout}
    return cfg["printer"]


def settings_view(cfg, users):
    receipt = cfg.get("receipt") or {}
    return {
        "printer_transport": (cfg.get("printer") or {}).get("transport")
        or "none",
        "printer_target": (cfg.get("printer") or {}).get("target") or "",
        "printer_timeout": (cfg.get("printer") or {}).get("timeout") or 5,
        "url": cfg.get("url") or "",
        "db": cfg.get("db") or "",
        "user": cfg.get("user") or "",
        "api": cfg.get("api") or "jsonrpc",
        "timeout": cfg.get("timeout") or 30,
        "shop_name": receipt.get("shop_name") or "",
        "shop_address_lines": receipt.get("shop_address_lines") or [],
        "shop_phone": receipt.get("shop_phone") or "",
        "footer_lines": receipt.get("footer_lines") or [],
        "width": receipt.get("width") or 48,
        "left_margin": receipt.get("left_margin") or 0,
        "timezone": receipt.get("timezone") or "",
        "show_unit_price": bool(receipt.get("show_unit_price")),
        "show_notes": bool(receipt.get("show_notes")),
        "show_barcode": bool(receipt.get("show_barcode")),
        "decimal_comma": bool(receipt.get("decimal_comma")),
        "price_mode": receipt.get("price_mode") or "total",
        "internal_receipts": receipt.get("internal_receipts") or "off",
        "states": cfg.get("states") or [],
        "only_website": bool(cfg.get("only_website")),
        "batch": cfg.get("batch") or 50,
    }, users


def apply_odoo_settings(cfg, payload):
    for key in ("url", "db", "user", "api_key"):
        if key in payload:
            cfg[key] = str(payload.get(key) or "").strip()
    cfg["url"] = (cfg.get("url") or "").rstrip("/")
    if payload.get("api") in ("jsonrpc", "json2"):
        cfg["api"] = payload["api"]
    try:
        timeout = int(payload.get("timeout") or 0)
    except (TypeError, ValueError):
        timeout = 0
    if timeout > 0:
        cfg["timeout"] = timeout


def apply_receipt_settings(cfg, payload):
    receipt = cfg.setdefault("receipt", {})
    for key in ("shop_name", "shop_phone", "timezone", "price_mode",
                "internal_receipts"):
        if key in payload:
            receipt[key] = str(payload.get(key) or "")
    for key in ("shop_address_lines", "footer_lines"):
        if key in payload:
            value = payload.get(key)
            if not isinstance(value, list):
                raise OdooError("%s must be a list of lines" % key)
            receipt[key] = [str(line) for line in value]
    for key in ("width", "left_margin"):
        if key in payload:
            try:
                receipt[key] = int(payload.get(key))
            except (TypeError, ValueError):
                raise OdooError("%s must be a number" % key)
    for key in ("show_unit_price", "show_notes", "show_barcode",
                "decimal_comma"):
        if key in payload:
            receipt[key] = bool(payload.get(key))


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for the bridge web interface and the print API.

    Sessions are cookie based. POS users can list orders and print.
    Admin users can also change the settings and manage the users. The
    old X-Print-Token header still works for the Odoo browser button.
    """

    server_version = "odoo_receipt/" + VERSION
    bridge_client = None
    bridge_cfg = None
    bridge_printer = None
    bridge_state = None
    bridge_token = ""
    bridge_users = None
    bridge_sessions = None
    bridge_config_path = ""

    def log_message(self, fmt, *log_args):
        print("[bridge] " + (fmt % log_args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-Print-Token")

    def _json(self, code, payload, set_cookie=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if set_cookie == "":
            self.send_header("Set-Cookie",
                             "bridge_session=; Max-Age=0; HttpOnly")
        elif set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, body, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        body = b""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _session(self):
        cookie = self.headers.get("Cookie") or ""
        token = ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "bridge_session":
                token = value
        return self.bridge_sessions.get(token), token

    def _authorized(self, admin=False):
        if self.headers.get("X-Print-Token") == self.bridge_token:
            return {"username": "button", "role": "admin"}
        session, _ = self._session()
        if session and (not admin or session["role"] == "admin"):
            return session
        return None

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(
                self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError:
            return None
        return payload

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self._json(200, {"ok": True, "version": VERSION})
            return
        if (self.bridge_users.is_empty()
                and not self.bridge_users.is_setup_complete()
                and path != "/quickstart"):
            self._redirect("/quickstart")
            return
        if path == "/login":
            session, _ = self._session()
            body = web_pages.LOGIN_PAGE.encode("utf-8")
            self._html(200, body)
            return
        if path == "/quickstart":
            if self.bridge_users.is_setup_complete():
                self._redirect("/login")
                return
            printer = self.bridge_cfg.get("printer") or {}
            current = json.dumps({
                "odoo": {"url": self.bridge_cfg.get("url") or "",
                         "db": self.bridge_cfg.get("db") or "",
                         "user": self.bridge_cfg.get("user") or ""},
                "printer": {"transport": printer.get("transport") or "none",
                            "target": printer.get("target") or ""},
            }).replace("</", "<\\/")
            body = web_pages.QUICKSTART_PAGE.replace("__CONFIG__", current)
            self._html(200, body.encode("utf-8"))
            return
        if path == "/logout":
            self._redirect("/login")
            return
        session, _ = self._session()
        if (not session and path in ("/order", "/orders")
                and self.headers.get("X-Print-Token")
                == self.bridge_token):
            session = {"username": "button", "role": "admin"}
        if not session:
            self._redirect("/login")
            return
        if path == "/":
            if not self.bridge_client:
                self._json(503, {"ok": False, "error": "no Odoo config"})
                return
            html = web_pages.render_page_html(
                web_pages.orders_page(session["role"]),
                self.bridge_token, session["username"], session["role"])
            self._html(200, html.encode("utf-8"))
            return
        if path == "/order":
            if not self._authorized():
                self._json(403, {"ok": False,
                                 "error": "log in first"})
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
                    variant = variants.get(product[0]) or {}
                    items.append({
                        "name": line.get("name") or product[1],
                        "product": (variant.get("template_name")
                                     or variant.get("name") or product[1]),
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
            if not self._authorized():
                self._json(403, {"ok": False,
                                 "error": "log in first"})
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
                printed_internal = (self.bridge_state.data.get(
                    "printed_internal") or {})
            else:
                printed = {}
                printed_internal = {}
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
                            "printed": o.get("name") in printed,
                            "printed_internal": (o.get("name")
                                                  in printed_internal)}
                           for o in orders]})
            return
        if path == "/settings":
            if session["role"] != "admin":
                self._json(403, {"ok": False,
                                 "error": "admins only"})
                return
            html = web_pages.render_page_html(
                web_pages.SETTINGS_PAGE, self.bridge_token,
                session["username"], session["role"])
            self._html(200, html.encode("utf-8"))
            return
        self._json(404, {"ok": False, "error": "unknown path"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        payload = self._body()
        if payload is None:
            self._json(400, {"ok": False, "error": "the body is not JSON"})
            return
        if path == "/login":
            self._do_login(payload)
            return
        if path == "/logout":
            session, token = self._session()
            if session:
                self.bridge_sessions.destroy(token)
            self._json(200, {"ok": True}, set_cookie="")
            return
        if path == "/setup/reset":
            if not self._authorized(admin=True):
                self._json(403, {"ok": False, "error": "admins only"})
                return
            self.bridge_users.clear_setup_complete()
            self._json(200, {"ok": True})
            return
        if path.startswith("/quickstart/"):
            self._do_quickstart(path, payload)
            return
        if path.startswith("/users/"):
            self._do_users(path, payload)
            return
        if path == "/settings" or path.startswith("/settings/"):
            self._do_settings(path, payload)
            return
        if path in ("/print", "/printed"):
            self._do_print(path, payload)
            return
        self._json(404, {"ok": False, "error": "unknown path"})

    def _do_login(self, payload):
        if self.bridge_users.is_empty():
            self._json(403, {"ok": False,
                             "error": "run the quickstart first"})
            return
        account = self.bridge_users.authenticate(payload.get("username"),
                                                  payload.get("password"))
        if not account:
            self._json(403, {"ok": False,
                             "error": "wrong username or password"})
            return
        token = self.bridge_sessions.create(account["username"],
                                             account["role"])
        self.bridge_users.mark_setup_complete()
        cookie = ("bridge_session=%s; Path=/; HttpOnly; SameSite=Lax"
                  % token)
        self._json(200, {"ok": True, "username": account["username"],
                         "role": account["role"]}, set_cookie=cookie)

    def _do_quickstart(self, path, payload):
        if self.bridge_users.is_setup_complete():
            self._json(403, {"ok": False,
                             "error": "the quickstart is finished"})
            return
        if path == "/quickstart/admin":
            try:
                self.bridge_users.create(payload.get("username"),
                                         payload.get("password"), "admin")
            except auth.AuthError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            self._json(200, {"ok": True,
                             "note": "Administrator created. Test Odoo "
                                     "and the printer below, or log in."})
            return
        if path == "/quickstart/odoo":
            apply_odoo_settings(self.bridge_cfg, payload)
            save_config(self.bridge_cfg, self.bridge_config_path)
            note = "Saved."
            try:
                uid = test_odoo(self.bridge_cfg)
                note = "Saved. Odoo login OK, user id %s." % uid
                BridgeHandler.bridge_client = make_client(self.bridge_cfg)
            except (OdooError, PrinterError) as exc:
                note = "Saved, but the connection failed: %s" % exc
            self._json(200, {"ok": True, "note": note})
            return
        if path == "/quickstart/printer":
            try:
                printer_from_settings(self.bridge_cfg, payload)
            except (OdooError, PrinterError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            save_config(self.bridge_cfg, self.bridge_config_path)
            note = "Saved."
            try:
                test_printer(self.bridge_cfg)
                note = "Saved. The printer answered."
                BridgeHandler.bridge_printer = open_printer(self.bridge_cfg)
            except (OdooError, PrinterError) as exc:
                note = "Saved, but the printer test failed: %s" % exc
            self._json(200, {"ok": True, "note": note})
            return
        if path == "/quickstart/detect":
            printers = detect_printers(self.bridge_cfg)
            self._json(200, {"ok": True, "printers": printers})
            return
        self._json(404, {"ok": False, "error": "unknown path"})

    def _do_users(self, path, payload):
        if not self._authorized(admin=True):
            self._json(403, {"ok": False, "error": "admins only"})
            return
        try:
            if path == "/users/add":
                self.bridge_users.create(payload.get("username"),
                                         payload.get("password"),
                                         payload.get("role") or "pos")
            elif path == "/users/password":
                self.bridge_users.set_password(payload.get("username"),
                                               payload.get("password"))
            elif path == "/users/role":
                self.bridge_users.set_role(payload.get("username"),
                                           payload.get("role"))
            elif path == "/users/delete":
                username = str(payload.get("username") or "").strip().lower()
                session, _ = self._session()
                if session and session["username"] == username:
                    raise auth.AuthError("you cannot delete your own "
                                         "account")
                self.bridge_users.delete(username)
            else:
                self._json(404, {"ok": False, "error": "unknown path"})
                return
        except auth.AuthError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        self._json(200, {"ok": True, "users": self.bridge_users.list()})

    def _do_settings(self, path, payload):
        if path == "/settings":
            if not self._authorized(admin=True):
                self._json(403, {"ok": False, "error": "admins only"})
                return
            cfg = load_config(self.bridge_config_path)
            settings, users = settings_view(cfg,
                                           self.bridge_users.list())
            self._json(200, {"ok": True, "settings": settings,
                             "users": users})
            return
        if not self._authorized(admin=True):
            self._json(403, {"ok": False, "error": "admins only"})
            return
        cfg = load_config(self.bridge_config_path)
        apply_overrides(cfg, argparse.Namespace(**{
            "url": None, "db": None, "user": None, "api_key": None,
            "api": None, "transport": None, "target": None,
            "width": None, "margin": None}))
        if path == "/settings/printer":
            try:
                printer_from_settings(cfg, payload)
            except (OdooError, PrinterError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
        elif path == "/settings/odoo":
            apply_odoo_settings(cfg, payload)
        elif path == "/settings/receipt":
            try:
                apply_receipt_settings(cfg, payload)
            except OdooError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
        elif path == "/settings/poll":
            states = str(payload.get("states") or "").strip()
            if states:
                cfg["states"] = [s for s in states.split(",") if s.strip()]
            cfg["only_website"] = bool(payload.get("only_website"))
            try:
                cfg["batch"] = max(1, min(int(payload.get("batch") or 50),
                                          500))
            except (TypeError, ValueError):
                cfg["batch"] = 50
        else:
            self._json(404, {"ok": False, "error": "unknown path"})
            return
        save_config(cfg, self.bridge_config_path)
        BridgeHandler.bridge_cfg = cfg
        if path == "/settings/printer":
            try:
                BridgeHandler.bridge_printer = open_printer(cfg)
            except (OdooError, PrinterError):
                BridgeHandler.bridge_printer = None
        elif path == "/settings/odoo":
            BridgeHandler.bridge_client = (make_client(cfg)
                                           if cfg.get("url") else None)
        note = "Saved."
        if path in ("/settings/printer", "/settings/odoo"):
            try:
                if path == "/settings/printer":
                    test_printer(cfg)
                else:
                    uid = test_odoo(cfg)
                    note = "Saved. Odoo login OK, user id %s." % uid
            except (OdooError, PrinterError) as exc:
                note = "Saved, but the test failed: %s" % exc
        self._json(200, {"ok": True, "note": note})

    def _do_print(self, path, payload):
        if not self._authorized():
            self._json(403, {"ok": False,
                             "error": "bad or missing token"})
            return
        if path == "/printed":
            ref = str(payload.get("ref") or payload.get("order") or "").strip()
            kind = "internal" if payload.get("kind") == "internal" \
                else "receipt"
            printed = bool(payload.get("printed"))
            if not ref:
                self._json(400, {"ok": False, "error": "no order given"})
                return
            if self.bridge_state is None:
                self._json(500, {"ok": False, "error": "no state store"})
                return
            self.bridge_state.load()
            self.bridge_state.set_printed(ref, kind, printed)
            self._json(200, {"ok": True, "order": ref, "kind": kind,
                             "printed": self.bridge_state.is_printed(
                                 ref, kind)})
            return
        ref = str(payload.get("ref") or payload.get("order") or "").strip()
        if not ref:
            self._json(400, {"ok": False, "error": "no order given"})
            return
        if not self.bridge_printer:
            self._json(500, {"ok": False, "error": "no printer configured"})
            return
        try:
            order = fetch_order(self.bridge_client, ref)
            mode = internal_mode(self.bridge_cfg)
            if "internal" in payload:
                mode = "only" if payload.get("internal") else "off"
            print_with_mode(self.bridge_client, self.bridge_cfg,
                            self.bridge_printer, order, mode)
            if self.bridge_state is not None:
                self.bridge_state.mark_printed(
                    order, advance=False, kinds=mode_kinds(mode))
        except (OdooError, PrinterError) as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return
        self._json(200, {"ok": True, "order": order["name"]})


def cmd_serve(cfg, args):
    """Run the HTTP bridge: a web interface with login, the orders page,
    the admin settings page, and the print API for the Odoo button."""
    bridge = cfg.get("bridge") or {}
    token = str(bridge.get("token") or "").strip()
    if not token:
        token = secrets.token_hex(16)
        cfg.setdefault("bridge", {})["token"] = token
        save_config(cfg, args.config)
        print("The bridge token was written to the config file.")
    folder = os.path.dirname(args.config)
    auth.set_pepper(auth.load_or_create_pepper(folder))
    users_path = os.environ.get("ODOO_USERS_PATH") or os.path.join(
        os.path.dirname(args.config) or ".",
        "users.json")
    users = auth.UserStore(users_path)
    sessions = auth.SessionStore()
    BridgeHandler.bridge_users = users
    BridgeHandler.bridge_sessions = sessions
    BridgeHandler.bridge_cfg = cfg
    BridgeHandler.bridge_config_path = args.config
    BridgeHandler.bridge_client = make_client(cfg) if cfg.get("url") else None
    BridgeHandler.bridge_printer = None
    try:
        BridgeHandler.bridge_printer = open_printer(cfg)
    except PrinterError:
        pass
    BridgeHandler.bridge_state = StateStore(cfg["state_path"])
    BridgeHandler.bridge_token = token
    port = args.port or bridge.get("port") or 8765
    bind = args.bind or bridge.get("bind") or "127.0.0.1"
    try:
        httpd = http.server.ThreadingHTTPServer((bind, port),
                                                BridgeHandler)
    except OSError as exc:
        raise PrinterError("cannot bind %s:%s: %s" % (bind, port, exc))
    print("The bridge listens on http://%s:%s" %
          (bind, httpd.server_address[1]))
    if users.is_empty():
        print("First run: open the quickstart page to create an admin "
              "account.")
    print("Bridge token: %s" % token)
    print("Open http://%s:%s/ in the browser." %
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
            state.mark_printed(order, advance=False, kinds=mode_kinds(mode))
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
            mode = internal_mode(cfg)
            print_with_mode(client, cfg, printer, order, mode,
                            dry_run=args.dry_run)
            state.mark_printed(order, advance=True, kinds=mode_kinds(mode))
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
