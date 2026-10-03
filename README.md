# Odoo Receipt Bridge

Fetch confirmed orders from an Odoo eCommerce store and print receipts on an
ESC/POS receipt printer (Epson type, for example MUNBYN). The tool is one
standalone Python script. It needs only the Python standard library.

It works with Odoo Online (odoo.com) and Odoo 17 to 20, and it prints over
LAN (TCP 9100), raw USB, a device file, CUPS, or a Windows RAW port.

## What it does

- Polls the store for new confirmed website orders and prints a receipt for
  each one, automatically.
- Prints a receipt on demand, by order name or number, from the command line
  or from a button inside the Odoo backend.
- Prints a customer receipt with the products, the variants, the custom
  attribute values, the totals, and an optional CODE128 barcode of the order
  number.
- Prints a packing list for internal use: the order number at the top, then
  each item with a check square that the packer can tick, and the variant
  labels underneath the item.
- Gives the receipt a left margin in characters, for printers that crop the
  first columns.

## Requirements

- Python 3.9 or later.
- An Odoo user with the API key permission and read access to Sales. Use an
  API key, not a password. Odoo Online requires an API key.
- A receipt printer that speaks ESC/POS. The `net` transport (TCP 9100) is
  recommended. It is two-way, so the `status` command works.
- Optional: `pyusb`, for the direct USB transport.

## Quick start

```bash
python odoo_receipt.py init            # write a config file with defaults
# Edit ~/.config/odoo-receipt/config.json: url, db, user, api_key, printer
python odoo_receipt.py check          # test the Odoo login and the printer
python odoo_receipt.py orders         # list the most recent eCommerce orders
python odoo_receipt.py print S00042 --text   # preview the receipt on screen
python odoo_receipt.py print S00042         # print the receipt
python odoo_receipt.py print S00042 --internal   # print the packing list
python odoo_receipt.py poll --interval 30   # print every new order
```

## Configuration

The config file is at `~/.config/odoo-receipt/config.json`. Environment
variables and command line options override it. Put the command line options
before the subcommand: `python odoo_receipt.py --dry-run poll --once`.

Environment variables: `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_API_KEY`
(or `ODOO_PASSWORD`), `PRINTER_TRANSPORT`, `PRINTER_TARGET`, and
`ODOO_STATE_PATH` (the poll state file).

Odoo connection:

- `url`: the base URL, for example `https://myshop.odoo.com`.
- `db`: the database name. On Odoo Online it is usually the shop subdomain.
- `user`: the login email of the API user.
- `api_key`: the API key of that user.
- `api`: `jsonrpc` (default) or `json2` (experimental, Odoo 19 and later).

Order selection:

- `only_website`: `true` prints only orders that come from the website.
- `states`: the order states that print. Default: `["sale", "done"]`.
- `batch`: how many orders one poll cycle reads at most.

Printer:

- `printer.transport`: `net` (recommended), `usb`, `dev`, `cups`, `win`, or
  `none`.
- `printer.target`: for LAN, the printer IP with port, for example
  `192.168.1.50:9100`. Print a self-test page on the MUNBYN (hold FEED while
  you power on) to find the IP.

Receipt:

- `receipt.width`: 48 for 80 mm paper, 32 for 58 mm paper.
- `receipt.left_margin`: blank space on the left, in characters. Set it to
  2 to 4 when the print is cropped on the left edge.
- `receipt.shop_name`, `receipt.shop_address_lines`, `receipt.shop_phone`:
  the receipt header.
- `receipt.footer_lines`: the receipt footer.
- `receipt.price_mode`: `total` (with tax) or `subtotal` (without tax).
- `receipt.show_unit_price`: `true` prints the unit price under each item.
- `receipt.show_notes`: `true` prints the order note lines.
- `receipt.show_barcode`: `true` prints the order number as a CODE128 barcode.
- `receipt.decimal_comma`: `true` for a decimal comma, for example on
  Belgian receipts.
- `receipt.timezone`: for example `Europe/Brussels`. Odoo stores dates in
  UTC; the receipt shows local time.
- `receipt.internal_receipts`: `off` (default), `add`, or `only`. Controls the
  packing list during the poll. `add` prints the packing list after each
  customer receipt. `only` replaces the customer receipt with the packing
  list.

## Commands

| Command | What it does |
| --- | --- |
| `init` | Write a config file with default values. |
| `check` | Test the Odoo login, the user rights, and the printer. |
| `orders` | List the most recent eCommerce orders. |
| `print ORDER [ORDER ...]` | Print the receipt for one or more orders. |
| `print --internal` | Print the packing list instead of the customer receipt. |
| `print --text` | Show the receipt on screen. Print nothing. |
| `poll` | Watch for new orders and print them. |
| `poll --once` | Run one poll cycle, then exit. |
| `poll --backfill N` | Print the last N confirmed orders now. |
| `test` | Print a test receipt with two sample variant products. |
| `test --internal` | Print a test packing list. |
| `status` | Read the printer status (net and usb transports). |
| `serve` | Run the local bridge for the button in the Odoo backend. |

Common options before the subcommand: `--config`, `--url`, `--db`, `--user`,
`--api-key`, `--transport`, `--target`, `--width`, `--margin`, `--dry-run`.

