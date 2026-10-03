# Odoo Receipt Plugin — Research Report

## Question

How can orders from an Odoo Online (odoo.com) eCommerce store be fetched automatically, and printed as receipts, including each product and its variant, on an ESC/POS receipt printer (MUNBYN, Epson mode, 80 mm, USB and LAN)?

## Executive Summary

1. Odoo exposes all data through its external API. For Odoo Online, the JSON-RPC endpoint `/jsonrpc` works today. An API key replaces the password, and this is the secure path for Online instances, where users normally have no local password.
2. Odoo marks website orders with the `website_id` field. Confirmed orders have the state `sale` or `done`. A poll on `website_id != False` plus `id > watermark` yields exactly the new eCommerce orders.
3. Each order line points to a `product.product` record. That record is the variant. Its variant attributes come from `product_template_attribute_value_ids`, which give pairs such as "Color: Blue".
4. The MUNBYN printer accepts a raw ESC/POS byte stream on TCP port 9100. The LAN transport is two-way, so the plugin can also read the printer status.
5. A standalone Python bridge is the correct architecture for Odoo Online: the server cannot reach a printer on the local network, but a local machine can reach both the Odoo API and the printer.
6. The old RPC endpoints (`/xmlrpc`, `/xmlrpc/2`, `/jsonrpc`) are scheduled for removal on Odoo Online in winter 2027 (version 21.1). The plugin isolates the API layer, so a move to the JSON-2 API (`/json/2`, bearer API key) stays contained.
7. The delivered plugin `odoo_receipt.py` implements both trigger modes: an automatic poll for new confirmed orders, and on-demand printing by order name or id.
8. A server-side module with a print button is not possible on Odoo Online: the platform does not accept custom Python code without Odoo.sh or self-hosting. The button therefore lives in the browser: a userscript inside the Odoo web client sends the order to a local bridge that the plugin runs.

## Methodology

The research covered three angles:

1. The Odoo external API: protocol choice, authentication, endpoints, deprecation timeline, and the exact call payload. Sources: official Odoo documentation for versions 18 and 19, Odoo forums, and practitioner guides.
2. The data model: how to identify eCommerce orders, and how product variants attach to order lines. Sources: official Odoo documentation, Odoo forums, and Stack Overflow.
3. Receipt printing: raw ESC/POS command usage for the MUNBYN printer, transports, and status queries. Sources: the Personal Knowledge topic on ESC/POS printers (which consolidates the MUNBYN help center and the Epson ESC/POS reference), plus the python-escpos documentation.

Limitations:

- The Odoo documentation pages could not be opened in full during the research session. The claims below rest on the official documentation snippets and cross-checked secondary sources.
- The exact request body of the JSON-2 API (Odoo 19) was not verified against a live server. The plugin ships a JSON-2 backend marked experimental.
- No live Odoo store or printer was available for an end-to-end test. The plugin was reviewed by hand, not executed against a real store.

## Findings

### 1. Talking to Odoo Online

