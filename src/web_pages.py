"""HTML pages for the bridge web interface.

The pages are plain HTML with a shared style and a small amount of
JavaScript. They call the JSON API of the bridge. The server renders
__TOKEN__, __USER__ and __ROLE__ into the page before serving it.
"""

STYLE = """
 :root {
   --bg: #fff; --fg: #222; --muted: #777; --line: #ddd; --line2: #999;
   --soft: #f6f6f6; --detail: #555; --ok: #2a7; --err: #c22;
   --btn: #eee; --btn-fg: #222; --btn-border: #bbb; --danger: #b22;
 }
 @media (prefers-color-scheme: dark) {
   :root:not([data-theme=light]) {
     --bg: #14161a; --fg: #e5e7ea; --muted: #9aa0a6; --line: #33373d;
     --line2: #555a61; --soft: #1d2025; --detail: #b0b6bc; --ok: #3c9;
     --err: #f77; --btn: #2a2e34; --btn-fg: #e5e7ea; --btn-border: #4a4f56;
     --danger: #e55;
   }
 }
 :root[data-theme=dark] {
   --bg: #14161a; --fg: #e5e7ea; --muted: #9aa0a6; --line: #33373d;
   --line2: #555a61; --soft: #1d2025; --detail: #b0b6bc; --ok: #3c9;
   --err: #f77; --btn: #2a2e34; --btn-fg: #e5e7ea; --btn-border: #4a4f56;
   --danger: #e55;
 }
 html { color-scheme: light dark; }
 body { font-family: sans-serif; max-width: 46rem; margin: 2rem auto;
        padding: 0 1rem; color: var(--fg); background: var(--bg); }
 h1 { font-size: 1.4rem; }
 h2 { font-size: 1.1rem; margin-top: 2rem; }
 input[type=text], input[type=password], input[type=number] {
   font-size: 1rem; padding: .35rem; color: var(--fg);
   background: var(--bg); border: 1px solid var(--btn-border); }
 textarea { font-size: 1rem; padding: .35rem; color: var(--fg);
            background: var(--bg); border: 1px solid var(--btn-border); }
 input.wide { width: 100%; box-sizing: border-box; }
 button { font-size: .95rem; padding: .4rem .9rem; cursor: pointer;
          color: var(--btn-fg); background: var(--btn);
          border: 1px solid var(--btn-border); border-radius: 4px; }
 button.danger { background: var(--danger); border-color: var(--danger);
                 color: #fff; }
 table { border-collapse: collapse; width: 100%; }
 th, td { text-align: left; padding: .45rem .6rem; border-bottom:
          1px solid var(--line); }
 th { border-bottom: 2px solid var(--line2); }
 td.num { text-align: right; }
 td.actions { white-space: nowrap; text-align: right; }
 td.actions button { padding: .3rem .6rem; }
 tr.printed .name { color: var(--muted); }
 td.mark { cursor: pointer; user-select: none; }
 td.mark:hover { outline: 1px dotted var(--line2); }
 tr.detail > td { background: var(--soft); }
 tr.detail div.item { padding: .1rem 0; }
 tr.detail div.vlabel { color: var(--detail); font-size: .9rem; }
 tr.detail button { padding: .2rem .5rem; font-size: .85rem; }
 .done { color: var(--ok); }
 select { font-size: .95rem; padding: .3rem; color: var(--fg);
          background: var(--bg); border: 1px solid var(--btn-border); }
 #status { min-height: 1.2rem; }
 label { display: block; margin: .6rem 0 .1rem; }
 label.inline { display: inline; }
 label.toggle { display: inline; font-size: .95rem; margin-left: .8rem; }
 label.toggle.nowrap { display: inline-block; white-space: nowrap; }
 .card { border: 1px solid var(--line); border-radius: 6px;
         padding: 1rem 1.2rem; margin: 1.5rem 0; }
 .card h2 { margin-top: 0; }
 .err { color: var(--err); }
 .toast { position: fixed; bottom: 1.2rem; right: 1.2rem; z-index: 50;
          margin: 0; padding: .6rem 1rem; border-radius: 6px;
          border: 1px solid var(--line); background: var(--bg);
          box-shadow: 0 2px 10px rgba(0,0,0,.25); display: none; }
 .toast.show { display: block; }
 .toast.done { color: var(--ok); }
 .toast.err { color: var(--err); }
 .muted { color: var(--muted); }
 .nav { margin-bottom: 1.5rem; }
 .nav a { margin-right: 1.2rem; }
 .menubar { position: relative; margin-bottom: 1rem; }
 #menu-btn { font-size: 1.1rem; padding: .25rem .6rem; }
 .menu { display: none; }
 .menu.open { display: block; position: absolute; top: 2.4rem; left: 0;
              background: var(--bg); border: 1px solid var(--line);
              border-radius: 6px; padding: .8rem 1.2rem; z-index: 20;
              min-width: 12rem; box-shadow: 0 2px 10px rgba(0,0,0,.25); }
 .menu a, .menu button { display: block; margin: .5rem 0; text-align: left; }
 .menu #who { display: block; margin-top: .8rem; font-size: .9rem; }
 .row { display: flex; gap: .6rem; align-items: center; flex-wrap: wrap; }
 #ref { max-width: 100%; box-sizing: border-box; }
 @media (max-width: 40rem) {
   body { margin: 1rem auto; padding: 0 .7rem; }
   h1 { font-size: 1.25rem; }
   h2 { margin-top: 1.5rem; }
   button { padding: .55rem .8rem; min-height: 2.6rem; }
   td.actions button, tr.detail button { padding: .45rem .7rem; }
   input[type=text], input[type=password], input[type=number],
   textarea, select { font-size: 1rem; }
   #ref { width: 100%; }
   td.mark { padding: .45rem .6rem; }
   .toast { left: .7rem; right: .7rem; bottom: .7rem; }
   table.responsive { border: 0; }
   table.responsive thead { display: none; }
   table.responsive tbody tr { display: block; border: 1px solid var(--line);
     border-radius: 6px; margin: .6rem 0; padding: .3rem .8rem .5rem; }
   table.responsive tbody td { display: flex; justify-content: space-between;
     align-items: baseline; gap: .8rem; border-bottom: 0;
     padding: .3rem 0; }
   table.responsive tbody td::before { content: attr(data-label);
     color: var(--muted); font-size: .85rem; flex: 0 0 auto; }
   table.responsive tbody td.actions { flex-wrap: wrap; gap: .4rem;
     padding-top: .5rem; border-top: 1px solid var(--line); }
   table.responsive tbody td.actions::before { content: none; }
   table.responsive tbody tr.detail { border: 0; padding: 0; }
   table.responsive tbody tr.detail td { display: block; padding: .4rem .2rem; }
   table.responsive tbody tr.detail td::before { content: none; }
 }
"""

