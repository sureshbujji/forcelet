/* Sales core UI fragment — Forcelet platform.
 *
 * Vanilla JS helpers rendering sales-core data into host-page containers.
 * The host page must set SalesCore.token (a Bearer session token) before
 * calling the render functions, e.g.:
 *   SalesCore.setToken(token);
 *   SalesCore.renderAccountHierarchy(document.getElementById("hier"), acctId);
 *
 * No emojis are used anywhere in this UI.
 */
window.SalesCore = (function () {
  "use strict";

  var S = { token: null };

  S.setToken = function (t) { S.token = t; };

  function headers() {
    return {
      "Authorization": "Bearer " + (S.token || ""),
      "Content-Type": "application/json"
    };
  }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = headers();
    return fetch(path, opts).then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) throw new Error((body && body.error) || ("HTTP " + r.status));
        return body;
      });
    });
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function money(v) {
    if (v === undefined || v === null || v === "") return "-";
    return "$" + Number(v).toFixed(2);
  }

  function loading(container, msg) {
    container.innerHTML = "";
    container.appendChild(el("div", "sc-muted", msg || "Loading..."));
  }

  function fail(container, err) {
    container.innerHTML = "";
    container.appendChild(el("div", "sc-error", "Error: " + err.message));
  }

  /* ---------------- account hierarchy tree ---------------- */
  function treeNode(node) {
    var li = el("li", "sc-tree-node");
    var label = el("span", "sc-tree-label", node.Name || node.Id);
    li.appendChild(label);
    if (node.children && node.children.length) {
      var ul = el("ul", "sc-tree");
      node.children.forEach(function (c) { ul.appendChild(treeNode(c)); });
      li.appendChild(ul);
    }
    return li;
  }

  S.renderAccountHierarchy = function (container, accountId) {
    loading(container, "Loading hierarchy...");
    api("/api/sales/accounts/" + accountId + "/hierarchy").then(function (d) {
      container.innerHTML = "";
      var wrap = el("div", "sc-hierarchy");
      if (d.ancestors && d.ancestors.length) {
        var crumb = el("div", "sc-crumb");
        d.ancestors.forEach(function (a, i) {
          if (i) crumb.appendChild(el("span", "sc-sep", " / "));
          var link = el("a", "sc-crumb-link", a.Name || a.Id);
          link.href = "#";
          link.onclick = function (e) {
            e.preventDefault();
            S.renderAccountHierarchy(container, a.Id);
          };
          crumb.appendChild(link);
        });
        wrap.appendChild(crumb);
      }
      var root = el("div", "sc-tree-root", (d.account && d.account.Name) || accountId);
      wrap.appendChild(root);
      if (d.children && d.children.length) {
        var ul = el("ul", "sc-tree");
        d.children.forEach(function (c) { ul.appendChild(treeNode(c)); });
        wrap.appendChild(ul);
      } else {
        wrap.appendChild(el("div", "sc-muted", "No child accounts."));
      }
      container.appendChild(wrap);
    }).catch(function (e) { fail(container, e); });
  };

  /* ---------------- opportunity line items ---------------- */
  S.renderLineItems = function (container, opportunityId) {
    loading(container, "Loading products...");
    api("/api/sales/opportunity-line-items?opportunity_id=" + opportunityId)
      .then(function (rows) {
        container.innerHTML = "";
        var wrap = el("div", "sc-lineitems");
        if (!rows.length) {
          wrap.appendChild(el("div", "sc-muted", "No products on this opportunity."));
        } else {
          var table = el("table", "sc-table");
          var head = el("tr", null);
          ["Product", "Qty", "Unit Price", "Discount %", "Total"].forEach(function (h) {
            head.appendChild(el("th", null, h));
          });
          table.appendChild(head);
          var total = 0;
          rows.forEach(function (r) {
            var tr = el("tr", null);
            tr.appendChild(el("td", null, r.ProductId || r.Description || "-"));
            tr.appendChild(el("td", "sc-num", String(r.Quantity)));
            tr.appendChild(el("td", "sc-num", money(r.UnitPrice)));
            tr.appendChild(el("td", "sc-num", String(r.Discount || 0)));
            tr.appendChild(el("td", "sc-num", money(r.TotalPrice)));
            total += Number(r.TotalPrice || 0);
            table.appendChild(tr);
          });
          var ft = el("tr", "sc-total");
          ft.appendChild(el("td", null, "Total"));
          ft.appendChild(el("td", null, ""));
          ft.appendChild(el("td", null, ""));
          ft.appendChild(el("td", null, ""));
          ft.appendChild(el("td", "sc-num", money(total)));
          table.appendChild(ft);
          wrap.appendChild(table);
        }
        container.appendChild(wrap);
      }).catch(function (e) { fail(container, e); });
  };

  /* ---------------- opportunity contact roles ---------------- */
  S.renderContactRoles = function (container, opportunityId) {
    loading(container, "Loading contact roles...");
    api("/api/sales/opportunities/" + opportunityId + "/contact-roles")
      .then(function (rows) {
        container.innerHTML = "";
        var wrap = el("div", "sc-roles");
        if (!rows.length) {
          wrap.appendChild(el("div", "sc-muted", "No contact roles."));
        } else {
          var ul = el("ul", "sc-list");
          rows.forEach(function (r) {
            var name = (r.contact && r.contact.Name) || r.ContactId || "-";
            var bits = name + " — " + (r.Role || "Other");
            if (r.IsPrimary) bits += " (Primary)";
            if (r.contact && r.contact.Title) bits += " · " + r.contact.Title;
            ul.appendChild(el("li", null, bits));
          });
          wrap.appendChild(ul);
        }
        container.appendChild(wrap);
      }).catch(function (e) { fail(container, e); });
  };

  /* ---------------- opportunity team + splits ---------------- */
  S.renderTeam = function (container, opportunityId) {
    loading(container, "Loading team...");
    api("/api/sales/opportunities/" + opportunityId + "/team")
      .then(function (d) {
        container.innerHTML = "";
        var wrap = el("div", "sc-team");
        if (!d.members.length) {
          wrap.appendChild(el("div", "sc-muted", "No team members."));
        } else {
          var ul = el("ul", "sc-list");
          d.members.forEach(function (m) {
            var label = (m.username || m.UserId) + " — " + (m.TeamRole || "") +
              " (" + (m.AccessLevel || "Read") + ")";
            (m.splits || []).forEach(function (s) {
              label += " · " + s.SplitType + " " + s.SplitPercentage + "%";
            });
            ul.appendChild(el("li", null, label));
          });
          wrap.appendChild(ul);
        }
        var totals = d.split_totals || {};
        var keys = Object.keys(totals);
        if (keys.length) {
          wrap.appendChild(el("div", "sc-muted",
            "Split totals: " + keys.map(function (k) {
              return k + " " + totals[k] + "%";
            }).join(", ")));
        }
        container.appendChild(wrap);
      }).catch(function (e) { fail(container, e); });
  };

  return S;
})();
