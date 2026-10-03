"""A tiny fake Odoo JSON-RPC server for the test suite."""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ORDER = {
    "id": 1,
    "name": "S00042",
    "state": "sale",
    "date_order": "2025-01-02 10:00:00",
    "amount_total": 20.5,
    "amount_untaxed": 17,
    "amount_tax": 3.5,
    "currency_id": [1, "EUR"],
    "partner_id": [5, "Alice"],
    "order_line": [10],
}

LINE = {
    "id": 10,
    "product_id": [3, "Shirt / Blue"],
    "name": "Shirt",
    "display_type": False,
    "product_uom_qty": 2,
    "price_unit": 10,
    "price_subtotal": 20,
    "price_total": 20.5,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = json.loads(self.rfile.read(length))
        params = raw.get("params") or {}
        method = params.get("method")
        args = params.get("args") or []
        if method == "version":
            self._reply({"result": {"server_version": "18.0"}})
        elif method == "authenticate":
            self._reply({"result": 7})
        elif method == "execute_kw":
            model = args[3]
            inner_method = args[4]
            inner_args = args[5] if len(args) > 5 else []
            if model == "sale.order" and inner_method == "search_read":
                domain = inner_args[0] if inner_args else []
                if domain and domain[0][0] == "name":
                    order = dict(ORDER, name=domain[0][2])
                    self._reply({"result": [order]})
                else:
                    self._reply({"result": [ORDER]})
            elif model == "sale.order.line":
                self._reply({"result": [LINE]})
            elif model == "product.product":
                self._reply({"result": [
                    {"id": 3, "name": "Shirt / Blue",
                     "product_template_attribute_value_ids": [],
                     "product_tmpl_id": [2, "Shirt"]}]})
            else:
                self._reply({"result": []})
        else:
            self._reply({"error": {"code": 404}})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18999
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
