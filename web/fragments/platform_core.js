/* Platform core UI fragment — Forcelet.
 *
 * WIRE-UP (for the main agent): this file is NOT loaded by web/index.html
 * yet. Include it with <script src="fragments/platform_core.js"></script>
 * and call PlatformCore.renderTabs(container) from the setup/admin area,
 * passing the same auth-header helper the main UI uses.
 *
 * No emojis anywhere in this UI.
 */
(function (global) {
  "use strict";

  var BASE = "/api/platform";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function rowHtml(item, cols) {
    return "<tr>" + cols.map(function (c) {
      return "<td>" + esc(item[c]) + "</td>";
    }).join("") + "<td><button data-del=\"" + esc(item.Id) +
      "\">Delete</button></td></tr>";
  }

  // Generic CRUD section: list + create form + delete buttons.
  function crudSection(container, headers, title, route, fields, listCols) {
    var div = document.createElement("div");
    div.className = "pc-section";
    div.innerHTML = "<h3>" + esc(title) + "</h3>" +
      "<table class=\"pc-table\"><thead><tr>" +
      listCols.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") +
      "<th></th></tr></thead><tbody></tbody></table>" +
      "<form class=\"pc-form\">" +
      fields.map(function (f) {
        return "<label>" + esc(f.label) +
          "<input name=\"" + esc(f.name) + "\" placeholder=\"" +
          esc(f.placeholder || "") + "\"></label>";
      }).join("") +
      "<button type=\"submit\">Add</button></form>" +
      "<p class=\"pc-msg\"></p>";

    function refresh() {
      fetch(BASE + route, { headers: headers() }).then(function (r) { return r.json(); })
        .then(function (items) {
          var tb = div.querySelector("tbody");
          tb.innerHTML = (items || []).map(function (it) {
            return rowHtml(it, listCols);
          }).join("");
        });
    }
    div.querySelector("tbody").addEventListener("click", function (e) {
      var id = e.target.getAttribute && e.target.getAttribute("data-del");
      if (!id) return;
      fetch(BASE + route + "/" + id, { method: "DELETE", headers: headers() })
        .then(refresh);
    });
    div.querySelector("form").addEventListener("submit", function (e) {
      e.preventDefault();
      var body = {};
      fields.forEach(function (f) {
        var v = div.querySelector("[name=\"" + f.name + "\"]").value.trim();
        if (v) body[f.name] = v;
      });
      fetch(BASE + route, {
        method: "POST", headers: headers(true), body: JSON.stringify(body)
      }).then(function (r) { return r.json(); }).then(function (res) {
        var msg = div.querySelector(".pc-msg");
        if (res && res.error) { msg.textContent = res.error; return; }
        msg.textContent = "";
        e.target.reset();
        refresh();
      });
    });
    container.appendChild(div);
    refresh();
  }

  function duplicatesTab(container, headers) {
    var div = document.createElement("div");
    div.className = "pc-section";
    div.innerHTML = "<h3>Duplicate check (dry run)</h3>" +
      "<form class=\"pc-form\">" +
      "<label>Object<input name=\"object\" value=\"Lead\"></label>" +
      "<label>Field values (JSON)<input name=\"values\" " +
      "placeholder='{\"Email\":\"a@b.com\"}'></label>" +
      "<button type=\"submit\">Check</button></form>" +
      "<pre class=\"pc-out\"></pre>";
    div.querySelector("form").addEventListener("submit", function (e) {
      e.preventDefault();
      var object = div.querySelector("[name=object]").value.trim();
      var values;
      try { values = JSON.parse(div.querySelector("[name=values]").value || "{}"); }
      catch (err) { div.querySelector(".pc-out").textContent = "Invalid JSON"; return; }
      fetch(BASE + "/duplicates/check", {
        method: "POST", headers: headers(true),
        body: JSON.stringify({ object: object, values: values })
      }).then(function (r) { return r.json(); }).then(function (res) {
        div.querySelector(".pc-out").textContent = JSON.stringify(res, null, 2);
      });
    });
    container.appendChild(div);
  }

  function forecastTab(container, headers) {
    var div = document.createElement("div");
    div.className = "pc-section";
    div.innerHTML = "<h3>Forecast summary</h3>" +
      "<form class=\"pc-form\">" +
      "<label>Owner id<input name=\"owner_id\" placeholder=\"(blank = me)\"></label>" +
      "<label>Period<input name=\"period\" placeholder=\"2026-Q4\"></label>" +
      "<button type=\"submit\">Load</button></form>" +
      "<pre class=\"pc-out\"></pre>";
    div.querySelector("form").addEventListener("submit", function (e) {
      e.preventDefault();
      var q = [];
      var o = div.querySelector("[name=owner_id]").value.trim();
      var p = div.querySelector("[name=period]").value.trim();
      if (o) q.push("owner_id=" + encodeURIComponent(o));
      if (p) q.push("period=" + encodeURIComponent(p));
      fetch(BASE + "/forecasts/summary" + (q.length ? "?" + q.join("&") : ""),
        { headers: headers() }).then(function (r) { return r.json(); })
        .then(function (res) {
          div.querySelector(".pc-out").textContent = JSON.stringify(res, null, 2);
        });
    });
    container.appendChild(div);
  }

  function influenceTab(container, headers) {
    var div = document.createElement("div");
    div.className = "pc-section";
    div.innerHTML = "<h3>Campaign influence</h3>" +
      "<form class=\"pc-form\">" +
      "<label>Opportunity id<input name=\"opportunity_id\"></label>" +
      "<label>Model<select name=\"model\">" +
      ["Primary Campaign Source", "First Touch", "Last Touch", "Even Split"]
        .map(function (m) { return "<option>" + esc(m) + "</option>"; }).join("") +
      "</select></label>" +
      "<button type=\"submit\">Attribute</button></form>" +
      "<pre class=\"pc-out\"></pre>";
    div.querySelector("form").addEventListener("submit", function (e) {
      e.preventDefault();
      var opp = div.querySelector("[name=opportunity_id]").value.trim();
      var model = div.querySelector("[name=model]").value;
      fetch(BASE + "/campaign-influence/attribute", {
        method: "POST", headers: headers(true),
        body: JSON.stringify({ opportunity_id: opp, model: model })
      }).then(function (r) { return r.json(); }).then(function () {
        return fetch(BASE + "/campaign-influence/report?opportunity_id=" +
          encodeURIComponent(opp), { headers: headers() });
      }).then(function (r) { return r.json(); }).then(function (res) {
        div.querySelector(".pc-out").textContent = JSON.stringify(res, null, 2);
      });
    });
    container.appendChild(div);
  }

  var PlatformCore = {
    // headers: () -> auth header dict; json=true adds Content-Type.
    renderTabs: function (container, headers) {
      function h(json) {
        return function () {
          var d = headers();
          if (json) d["Content-Type"] = "application/json";
          return d;
        };
      }
      crudSection(container, h, "Matching rules", "/matching-rules", [
        { name: "Name", label: "Name", placeholder: "Lead email match" },
        { name: "ObjectName", label: "Object", placeholder: "Lead" },
        { name: "Fields", label: "Fields (comma-separated)", placeholder: "Email" },
        { name: "MatchType", label: "Match type", placeholder: "Exact or Fuzzy" }
      ], ["Name", "ObjectName", "Fields", "MatchType"]);
      crudSection(container, h, "Duplicate rules", "/duplicate-rules", [
        { name: "Name", label: "Name", placeholder: "Block dup leads" },
        { name: "ObjectName", label: "Object", placeholder: "Lead" },
        { name: "MatchingRuleId", label: "Matching rule id", placeholder: "(id)" },
        { name: "Action", label: "Action", placeholder: "Block or Warn" },
        { name: "Message", label: "Message", placeholder: "Possible duplicate" },
        { name: "AppliesOn", label: "Applies on", placeholder: "Create, Update or Both" }
      ], ["Name", "ObjectName", "Action", "AppliesOn"]);
      duplicatesTab(container, h);
      crudSection(container, h, "Forecast quotas", "/forecast-quotas", [
        { name: "Name", label: "Name", placeholder: "Q4 quota" },
        { name: "OwnerId", label: "Owner id", placeholder: "(user id)" },
        { name: "Period", label: "Period", placeholder: "2026-Q4" },
        { name: "QuotaAmount", label: "Quota amount", placeholder: "100000" }
      ], ["Name", "OwnerId", "Period", "QuotaAmount"]);
      forecastTab(container, h);
      crudSection(container, h, "Email alerts", "/email-alerts", [
        { name: "Name", label: "Name", placeholder: "Closed-won notice" },
        { name: "ObjectName", label: "Object", placeholder: "Opportunity" },
        { name: "TriggerEvent", label: "Event", placeholder: "Create or Update" },
        { name: "Recipients", label: "Recipients", placeholder: "user id or email, comma-separated" },
        { name: "EmailTemplateId", label: "Template id", placeholder: "(template id)" },
        { name: "Criteria", label: "Criteria JSON", placeholder: "{\"Stage\":\"Closed Won\"}" }
      ], ["Name", "ObjectName", "TriggerEvent"]);
      influenceTab(container, h);
    }
  };

  global.PlatformCore = PlatformCore;
})(window);
