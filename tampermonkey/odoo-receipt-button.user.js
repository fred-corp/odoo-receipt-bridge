// ==UserScript==
// @name         Odoo: Print Receipt Button
// @namespace    odoo-receipt
// @version      1.1.0
// @description  Shows a Print Receipt panel in the Odoo backend. The panel sends the open sale order to the local odoo_receipt bridge.
// @match        https://*.odoo.com/*
// @match        http://localhost:8069/*
// @match        http://127.0.0.1:8069/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

// Setup:
//   1. Start the bridge on the machine next to the printer:
//      python odoo_receipt.py serve
//   2. Copy the bridge token from the bridge output.
//   3. Paste the token into BRIDGE_TOKEN below.
//   4. Odoo does not run on odoo.com? Add a @match line with your domain.
//   5. Open a sale order in Odoo. The panel shows at the bottom right.
//      Press Print. On the first print, Chrome asks for permission to
//      reach the local network. Allow it for your Odoo domain.
//
// The bridge must run with a token, or every print request fails with 403.

(function () {
  "use strict";

  // Edit these two values once.
  var BRIDGE_URL = "http://127.0.0.1:8765";
  var BRIDGE_TOKEN = "PASTE_THE_BRIDGE_TOKEN_HERE";

  if (window.top !== window.self) {
    return;  // Do not run inside iframes.
  }

  var CSS = [
    "#odoo-receipt-panel{position:fixed;right:16px;bottom:16px;z-index:99999;",
    "background:#fff;border:1px solid #d8d8d8;border-radius:8px;",
    "padding:10px 12px;box-shadow:0 2px 8px rgba(0,0,0,.15);",
    "font:13px/1.4 sans-serif;color:#303030;width:250px}",
    "#odoo-receipt-panel h4{margin:0 0 6px 0;font-size:13px}",
    "#odoo-receipt-panel input{width:140px;padding:3px 6px;margin-right:4px}",
    "#odoo-receipt-panel .packing{display:block;margin:6px 0 0 0;color:#666}",
    "#odoo-receipt-panel .packing input{width:auto;margin-right:4px}",
    "#odoo-receipt-panel button{padding:4px 12px;cursor:pointer}",
    "#odoo-receipt-panel .status{margin-top:6px;min-height:16px;color:#666}",
    "#odoo-receipt-panel .err{color:#b00020}",
    "#odoo-receipt-panel .ok{color:#0b7a3b}"
  ].join("");

  // Best effort. The last breadcrumb entry usually holds the record name,
  // for example S00042. The user can always type the number by hand.
  function findOrderRef() {
    var roots = document.querySelectorAll(
      ".o_breadcrumb, .breadcrumb, [itemprop='breadcrumb']");
    for (var i = roots.length - 1; i >= 0; i--) {
      var nodes = roots[i].querySelectorAll("*");
      for (var j = nodes.length - 1; j >= 0; j--) {
        var text = (nodes[j].textContent || "").trim();
        if (text && /^[A-Za-z]{1,4}\d{3,}/.test(text)) {
          return text;
        }
      }
    }
    var match = document.title.match(/\b([A-Za-z]{1,4}\d{3,})\b/);
    return match ? match[1] : "";
  }

  var panel = null;

  function status(message, isError) {
    var element = panel.querySelector(".status");
    element.textContent = message;
    element.className = "status " + (isError ? "err" : "ok");
  }

  function print() {
    var input = panel.querySelector("input[placeholder]");
    var ref = (input.value || "").trim() || findOrderRef();
    if (!ref) {
      status("No order number found. Type one first.", true);
      return;
    }
    var internal = panel.querySelector(".packing input").checked;
    status("Printing " + ref + (internal ? " (packing list)" : "") + " ...",
           false);
    fetch(BRIDGE_URL + "/print", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Print-Token": BRIDGE_TOKEN
      },
      body: JSON.stringify({ref: ref, internal: internal})
    }).then(function (response) {
      return response.json();
    }).then(function (data) {
      if (data.ok) {
        status("Printed " + data.order + ".", false);
      } else {
        status("Failed: " + data.error, true);
      }
    }).catch(function (error) {
      status("Failed: " + error + ". Is the bridge running?", true);
    });
  }

  function install() {
    if (document.getElementById("odoo-receipt-panel")) {
      return;
    }
    var style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);

    panel = document.createElement("div");
    panel.id = "odoo-receipt-panel";
    panel.innerHTML =
      "<h4>Receipt printer</h4>" +
      "<input placeholder='S00042'>" +
      "<label class='packing'><input type='checkbox'> Packing list</label>" +
      "<button type='button'>Print</button>" +
      "<div class='status'></div>";

    var input = panel.querySelector("input[placeholder]");
    var manual = false;
    input.addEventListener("input", function () {
      manual = true;  // Keep the typed value until the page changes.
    });
    panel.querySelector("button").addEventListener("click", print);
    document.body.appendChild(panel);

    // Follow the open record. Refresh the input when the user does not
    // edit it by hand.
    setInterval(function () {
      if (manual || document.activeElement === input) {
        return;
      }
      var ref = findOrderRef();
      if (ref) {
        input.value = ref;
      }
    }, 2000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", install);
  } else {
    install();
  }
})();