Odoo exposes its models over RPC, and the data of all modules is available, including `sale.order` and `sale.order.line` ([Odoo 18 External API documentation](https://www.odoo.com/documentation/18.0/developer/reference/external_api.html)). For Odoo Online, users are created without a local password, so an API key (or a manually set password) is required for API access ([Odoo 18 External API documentation](https://www.odoo.com/documentation/18.0/developer/reference/external_api.html)).

An API key is generated in the user account settings and replaces the password in RPC calls; Odoo supports this since version 14 ([oec.sh API guide](https://oec.sh/blog/odoo-api-integration), [Knit API guide](https://getknit.dev/blog/odoo-api-integration-guide-in-depth/)).

The JSON-RPC endpoint `/jsonrpc` takes a payload with `service`, `method`, and `args` ([dev.to JSON-RPC walkthrough](https://dev.to/maurice_ombewa_21d073ef7a/using-odoo-as-a-backend-module-setup-api-access-and-curl-commands-1dia)):

- Login: `service: "common"`, `method: "authenticate"`, `args: [db, user, api_key, {}]`. The call returns the user id.
- Data: `service: "object"`, `method: "execute_kw"`, `args: [db, uid, api_key, model, method, positional_args, keyword_args]` ([Odoo 17 forum thread on JSON-RPC and API keys](https://www.odoo.com/forum/help-1/odoo-17-json-rpc-and-api-key-263158), [Odoo forum on endpoints](https://www.odoo.com/forum/help-1/difference-between-odoo-api-endpoints-rpc-vs-web-dataset-164571)).

The database name is required for the login. On Odoo Online, it appears in developer mode near the logged-in user name, or through the database selector page ([Odoo forum](https://www.odoo.com/forum/help-1/retrieve-database-name-in-odoo-sh-188704), [Odoo forum](https://www.odoo.com/forum/help-1/how-do-i-find-out-the-name-of-the-database-to-use-to-connect-over-xmlrpc-58297)).

Deprecation: the endpoints `/xmlrpc`, `/xmlrpc/2`, and `/jsonrpc` are scheduled for removal in Odoo 22 (fall 2028) and on Odoo Online 21.1 (winter 2027). The JSON-2 API at `/json/2` replaces them and authenticates with a bearer API key ([Odoo 19 External RPC API](https://www.odoo.com/documentation/19.0/developer/reference/external_rpc_api.html), [Odoo 19 JSON-2 API](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html)). The store runs Odoo 19 today and moves to Odoo 20 next, so the default `jsonrpc` backend keeps working, and the JSON-2 backend can be verified now, ahead of the deadline.

### 2. Identifying eCommerce orders and their variants

Orders created by the website carry `website_id`; backend orders do not. This field is the standard way to separate website orders from backend orders ([Stack Overflow](https://stackoverflow.com/questions/62610388/odoo-13-create-sale-order-linked-with-website-using-python), [Odoo forum](https://www.odoo.com/forum/help-1/differentiate-odoo-website-orders-backend-orders-152279)). Confirmed orders have the state `sale` (or `done` when locked). Draft orders from the website are typically abandoned carts.

Variants: the product template holds the attributes, and each combination of attribute values is a `product.product` record, the variant ([Odoo 18 product variants documentation](https://www.odoo.com/documentation/18.0/applications/sales/sales/products_prices/products/variants.html)). A `sale.order.line` points to that variant through its `product_id` field. The variant, in turn, carries `product_template_attribute_value_ids`; each of those records links an attribute (for example "Color") to a value (for example "Blue").

Performance note: `search_read` with an explicit field list is the recommended pattern; fetching all fields is slow and can fail on Odoo Online ([Odoo forum](https://www.odoo.com/forum/help-1/external-api-error-getting-saleorder-information-218782), [oec.sh API guide](https://oec.sh/blog/odoo-api-integration)).

Custom attribute values (customer-entered text, Odoo 17 and later) sit in `custom_product_template_attribute_value_ids` on the order line. The plugin reads this field opportunistically and ignores it on older versions.

### 3. Printing on the MUNBYN (ESC/POS)

The printer accepts a raw ESC/POS byte stream. The LAN transport (TCP 9100) needs no driver and is two-way, so status queries work. Direct USB, a raw device file, CUPS in raw mode, and the Windows spooler in RAW mode are alternatives (Personal Knowledge: ESC/POS printers, consolidating the MUNBYN help center and the Epson ESC/POS reference; see also [python-escpos](https://python-escpos.readthedocs.io/)).

Key commands used by the plugin:

| Function | Bytes |
| --- | --- |
| Initialize | `1B 40` |
| Code page WPC1252 (accents) | `1B 74 10` |
| Bold on / off | `1B 45 1` / `1B 45 0` |
| Double size | `1D 21 11` |
| Alignment left / center / right | `1B 61 0..2` |
| Feed n lines | `1B 64 n` |
| Partial cut | `1D 56 42 00` |
| Real-time status | `10 04 n` |
| CODE128 barcode | `1D 6B 49 n data` |

### 4. Why a standalone bridge

Odoo store modules that print receipts exist (for example `pos_tcp_esc_printer` and `pos_print_agent` on the Odoo Apps Store), but they target the Point of Sale app, not eCommerce orders, and several need a local agent anyway ([Odoo Apps Store](https://apps.odoo.com/apps/modules/18.0/pos_tcp_esc_printer), [Odoo Apps Store](https://apps.odoo.com/apps/modules/17.0/pos_print_agent)). The IoT Box can drive a receipt printer from Odoo, but it adds hardware and is not needed when a local machine already sits next to the printer ([Odoo 19 IoT documentation](https://www.odoo.com/documentation/19.0/applications/general/iot/devices/printer.html)).

With Odoo Online, the hosted server cannot open a TCP connection to a printer on the local network. A standalone tool on a local machine solves this in the simplest way: it polls Odoo over HTTPS and writes the receipt bytes to the printer.

### 5. Poll design

The plugin uses two safeguards:

1. A watermark (the highest printed order id). Each cycle reads only orders above the watermark, in ascending id order.
2. A set of printed order names on disk. A name is added only after the printer accepts the receipt. If the printer is offline, the order stays unprinted and the next cycle retries it.

The first run records the watermark and prints nothing, so the tool never dumps the full order history on first launch. `--backfill N` prints the last N confirmed orders on demand.

### 6. A button inside Odoo on Odoo Online

Odoo Online does not support custom modules with Python code. Only XML/data modules can be imported through `base_import_module`; anything beyond that needs Odoo.sh or a self-hosted server ([Odoo forum](https://www.odoo.com/forum/help-1/integrating-custom-modules-with-the-odoo-online-website-possibilities-and-limitations-294294), [Odoo forum](https://www.odoo.com/forum/help-1/add-custom-app-to-odoo-online-197931), [Odoo forum](https://www.odoo.com/forum/help-1/how-can-we-use-3rd-party-apps-custom-module-in-odoo-cloud-138892)). A server-side print button is therefore not feasible on the current contract, and the hosted server could not reach the printer anyway (finding 4).

The button works from the browser instead, with three parts:

1. The bridge: `python odoo_receipt.py serve` listens on 127.0.0.1:8765. On the first start it generates a token and writes it to the config file.
2. The button: a userscript (canvas "Odoo Print Receipt Button") adds a small "Receipt printer" panel inside the Odoo backend. It reads the order number of the open record from the breadcrumb. Press Print, and the panel sends the order to the bridge with the token.
3. The bridge fetches the order through the Odoo API and prints the receipt. The token stops other websites from triggering prints.

Browser security: Chrome 142 and later gate requests from public sites to 127.0.0.1 behind a Local Network Access permission prompt. Loopback requests stay exempt from mixed-content blocking once the user grants the permission ([Chrome for Developers](https://developer.chrome.com/blog/local-network-access), [Stack Overflow](https://stackoverflow.com/questions/79819723/how-to-adapt-to-local-network-access-introduced-in-chrome-142)). Chrome asks on the first click; allow it once for the Odoo domain. The bridge also serves a plain page at `http://127.0.0.1:8765/`, so printing works without any browser extension as well.

## Plugin Setup

The plugin is the `odoo_receipt.py` code canvas. It needs Python 3.9 or later and no third-party package (the direct USB transport optionally needs pyusb).

### Step 1: Create an API key in Odoo

1. Log in to Odoo with a user that has Sales access.
2. Open the user preferences, go to the API Keys section (Account Security), and generate a key.
3. Store the key in a safe place. The key replaces the password in all API calls.

The user needs read access to `sale.order`, `sale.order.line`, `product.product`, and `res.currency`. The `check` command verifies each of these rights.

### Step 2: Configure the tool

Run `python odoo_receipt.py init`. Then edit the config file and fill in:

- `url`: the store URL, for example `https://myshop.odoo.com`.
- `db`: the database name. Activate developer mode in Odoo to see it near your user name.
- `user`: the login email of the API user.
- `api_key`: the key from step 1.
- `printer.transport`: `net` (recommended), `usb`, `dev`, `cups`, or `win`.
- `printer.target`: for LAN, the printer IP with port, for example `192.168.1.50:9100`. Print a self-test page on the MUNBYN (hold FEED while you power on) to find the IP.
- `receipt.shop_name`, `receipt.shop_address_lines`, `receipt.footer_lines`: the receipt header and footer.
- `receipt.width`: 48 for 80 mm paper, 32 for 58 mm paper.
- `receipt.left_margin`: blank space on the left, in characters. Set it to 2 to 4 when the print is cropped on the left edge.
- `receipt.decimal_comma`: set it to `true` for a decimal comma on Belgian receipts.
- `receipt.timezone`: for example `Europe/Brussels`, so the receipt shows local time. Odoo stores dates in UTC.
- `receipt.show_barcode`: set it to `true` to print the order number as a CODE128 barcode.
- `receipt.internal_receipts`: `off` (default), `add`, or `only`. Controls the packing list during the poll. `add` prints the packing list after each customer receipt. `only` replaces the customer receipt with the packing list.

### Step 3: Verify, then print

Run the commands in this order:

1. `python odoo_receipt.py check` — tests the login, the user rights, and the printer connection.
2. `python odoo_receipt.py orders` — lists the most recent eCommerce orders.
3. `python odoo_receipt.py print S00042 --text` — shows the receipt on screen, with the products and their variants. Nothing prints.
4. `python odoo_receipt.py print S00042` — prints the receipt.
5. `python odoo_receipt.py print S00042 --internal --text` — shows the packing list on screen: the order number at the top, then each item with a check square and its variant labels underneath. Remove `--text` to print it. Add `--internal` to the `print` and `test` commands, or set `receipt.internal_receipts` in the config file for the poll.
6. `python odoo_receipt.py status` — reads the printer status (net and usb transports).
7. `python odoo_receipt.py poll --interval 30` — prints every new confirmed website order.

### Step 4: Run the poll as a service (optional)

On Linux, create a systemd unit. Adjust the paths:

```ini
[Unit]
Description=Odoo receipt poller
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/odoo_receipt/odoo_receipt.py poll --interval 30
Restart=always
User=printer

[Install]
WantedBy=default.target
```

Enable it with `systemctl enable --now <unit-file>`. On Windows, use Task Scheduler with the `poll` command.

### Step 5: The button inside Odoo (optional)

1. Start the bridge on the machine next to the printer: `python odoo_receipt.py serve`. On the first start it generates a token and writes it to the config file.
2. Install Tampermonkey in the browser. Create a new script. Copy the code from the "Odoo Print Receipt Button" canvas into it. Replace the placeholder in `BRIDGE_TOKEN` with the token from the bridge output. Add an extra `@match` line if the store does not run on odoo.com.
3. Open a sale order in Odoo. The "Receipt printer" panel shows at the bottom right. It fills in the order number of the open record. Press Print.
4. Chrome asks once for permission to reach the local network. Allow it for the Odoo domain.
5. Run the bridge and the poll as two separate services. The bridge serves the button; the poll covers orders that nobody prints by hand.
6. Without Tampermonkey, open `http://127.0.0.1:8765/` in a browser tab, type the order name, and press Print.

## Source Notes

| Source | Credibility | Last updated |
| --- | --- | --- |
| [Odoo 18 External API documentation](https://www.odoo.com/documentation/18.0/developer/reference/external_api.html) | 5/5 | - |
| [Odoo 19 External RPC API documentation](https://www.odoo.com/documentation/19.0/developer/reference/external_rpc_api.html) | 5/5 | - |
| [Odoo 19 JSON-2 API documentation](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html) | 5/5 | - |
| [Odoo 18 product variants documentation](https://www.odoo.com/documentation/18.0/applications/sales/sales/products_prices/products/variants.html) | 5/5 | - |
| [Odoo forum: database name discovery](https://www.odoo.com/forum/help-1/how-do-i-find-out-the-name-of-the-database-to-use-to-connect-over-xmlrpc-58297) | 4/5 | - |
| [Odoo forum: website orders vs backend orders](https://www.odoo.com/forum/help-1/differentiate-odoo-website-orders-backend-orders-152279) | 4/5 | - |
| [Odoo forum: JSON-RPC and API keys on Odoo 17](https://www.odoo.com/forum/help-1/odoo-17-json-rpc-and-api-key-263158) | 4/5 | - |
| [Odoo forum: explicit fields in search_read](https://www.odoo.com/forum/help-1/external-api-error-getting-saleorder-information-218782) | 4/5 | - |
| [Stack Overflow: website_id on sale orders](https://stackoverflow.com/questions/62610388/odoo-13-create-sale-order-linked-with-website-using-python) | 4/5 | - |
| [oec.sh Odoo API integration guide](https://oec.sh/blog/odoo-api-integration) | 3/5 | - |
| [Knit Odoo API guide](https://getknit.dev/blog/odoo-api-integration-guide-in-depth/) | 3/5 | - |
| [dev.to JSON-RPC walkthrough](https://dev.to/maurice_ombewa_21d073ef7a/using-odoo-as-a-backend-module-setup-api-access-and-curl-commands-1dia) | 3/5 | - |
| [Odoo Apps Store: pos_tcp_esc_printer](https://apps.odoo.com/apps/modules/18.0/pos_tcp_esc_printer) | 3/5 | - |
| [Odoo 19 IoT documentation: connect a printer](https://www.odoo.com/documentation/19.0/applications/general/iot/devices/printer.html) | 5/5 | - |
| [Epson ESC/POS reference](https://download4.epson.biz/sec_pubs/pos/reference_en/escpos/commands.html) | 5/5 | - |
| [MUNBYN help center: ITPP047 ESC/POS commands](https://support.munbyn.com/hc/en-us/articles/11503298932243-ITPP047-esc-pos-HEX-command) | 4/5 | - |
| [python-escpos documentation](https://python-escpos.readthedocs.io/) | 4/5 | - |
| [Odoo forum: custom modules on Odoo Online](https://www.odoo.com/forum/help-1/integrating-custom-modules-with-the-odoo-online-website-possibilities-and-limitations-294294) | 4/5 | - |
| [Chrome for Developers: Local Network Access](https://developer.chrome.com/blog/local-network-access) | 5/5 | - |
| Personal Knowledge: ESC/POS printers (internal topic) | 4/5 | 2026-10-02 |

Conflicts and caveats:

- Forum sources disagree on how reliably `website_id` separates website orders from backend orders, because a customized module can alter the field. On an unmodified Odoo Online store, the field is set by the website flow and is the accepted filter.
- The JSON-2 request body was inferred from the Odoo 19 documentation snippets, not from a live call. Treat the `--api json2` option as experimental until it is verified against a real Odoo 19+ server.
- Status byte decoding is conservative. The plugin prints the raw hex and only interprets the paper sensors. The Epson manual is authoritative for the bit meanings.
- The cutter must be enabled with DIP switch SW-1 on the MUNBYN. If the cutter is disabled in hardware, no command makes the printer cut.

## Open Questions

1. The store runs Odoo 19 today and moves to Odoo 20. The `jsonrpc` backend serves both versions. Verify the JSON-2 backend against the live store during v19 or v20, so the switch is ready before Odoo Online reaches version 21.1 (winter 2027).
2. Which order states count as "paid" for the store? The default is `sale` and `done`. With payment providers in manual capture, a paid order may stay in `sent`. If so, add `sent` to `states` in the config.
3. Do the products use custom text attributes (engraving, personalization)? The plugin supports them on Odoo 17 and later. A test with a real order confirms the output.
4. USB or LAN? LAN (port 9100) is the recommended transport because it also supports the `status` command. If only USB is available, decide between direct USB (pyusb), `/dev/usb/lp0`, CUPS raw, or the Windows spooler.

## Recommendations / Next Steps

1. Run `init`, fill the config, and run `check` first. It pinpoints missing rights and connection problems before any printing.
2. Test one real order with `print --text`, then with `--dry-run`, then print it for real.
3. Start the poll with a short interval on a busy day and confirm that each new paid order prints exactly once.
4. Keep the API key in the config file only, restrict the file permissions, and use a dedicated Odoo user with read-only Sales rights.
5. Plan the API migration before winter 2027: when Odoo Online moves to version 21.1, switch the config to the JSON-2 backend after verification.
6. To print from inside Odoo, run the `serve` bridge and install the userscript. Keep the bridge bound to 127.0.0.1, and keep the bridge token secret.