THEME_JS = """
(function () {
  "use strict";
  var root = document.documentElement;
  var stored = null;
  try { stored = localStorage.getItem("theme"); } catch (e) {}
  if (stored === "light" || stored === "dark") {
    root.setAttribute("data-theme", stored);
  }
  document.addEventListener("click", function (event) {
    var btn = event.target.closest ? event.target.closest("#theme") : null;
    if (!btn) { return; }
    var current = root.getAttribute("data-theme");
    var dark = current ? current === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("theme", next); } catch (e) {}
  });
})();
"""

THEME_BTN = ("""<button id="theme" type="button" """
             """title="Switch light/dark theme">&#9681;</button>""")

NAV_JS = """
(function () {
  "use strict";
  document.addEventListener("click", function (event) {
    var btn = event.target.closest ? event.target.closest("#menu-btn") : null;
    var menu = document.getElementById("menu");
    if (btn) {
      menu.classList.toggle("open");
      return;
    }
    if (menu && menu.classList.contains("open")
        && !menu.contains(event.target)) {
      menu.classList.remove("open");
    }
  });
})();
"""


def nav_menu(items):
    links = "".join(
        '<a href="%s"%s>%s</a>' % (href, ' id="%s"' % id_ if id_ else "", label)
        for href, id_, label in items)
    return ("""<nav class="menubar">"""
            """<button id="menu-btn" type="button" """
            """title="Menu">"""
            """&#9776;</button>"""
            """<div id="menu" class="menu">"""
            + links +
            """<span id="who" class="muted"></span>"""
            + THEME_BTN +
            """</div></nav><script>""" + NAV_JS + """</script>""")


NAV_ADMIN = nav_menu([("/", "", "Orders"),
                     ("/settings", "", "System settings"),
                     ("#", "logout", "Log out")])
NAV_POS = nav_menu([("/", "", "Orders"),
                   ("#", "logout", "Log out")])