## How the poll works

The poll uses an id watermark and a list of printed order names in the state
file (`~/.local/state/odoo-receipt/state.json`). The first run sets the
watermark and prints nothing. New confirmed website orders print from then
on. A manual `print` records the order as printed, so the poll does not
print it again.

## The button inside Odoo

Odoo Online does not accept custom Python modules, so the button is a
Tampermonkey userscript that talks to a local bridge:

1. Start the bridge on the machine next to the printer:
   `python odoo_receipt.py serve`. On the first start it generates a token
   and writes it to the config file.
2. Install Tampermonkey in the browser. Create a new script. Copy the code
   from `tampermonkey/odoo-receipt-button.user.js` into it. Replace the
   placeholder in `BRIDGE_TOKEN` with the token from the bridge output. Add
   an extra `@match` line if the store does not run on odoo.com.
3. Open a sale order in Odoo. The panel shows at the bottom right. It fills
   in the order number of the open record. Press Print, or tick
   "Packing list" first to print the packing list.
4. Chrome asks once for permission to reach the local network. Allow it for
   the Odoo domain.

The bridge also serves a small web page at `http://127.0.0.1:8765/` for
printing without the userscript.

## Run the poll as a service (Linux)

```ini
[Unit]
Description=Odoo receipt poller
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/odoo-receipt-bridge/odoo_receipt.py poll --interval 30
Restart=always
User=printer

[Install]
WantedBy=default.target
```

On Windows, use Task Scheduler with the `poll` command.

## Docker

The repo has a `Dockerfile` and a `docker-compose.yml`, so you can self-host
the poller as a container. The image is small: the script needs only the
Python standard library.

### Run with Docker

```bash
docker build -t odoo-receipt-bridge .

docker run -d --name odoo-receipt-bridge \
  -e ODOO_URL=https://myshop.odoo.com \
  -e ODOO_DB=myshop \
  -e ODOO_USER=api-user@example.com \
  -e ODOO_API_KEY=... \
  -e PRINTER_TRANSPORT=net \
  -e PRINTER_TARGET=192.168.1.50:9100 \
  -e ODOO_STATE_PATH=/data/state.json \
  -v receipt-config:/config \
  -v receipt-state:/data \
  odoo-receipt-bridge poll --interval 30
```

The entrypoint always passes `--config /config/config.json`, so mount a
volume (or a directory) on `/config` to keep the config. The default command
is `poll --interval 30`. Any arguments after the image name replace the
command, for example:

```bash
docker run --rm -it ...same env vars... odoo-receipt-bridge check
docker run --rm -it ...same env vars... odoo-receipt-bridge orders
docker run --rm -it ...same env vars... odoo-receipt-bridge print S00042 --text
```

### Run with Docker Compose

Edit the environment values in `docker-compose.yml`, then:

```bash
mkdir -p config state
docker compose up -d --build
docker compose logs -f          # watch the poller
docker compose run --rm odoo-receipt-bridge check
```

`config/` and `state/` are bind-mounted, so the config file and the poll
watermark survive a rebuild. To write the initial config file with
defaults:

```bash
docker compose run --rm odoo-receipt-bridge init
# then edit config/config.json
docker compose restart
```

### The bridge button in Docker

To run the `serve` command for the Odoo backend button instead of the poll,
override the command and the port mapping. Keep the bind on `0.0.0.0`
inside the container and map the port; the token still protects the
endpoint:

```yaml
    command: serve --bind 0.0.0.0 --port 8765
    ports:
      - "127.0.0.1:8765:8765"
```

Mapping `127.0.0.1:8765:8765` keeps the bridge reachable only from the host
that runs the container, which is what the Tampermonkey button expects.

### Printer notes

- `net` (TCP 9100) is the natural transport in a container. The printer
  must be reachable from the container network, which is the case for a
  LAN printer on the default bridge network.
- `usb` and `dev` need access to the USB or printer device; pass it with
  `--device /dev/usb/lp0` (and `privileged: true` for raw USB), but prefer
  `net` in Docker.
- `cups` and `win` do not apply inside the container. Print through the
  CUPS or Windows host with the `net` transport instead.
- Set `receipt.timezone`, because the container runs in UTC by default.

## Notes on the Odoo API

- The default `jsonrpc` backend works through Odoo 20 and on Odoo Online
  today. Odoo plans to remove `/jsonrpc` for Online databases in v21.1
  (winter 2027).
- The script includes an experimental `json2` backend for Odoo 19 and
  later. It uses the API key as a bearer token. Verify the request format
  against the Odoo documentation before you rely on it.
- Product variants come from the order line product (`product.product`) and
  its `product_template_attribute_value_ids` records. Custom attribute
  values are read from the order line, when the Odoo version has the field.

## Project layout

- `odoo_receipt.py` — the complete tool: Odoo client, receipt rendering,
  ESC/POS output, printers, poll, bridge, and commands.
- `tampermonkey/odoo-receipt-button.user.js` — the userscript for the
  button in the Odoo backend.
- `docs/research-notes.md` — the research report behind the design, with
  sources.

## License

See the LICENSE file.