def page(title, body, extra_style="", nav=""):
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>%(title)s</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
%(style)s
%(extra)s
</style>
<script>
%(themejs)s
</script>
</head>
<body>
%(nav)s
%(body)s
</body>
</html>
""" % {"title": title, "style": STYLE, "extra": extra_style, "nav": nav,
        "body": body, "themejs": THEME_JS}


LOGIN_PAGE = page(
    "Log in - Odoo Receipt Bridge",
    """
<h1>Odoo Receipt Bridge</h1>
<h2>Log in</h2>
<form id="form" autocomplete="off">
<label>Username
<input type="text" id="username" class="wide" autocomplete="username">
</label>
<label>Password
<input type="password" id="password" class="wide"
       autocomplete="current-password">
</label>
<p><button type="submit">Log in</button>
<span id="status" class="err"></span></p>
</form>
<script>
"use strict";
document.getElementById("form")
  .addEventListener("submit", function (event) {
  event.preventDefault();
  var status = document.getElementById("status");
  status.textContent = "Logging in ...";
  fetch("/login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      username: document.getElementById("username").value,
      password: document.getElementById("password").value
    })
  }).then(function (r) { return r.json(); }).then(function (data) {
    if (data.ok) { window.location.href = "/"; return; }
    status.textContent = data.error || "Login failed.";
  }).catch(function (err) { status.textContent = "Failed: " + err; });
});
</script>
""", extra_style="#status { display: inline-block; }", nav=THEME_BTN)


QUICKSTART_PAGE = page(
    "Quickstart - Odoo Receipt Bridge",
    """
<h1>Odoo Receipt Bridge</h1>
<h2>Welcome</h2>
<p>Set up an administrator account, connect to Odoo, and connect a
printer. Each step can be skipped and changed later in the System
settings page.</p>

<div class="card">
<h2>1. Create the administrator account</h2>
<label>Username
<input type="text" id="admin-user" class="wide" value="admin">
</label>
<label>Password (at least 8 characters)
<input type="password" id="admin-pass" class="wide">
</label>
<p><button id="create-admin" type="button">Create account</button>
<span id="admin-status" class="err"></span></p>
</div>

<div class="card">
<h2>2. Connect to Odoo</h2>
<label>URL, for example https://myshop.odoo.com
<input type="text" id="odoo-url" class="wide" placeholder="https://myshop.odoo.com">
</label>
<label>Database name (empty for Odoo Online)
<input type="text" id="odoo-db" class="wide">
</label>
<label>API user login
<input type="text" id="odoo-user" class="wide" placeholder="you@example.com">
</label>
<label>API key
<input type="password" id="odoo-key" class="wide">
</label>
<p><button id="save-odoo" type="button">Save and test</button>
<span id="odoo-status" class="err"></span></p>
</div>

<div class="card">
<h2>3. Connect a printer</h2>
<p><button id="detect" type="button">Autodetect</button>
<span id="detect-status"></span></p>
<div id="detect-list"></div>
<div class="row" style="margin-top:.6rem;">
<select id="printer-transport">
<option value="net">net (TCP 9100)</option>
<option value="dev">dev (device file)</option>
<option value="cups">cups</option>
<option value="usb">usb (pyusb)</option>
<option value="win">win (Windows RAW)</option>
<option value="none">none</option>
</select>
<input type="text" id="printer-target" placeholder="192.168.1.50:9100">
<button id="save-printer" type="button">Save and test</button>
<span id="printer-status" class="err"></span>
</div>
</div>

<div class="card">
<h2>Finish</h2>
<p>Done, or not interested right now? Log in with the account created
above. Everything can be changed later in the System settings.</p>
<p><button id="skip" type="button">Go to the login page</button></p>
</div>

<script id="current-config" type="application/json">__CONFIG__</script>
<script>
"use strict";
var currentConfig = {};
try {
  currentConfig = JSON.parse(
    document.getElementById("current-config").textContent || "{}");
} catch (e) { currentConfig = {}; }
if (currentConfig.odoo) {
  if (currentConfig.odoo.url) {
    document.getElementById("odoo-url").value = currentConfig.odoo.url;
  }
  if (currentConfig.odoo.db) {
    document.getElementById("odoo-db").value = currentConfig.odoo.db;
  }
  if (currentConfig.odoo.user) {
    document.getElementById("odoo-user").value = currentConfig.odoo.user;
  }
}
if (currentConfig.printer) {
  var select = document.getElementById("printer-transport");
  for (var i = 0; i < select.options.length; i++) {
    if (select.options[i].value === currentConfig.printer.transport) {
      select.selectedIndex = i;
      break;
    }
  }
  if (currentConfig.printer.target) {
    document.getElementById("printer-target").value =
      currentConfig.printer.target;
  }
}
function post(path, payload, status) {
  status.className = "";
  status.textContent = "Working ...";
  return fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  }).then(function (r) { return r.json(); }).then(function (data) {
    if (data.ok) {
      status.className = data.warning ? "err" : "done";
      status.textContent = data.note || "";
    } else {
      status.className = "err";
      status.textContent = data.error || "Failed.";
    }
    return data;
  }).catch(function (err) {
    status.className = "err";
    status.textContent = "Failed: " + err;
  });
}
document.getElementById("create-admin").addEventListener("click", function () {
  post("/quickstart/admin", {
    username: document.getElementById("admin-user").value,
    password: document.getElementById("admin-pass").value
  }, document.getElementById("admin-status"));
});
document.getElementById("save-odoo").addEventListener("click", function () {
  post("/quickstart/odoo", {
    url: document.getElementById("odoo-url").value,
    db: document.getElementById("odoo-db").value,
    user: document.getElementById("odoo-user").value,
    api_key: document.getElementById("odoo-key").value
  }, document.getElementById("odoo-status"));
});
document.getElementById("detect").addEventListener("click", function () {
  var status = document.getElementById("detect-status");
  var list = document.getElementById("detect-list");
  status.textContent = "Scanning ...";
  list.replaceChildren();
  fetch("/quickstart/detect", {method: "POST", body: "{}"})
    .then(function (r) { return r.json(); })
    .then(function (data) {
    status.textContent = "";
    if (!data.ok) {
      status.textContent = data.error || "Detection failed.";
      return;
    }
    if (!data.printers || !data.printers.length) {
      status.textContent = "No printer found on port 9100.";
      return;
    }
    data.printers.forEach(function (p) {
      var label = document.createElement("label");
      label.className = "inline";
      var radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "detected";
      radio.value = p.target;
      label.appendChild(radio);
      label.appendChild(document.createTextNode(" " + p.target + " (" +
        p.name + ")"));
      list.appendChild(label);
      list.appendChild(document.createElement("br"));
    });
  }).catch(function (err) { status.textContent = "Failed: " + err; });
});
document.getElementById("save-printer").addEventListener("click", function () {
  var chosen = document.querySelector("input[name=detected]:checked");
  var transport = document.getElementById("printer-transport").value;
  var target = document.getElementById("printer-target").value;
  if (chosen && !target) { target = chosen.value; }
  post("/quickstart/printer", {transport: transport, target: target},
       document.getElementById("printer-status"));
});
document.getElementById("skip").addEventListener("click", function () {
  window.location.href = "/login";
});
</script>
""", nav=THEME_BTN)


def render_page_html(html, token, user, role):
    return (html.replace("__TOKEN__", token)
                .replace("__USER__", user or "")
                .replace("__ROLE__", role or ""))


def orders_page(role):
    nav = NAV_ADMIN if role == "admin" else NAV_POS
    return page("Odoo Receipt Bridge", """
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
<label class="toggle nowrap">Hide
<select id="hide">
<option value="off">nothing</option>
<option value="either">receipt or packing printed</option>
<option value="both">receipt and packing printed</option>
</select></label>
<span id="hint">Loading orders ...</span>
</p>
<table id="orders" class="responsive">
<thead><tr><th>Order</th><th>Date</th><th>State</th><th>Total</th>
<th>Customer</th><th>Receipt</th><th>Packing</th><th></th></tr></thead>
<tbody></tbody>
</table>
<h2>Print by reference</h2>
<p>Type an order name or id. Then press Print.</p>
<input type="text" id="ref" placeholder="S00042">
<label class="toggle"><input type="checkbox" id="internal">
Packing list</label>
<button id="print" type="button">Print</button>
<script>
"use strict";
var token = "__TOKEN__";
var user = "__USER__";
var role = "__ROLE__";
document.getElementById("who").textContent = user + (role === "admin" ?
  ", admin" : ", POS");
document.getElementById("logout").addEventListener("click", function () {
  fetch("/logout", {method: "POST"}).then(function () {
    window.location.href = "/login";
  });
});
var statusEl = document.getElementById("status");
var tbody = document.querySelector("#orders tbody");
var hint = document.getElementById("hint");
function setStatus(text) { statusEl.textContent = text; }
function api(path, options) {
  options = options || {};
  options.headers = Object.assign({"X-Print-Token": token},
                                  options.headers || {});
  return fetch(path, options);
}
function printRef(ref, internal, rowStatus) {
  var where = rowStatus || statusEl;
  where.textContent = "Printing " + ref + " ...";
  return api("/print", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
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
  td.colSpan = 8;
  td.textContent = "Loading " + ref + " ...";
  row.appendChild(td);
  tr.after(row);
  openDetails[ref] = {row: row};
  btn.textContent = "Hide";
  api("/order?ref=" + encodeURIComponent(ref))
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
          div.textContent = "\\u2022 " + label;
          box.appendChild(div);
        });
        td.appendChild(box);
      });
    })
    .catch(function (err) { td.textContent = "Failed: " + err; });
}
function cell(row, text, cls, label) {
  var td = document.createElement("td");
  td.textContent = text == null ? "" : text;
  if (cls) { td.className = cls; }
  if (label) { td.setAttribute("data-label", label); }
  row.appendChild(td);
  return td;
}
function togglePrinted(ref, kind, now) {
  api("/printed", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ref: ref, kind: kind, printed: !now})
  }).then(function (r) { return r.json(); }).then(function (data) {
    if (!data.ok) {
      setStatus("Failed: " + data.error);
      return;
    }
    loadOrders();
  }).catch(function (err) { setStatus("Failed: " + err); });
}
function markCell(order, kind, printed) {
  var td = document.createElement("td");
  td.className = "mark";
  td.textContent = printed ? "yes" : "no";
  if (printed) { td.classList.add("done"); }
  td.title = "Click to toggle";
  td.addEventListener("click", function () {
    togglePrinted(order.name, kind, printed);
  });
  return td;
}
function addOrder(order) {
  var tr = document.createElement("tr");
  var nameCell = cell(tr, order.name, null, "Order");
  nameCell.className = "name";
  cell(tr, (order.date_order || "").slice(0, 16).replace("T", " "),
       null, "Date");
  cell(tr, order.state, null, "State");
  cell(tr, order.amount_total == null ? "" : order.amount_total.toFixed(2),
       "num", "Total");
  cell(tr, order.partner_id || "", null, "Customer");
  var receiptCell = markCell(order, "receipt", !!order.printed);
  receiptCell.setAttribute("data-label", "Receipt");
  tr.appendChild(receiptCell);
  var packingCell = markCell(order, "internal", !!order.printed_internal);
  packingCell.setAttribute("data-label", "Packing");
  tr.appendChild(packingCell);
  var actions = document.createElement("td");
  actions.className = "actions";
  actions.setAttribute("data-label", "Actions");
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
  api("/orders?" + params.toString())
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (!data.ok) {
        hint.textContent = "Failed: " + data.error;
        return;
      }
      hint.textContent = "";
      var hide = document.getElementById("hide").value;
      var shown = 0;
      (data.orders || []).forEach(function (order) {
        if (hide == "either" && (order.printed || order.printed_internal)) {
          return;
        }
        if (hide == "both" && order.printed && order.printed_internal) {
          return;
        }
        addOrder(order);
        shown++;
      });
      if (!shown) {
        hint.textContent = "No orders to show.";
      }
    })
    .catch(function (err) { hint.textContent = "Failed: " + err; });
}
document.getElementById("refresh").addEventListener("click", loadOrders);
document.getElementById("limit").addEventListener("change", loadOrders);
document.getElementById("state").addEventListener("change", loadOrders);
document.getElementById("website").addEventListener("change", loadOrders);
document.getElementById("hide").addEventListener("change", loadOrders);
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
""", nav=nav)


SETTINGS_PAGE = page("System settings - Odoo Receipt Bridge", """
<h1>System settings</h1>
<p id="status" class="toast"></p>
<div class="card">
<h2>Users</h2>
<table id="users" class="responsive"><thead><tr><th>Username</th><th>Role</th>
<th>Actions</th></tr></thead><tbody></tbody></table>
<h3>Add a user</h3>
<p class="row">
<input type="text" id="new-user" placeholder="username">
<select id="new-role">
<option value="pos">POS - orders and printing only</option>
<option value="admin">admin - also the settings page</option>
</select>
<input type="password" id="new-pass" placeholder="password">
<button id="add-user" type="button">Add user</button>
</p>
<p class="muted">POS accounts can open the orders page and print receipts.
Admins can also open this page. Passwords are stored as salted hashes with
a server pepper.</p>
</div>

<div class="card">
<h2>Printer</h2>
<p class="row">
<select id="printer-transport">
<option value="net">net (TCP 9100)</option>
<option value="dev">dev (device file)</option>
<option value="cups">cups</option>
<option value="usb">usb (pyusb)</option>
<option value="win">win (Windows RAW)</option>
<option value="none">none</option>
</select>
<input type="text" id="printer-target" class="wide"
       placeholder="192.168.1.50:9100">
<input type="number" id="printer-timeout" placeholder="timeout s" min="1">
<button id="save-printer" type="button">Save and test</button>
</p>
<p class="muted">The transport and the target. net takes HOST[:9100], dev
takes a device path, cups takes a queue name, usb takes VID:PID, win takes
the Windows printer name.</p>
</div>

<div class="card">
<h2>Odoo</h2>
<label>URL, for example https://myshop.odoo.com
<input type="text" id="odoo-url" class="wide">
</label>
<label>Database name (empty for Odoo Online)
<input type="text" id="odoo-db" class="wide">
</label>
<label>API user login
<input type="text" id="odoo-user" class="wide">
</label>
<label>API key
<input type="password" id="odoo-key" class="wide">
</label>
<label>API flavor
<select id="odoo-api">
<option value="jsonrpc">jsonrpc</option>
<option value="json2">json2 (experimental)</option>
</select>
</label>
<label>Timeout in seconds
<input type="number" id="odoo-timeout" min="1" class="wide">
</label>
<p><button id="save-odoo" type="button">Save and test</button></p>
</div>

<div class="card">
<h2>Receipt</h2>
<label>Shop name
<input type="text" id="receipt-shop-name" class="wide">
</label>
<label>Shop address lines, one per line
<textarea id="receipt-shop-address" rows="3" class="wide"></textarea>
</label>
<label>Shop phone
<input type="text" id="receipt-shop-phone" class="wide">
</label>
<label>Footer lines, one per line
<textarea id="receipt-footer" rows="3" class="wide"></textarea>
</label>
<p class="row">
<label class="toggle">Width
<input type="number" id="receipt-width" min="24" max="96"></label>
<label class="toggle">Left margin
<input type="number" id="receipt-margin" min="0" max="16"></label>
<label class="toggle">Timezone
<input type="text" id="receipt-timezone" placeholder="Europe/Brussels"
       size="16"></label>
</p>
<p class="row">
<label class="toggle"><input type="checkbox" id="receipt-unit-price">
Unit prices</label>
<label class="toggle"><input type="checkbox" id="receipt-notes">
Order notes</label>
<label class="toggle"><input type="checkbox" id="receipt-barcode">
Barcode</label>
<label class="toggle"><input type="checkbox" id="receipt-comma">
Decimal comma</label>
</p>
<p class="row">
<label class="toggle">Price mode
<select id="receipt-price-mode">
<option value="total">total</option>
<option value="untaxed">untaxed</option>
</select></label>
<label class="toggle">Packing list with each receipt
<select id="receipt-internal">
<option value="off">off</option>
<option value="with">with</option>
<option value="only">only</option>
</select></label>
</p>
<p><button id="save-receipt" type="button">Save receipt</button></p>
</div>

<div class="card">
<h2>Polling</h2>
<label>Order states to print, comma separated
<input type="text" id="poll-states" class="wide" placeholder="sale,done">
</label>
<p class="row">
<label class="toggle"><input type="checkbox" id="poll-website">
Website orders only</label>
<label class="toggle">Batch size
<input type="number" id="poll-batch" min="1" max="500"></label>
</p>
<p><button id="save-poll" type="button">Save polling</button></p>
</div>

<div class="card">
<h2>Setup wizard</h2>
<p>Run the first-time setup again: create a new administrator account,
reconnect Odoo, and reconnect a printer. The current users keep working
until the wizard is finished.</p>
<p><button id="rerun-setup" class="danger" type="button">
Re-run setup wizard</button></p>
</div>

<script>
"use strict";
var token = "__TOKEN__";
var user = "__USER__";
var role = "__ROLE__";
document.getElementById("who").textContent = user + ", admin";
document.getElementById("logout").addEventListener("click", function () {
  fetch("/logout", {method: "POST"}).then(function () {
    window.location.href = "/login";
  });
});
var statusEl = document.getElementById("status");
var statusTimer = null;
function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = "toast show " + (kind || (text.indexOf("Failed") === 0 ? "err" : "done"));
  if (statusTimer) { clearTimeout(statusTimer); }
  statusTimer = setTimeout(function () {
    statusEl.className = "toast";
  }, 4000);
}
function post(path, payload) {
  return fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Print-Token": token},
    body: JSON.stringify(payload)
  }).then(function (r) { return r.json(); });
}
function loadSettings() {
  post("/settings", {})
    .then(function (r) { return r; })
    .then(function (data) {
    if (!data.ok) { setStatus("Failed: " + data.error); return; }
    var s = data.settings;
    document.getElementById("printer-transport").value =
      s.printer_transport;
    document.getElementById("printer-target").value = s.printer_target;
    document.getElementById("printer-timeout").value = s.printer_timeout;
    document.getElementById("odoo-url").value = s.url;
    document.getElementById("odoo-db").value = s.db;
    document.getElementById("odoo-user").value = s.user;
    var keyInput = document.getElementById("odoo-key");
    keyInput.value = s.has_api_key ? "\u2022".repeat(s.api_key_length) : "";
    keyInput.dataset.placeholderLength = s.has_api_key ? s.api_key_length : 0;
    document.getElementById("odoo-api").value = s.api;
    document.getElementById("odoo-timeout").value = s.timeout;
    document.getElementById("receipt-shop-name").value = s.shop_name;
    document.getElementById("receipt-shop-address").value =
      s.shop_address_lines.join("\\n");
    document.getElementById("receipt-shop-phone").value = s.shop_phone;
    document.getElementById("receipt-footer").value =
      s.footer_lines.join("\\n");
    document.getElementById("receipt-width").value = s.width;
    document.getElementById("receipt-margin").value = s.left_margin;
    document.getElementById("receipt-timezone").value = s.timezone;
    document.getElementById("receipt-unit-price").checked =
      s.show_unit_price;
    document.getElementById("receipt-notes").checked = s.show_notes;
    document.getElementById("receipt-barcode").checked = s.show_barcode;
    document.getElementById("receipt-comma").checked = s.decimal_comma;
    document.getElementById("receipt-price-mode").value = s.price_mode;
    document.getElementById("receipt-internal").value =
      s.internal_receipts;
    document.getElementById("poll-states").value =
      (s.states || []).join(",");
    document.getElementById("poll-website").checked = s.only_website;
    document.getElementById("poll-batch").value = s.batch;
    loadUsers(data.users || []);
  }).catch(function (err) { setStatus("Failed: " + err); });
}
var userTable = document.querySelector("#users tbody");
function loadUsers(users) {
  userTable.replaceChildren();
  users.forEach(function (u) {
    var tr = document.createElement("tr");
    var td = document.createElement("td");
    td.textContent = u.username;
    td.setAttribute("data-label", "Username");
    tr.appendChild(td);
    td = document.createElement("td");
    td.textContent = u.role;
    td.setAttribute("data-label", "Role");
    tr.appendChild(td);
    var actions = document.createElement("td");
    actions.className = "actions";
    actions.setAttribute("data-label", "Actions");
    var select = document.createElement("select");
    ["admin", "pos"].forEach(function (r) {
      var option = document.createElement("option");
      option.value = r;
      option.textContent = r;
      if (u.role === r) { option.selected = true; }
      select.appendChild(option);
    });
    select.addEventListener("change", function () {
      post("/users/role", {username: u.username, role: select.value})
        .then(function (data) {
        if (!data.ok) { setStatus("Failed: " + data.error); loadUsers2(); }
      });
    });
    var pass = document.createElement("input");
    pass.type = "password";
    pass.placeholder = "new password";
    pass.size = 12;
    var setBtn = document.createElement("button");
    setBtn.type = "button";
    setBtn.textContent = "Set";
    setBtn.addEventListener("click", function () {
      post("/users/password", {username: u.username, password: pass.value})
        .then(function (data) {
        setStatus(data.ok ? "Password updated for " + u.username + "."
                          : "Failed: " + data.error);
        pass.value = "";
      });
    });
    var delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "danger";
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", function () {
      post("/users/delete", {username: u.username}).then(function (data) {
        if (!data.ok) { setStatus("Failed: " + data.error); return; }
        loadUsers2();
      });
    });
    actions.appendChild(select);
    actions.appendChild(document.createTextNode(" "));
    actions.appendChild(pass);
    actions.appendChild(document.createTextNode(" "));
    actions.appendChild(setBtn);
    actions.appendChild(document.createTextNode(" "));
    actions.appendChild(delBtn);
    tr.appendChild(actions);
    userTable.appendChild(tr);
  });
}
function loadUsers2() {
  fetch("/settings", {headers: {"X-Print-Token": token}})
    .then(function (r) { return r.json(); })
    .then(function (data) { loadUsers(data.users || []); });
}
document.getElementById("add-user").addEventListener("click", function () {
  post("/users/add", {
    username: document.getElementById("new-user").value,
    password: document.getElementById("new-pass").value,
    role: document.getElementById("new-role").value
  }).then(function (data) {
    if (!data.ok) { setStatus("Failed: " + data.error); return; }
    setStatus("User added.");
    document.getElementById("new-user").value = "";
    document.getElementById("new-pass").value = "";
    loadUsers2();
  });
});
document.getElementById("save-printer").addEventListener("click", function () {
  var timeout = parseInt(
    document.getElementById("printer-timeout").value, 10);
  post("/settings/printer", {
    transport: document.getElementById("printer-transport").value,
    target: document.getElementById("printer-target").value,
    timeout: isNaN(timeout) ? 5 : timeout
  }).then(function (data) {
    if (!data.ok) { setStatus("Failed: " + data.error); return; }
    setStatus(data.note || "Settings saved.", data.warning ? "err" : "done");
  });
});
document.getElementById("save-odoo").addEventListener("click", function () {
  var timeout = parseInt(
    document.getElementById("odoo-timeout").value, 10);
  post("/settings/odoo", {
    url: document.getElementById("odoo-url").value,
    db: document.getElementById("odoo-db").value,
    user: document.getElementById("odoo-user").value,
    api_key: (function () {
      var key = document.getElementById("odoo-key").value;
      var stored = parseInt(
        document.getElementById("odoo-key").dataset.placeholderLength || "0",
        10);
      if (!key) { return ""; }
      if (stored && key.split("\u2022").join("") === "" &&
          key.length === stored) { return undefined; }
      return key;
    })(),
    api: document.getElementById("odoo-api").value,
    timeout: isNaN(timeout) ? 30 : timeout
  }).then(function (data) {
    if (!data.ok) { setStatus("Failed: " + data.error); return; }
    setStatus(data.note || "Settings saved.", data.warning ? "err" : "done");
  });
});
document.getElementById("save-receipt").addEventListener("click", function () {
  post("/settings/receipt", {
    shop_name: document.getElementById("receipt-shop-name").value,
    shop_address_lines:
      document.getElementById("receipt-shop-address").value.split("\\n"),
    shop_phone: document.getElementById("receipt-shop-phone").value,
    footer_lines:
      document.getElementById("receipt-footer").value.split("\\n"),
    width: parseInt(document.getElementById("receipt-width").value, 10),
    left_margin:
      parseInt(document.getElementById("receipt-margin").value, 10),
    timezone: document.getElementById("receipt-timezone").value,
    show_unit_price:
      document.getElementById("receipt-unit-price").checked,
    show_notes: document.getElementById("receipt-notes").checked,
    show_barcode: document.getElementById("receipt-barcode").checked,
    decimal_comma: document.getElementById("receipt-comma").checked,
    price_mode: document.getElementById("receipt-price-mode").value,
    internal_receipts: document.getElementById("receipt-internal").value
  }).then(function (data) {
    setStatus(data.ok ? "Settings saved." : "Failed: " + data.error);
  });
});
document.getElementById("save-poll").addEventListener("click", function () {
  var batch = parseInt(document.getElementById("poll-batch").value, 10);
  post("/settings/poll", {
    states: document.getElementById("poll-states").value,
    only_website: document.getElementById("poll-website").checked,
    batch: isNaN(batch) ? 50 : batch
  }).then(function (data) {
    setStatus(data.ok ? "Settings saved." : "Failed: " + data.error);
  });
});
document.getElementById("rerun-setup")
  .addEventListener("click", function () {
  if (!window.confirm("Re-run the setup wizard? You will set up an " +
    "administrator account again.")) { return; }
  post("/setup/reset", {}).then(function (data) {
    if (data.ok) { window.location.href = "/quickstart"; }
  });
});
loadSettings();
</script>
""", nav=NAV_ADMIN)
