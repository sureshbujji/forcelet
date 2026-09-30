/* Service core UI: case teams, entitlements, notes.
 *
 * Fragment for the Forcelet single-page app. The main shell wires it by
 * calling ServiceCore.init(api) once (api mirrors the shell's
 * api(method, path, body) helper) and then invoking the render functions
 * with a container element. No emojis are used anywhere in this UI.
 */
(function () {
  "use strict";

  var call = null;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  function fieldRow(label, input) {
    var wrap = el("div", "sc-field");
    wrap.appendChild(el("label", "", esc(label)));
    wrap.appendChild(input);
    return wrap;
  }

  function textInput(value, placeholder) {
    var i = document.createElement("input");
    i.type = "text";
    i.value = value || "";
    if (placeholder) i.placeholder = placeholder;
    return i;
  }

  function selectInput(options, value) {
    var s = document.createElement("select");
    options.forEach(function (o) {
      var opt = document.createElement("option");
      opt.value = o;
      opt.textContent = o;
      if (o === value) opt.selected = true;
      s.appendChild(opt);
    });
    return s;
  }

  function button(label, onClick, primary) {
    var b = el("button", primary ? "sc-btn sc-btn-primary" : "sc-btn", esc(label));
    b.addEventListener("click", onClick);
    return b;
  }

  function notice(container, ok, msg) {
    var n = container.querySelector(".sc-notice");
    if (n) n.remove();
    n = el("div", "sc-notice " + (ok ? "sc-ok" : "sc-err"), esc(msg));
    container.prepend(n);
    setTimeout(function () { if (n.parentNode) n.remove(); }, 5000);
  }

  async function refresh(fn, container) {
    container.innerHTML = "";
    await fn(container);
  }

  /* ---------------------------------------------------------- case teams */
  async function renderCaseTeams(container) {
    var head = el("div", "sc-head",
      "<h3>Case Teams</h3><p>Predefined teams that can be assigned to cases. " +
      "Team members gain visibility of the case through the service routes.</p>");
    container.appendChild(head);

    var list = el("div", "sc-list");
    container.appendChild(list);
    var r = await call("GET", "/api/service/case-teams");
    if (!r.ok) { notice(container, false, "Could not load case teams."); return; }
    if (!r.data.length) list.appendChild(el("p", "sc-empty", "No case teams yet."));
    r.data.forEach(function (t) {
      var card = el("div", "sc-card",
        "<strong>" + esc(t.Name) + "</strong><div class='sc-muted'>" +
        esc(t.Description || "") + "</div>");
      var row = el("div", "sc-row");
      row.appendChild(button("Manage", function () {
        renderTeamDetail(container, t.Id);
      }));
      row.appendChild(button("Delete", async function () {
        if (!confirm("Delete team '" + t.Name + "' and its members?")) return;
        var d = await call("DELETE", "/api/service/case-teams/" + t.Id);
        if (d.ok) refresh(renderCaseTeams, container);
        else notice(container, false, d.data.error || "Delete failed.");
      }));
      card.appendChild(row);
      list.appendChild(card);
    });

    var form = el("div", "sc-card");
    form.appendChild(el("h4", "", "New case team"));
    var nameI = textInput("", "Team name");
    var descI = document.createElement("textarea");
    descI.placeholder = "Description";
    form.appendChild(fieldRow("Name", nameI));
    form.appendChild(fieldRow("Description", descI));
    form.appendChild(button("Create team", async function () {
      var c = await call("POST", "/api/service/case-teams",
        { Name: nameI.value.trim(), Description: descI.value.trim() });
      if (c.ok) refresh(renderCaseTeams, container);
      else notice(container, false, c.data.error || "Create failed.");
    }, true));
    container.appendChild(form);
  }

  async function renderTeamDetail(container, teamId) {
    container.innerHTML = "";
    var r = await call("GET", "/api/service/case-teams/" + teamId);
    if (!r.ok) { notice(container, false, "Team not found."); return; }
    var team = r.data.team, members = r.data.members || [];
    container.appendChild(el("div", "sc-head",
      "<h3>" + esc(team.Name) + "</h3><p>" + esc(team.Description || "") + "</p>"));
    container.appendChild(button("Back to teams", function () {
      refresh(renderCaseTeams, container);
    }));

    var list = el("div", "sc-list");
    container.appendChild(list);
    if (!members.length) list.appendChild(el("p", "sc-empty", "No members yet."));
    members.forEach(function (m) {
      var card = el("div", "sc-card",
        "<strong>" + esc(m.UserId) + "</strong>" +
        "<div class='sc-muted'>" + esc(m.TeamRole || "") + "</div>");
      card.appendChild(button("Remove", async function () {
        var d = await call("DELETE",
          "/api/service/case-teams/" + teamId + "/members/" + m.Id);
        if (d.ok) renderTeamDetail(container, teamId);
        else notice(container, false, d.data.error || "Remove failed.");
      }));
      list.appendChild(card);
    });

    var form = el("div", "sc-card");
    form.appendChild(el("h4", "", "Add member"));
    var userI = textInput("", "User ID");
    var roleI = selectInput(["Support Agent", "Support Manager", "SME", "Other"]);
    form.appendChild(fieldRow("User ID", userI));
    form.appendChild(fieldRow("Role", roleI));
    form.appendChild(button("Add member", async function () {
      var c = await call("POST", "/api/service/case-teams/" + teamId + "/members",
        { UserId: userI.value.trim(), TeamRole: roleI.value });
      if (c.ok) renderTeamDetail(container, teamId);
      else notice(container, false, c.data.error || "Add failed.");
    }, true));
    container.appendChild(form);
  }

  async function renderCaseTeamPanel(container, caseId) {
    var r = await call("GET", "/api/service/cases/" + caseId + "/team");
    var box = el("div", "sc-card");
    box.appendChild(el("h4", "", "Case team"));
    if (r.ok) {
      box.appendChild(el("div", "",
        "<strong>" + esc(r.data.team.Name) + "</strong>"));
      (r.data.members || []).forEach(function (m) {
        box.appendChild(el("div", "sc-muted",
          esc(m.UserId) + " — " + esc(m.TeamRole || "")));
      });
    } else {
      box.appendChild(el("p", "sc-muted", "No team assigned."));
    }
    var teams = await call("GET", "/api/service/case-teams");
    if (teams.ok && teams.data.length) {
      var sel = selectInput(teams.data.map(function (t) { return t.Id; }));
      var labels = {};
      teams.data.forEach(function (t) { labels[t.Id] = t.Name; });
      Array.prototype.forEach.call(sel.options, function (o) {
        o.textContent = labels[o.value] || o.value;
      });
      box.appendChild(fieldRow("Assign team", sel));
      box.appendChild(button("Assign", async function () {
        var a = await call("POST", "/api/service/cases/" + caseId + "/assign-team",
          { team_def_id: sel.value });
        if (a.ok) renderCaseTeamPanel(container, caseId);
        else notice(container, false, a.data.error || "Assign failed.");
      }, true));
    }
    container.appendChild(box);
  }

  /* --------------------------------------------------------- entitlements */
  async function renderEntitlements(container) {
    container.appendChild(el("div", "sc-head",
      "<h3>Entitlements</h3><p>Support entitlements per account. " +
      "Status derives from the start/end dates; overlapping active " +
      "entitlements of the same type are rejected.</p>"));
    var list = el("div", "sc-list");
    container.appendChild(list);
    var r = await call("GET", "/api/service/entitlements");
    if (!r.ok) { notice(container, false, "Could not load entitlements."); return; }
    if (!r.data.length) list.appendChild(el("p", "sc-empty", "No entitlements yet."));
    r.data.forEach(function (e) {
      var card = el("div", "sc-card",
        "<strong>" + esc(e.Name) + "</strong> " +
        "<span class='sc-badge'>" + esc(e.Status || "") + "</span>" +
        "<div class='sc-muted'>" + esc(e.Type || "") + " · " +
        esc(e.StartDate || "—") + " to " + esc(e.EndDate || "—") + "</div>");
      card.appendChild(button("Delete", async function () {
        if (!confirm("Delete entitlement '" + e.Name + "'?")) return;
        var d = await call("DELETE", "/api/service/entitlements/" + e.Id);
        if (d.ok) refresh(renderEntitlements, container);
        else notice(container, false, d.data.error || "Delete failed.");
      }));
      list.appendChild(card);
    });

    var form = el("div", "sc-card");
    form.appendChild(el("h4", "", "New entitlement"));
    var nameI = textInput("", "Entitlement name");
    var acctI = textInput("", "Account ID");
    var typeI = selectInput(["Phone Support", "Web Support", "Premier"]);
    var startI = textInput("", "Start date (YYYY-MM-DD)");
    var endI = textInput("", "End date (YYYY-MM-DD)");
    form.appendChild(fieldRow("Name", nameI));
    form.appendChild(fieldRow("Account ID", acctI));
    form.appendChild(fieldRow("Type", typeI));
    form.appendChild(fieldRow("Start date", startI));
    form.appendChild(fieldRow("End date", endI));
    form.appendChild(button("Create entitlement", async function () {
      var c = await call("POST", "/api/service/entitlements", {
        Name: nameI.value.trim(), AccountId: acctI.value.trim() || null,
        Type: typeI.value,
        StartDate: startI.value.trim() || null,
        EndDate: endI.value.trim() || null
      });
      if (c.ok) refresh(renderEntitlements, container);
      else notice(container, false, c.data.error || "Create failed.");
    }, true));
    container.appendChild(form);
  }

  async function renderEntitlementProcesses(container) {
    container.appendChild(el("div", "sc-head",
      "<h3>Entitlement Processes</h3><p>Ordered milestone templates. " +
      "Applying an entitlement stamps these milestones onto the case.</p>"));
    var list = el("div", "sc-list");
    container.appendChild(list);
    var r = await call("GET", "/api/service/entitlement-processes");
    if (!r.ok) { notice(container, false, "Could not load processes."); return; }
    if (!r.data.length) list.appendChild(el("p", "sc-empty", "No processes yet."));
    r.data.forEach(function (p) {
      var card = el("div", "sc-card", "<strong>" + esc(p.Name) + "</strong>" +
        "<div class='sc-muted'>" + esc(p.Description || "") + "</div>");
      var row = el("div", "sc-row");
      row.appendChild(button("Milestones", function () {
        renderProcessMilestones(container, p.Id, p.Name);
      }));
      row.appendChild(button("Delete", async function () {
        if (!confirm("Delete process '" + p.Name + "'?")) return;
        var d = await call("DELETE", "/api/service/entitlement-processes/" + p.Id);
        if (d.ok) refresh(renderEntitlementProcesses, container);
        else notice(container, false, d.data.error || "Delete failed.");
      }));
      card.appendChild(row);
      list.appendChild(card);
    });

    var form = el("div", "sc-card");
    form.appendChild(el("h4", "", "New process"));
    var nameI = textInput("", "Process name");
    var descI = document.createElement("textarea");
    form.appendChild(fieldRow("Name", nameI));
    form.appendChild(fieldRow("Description", descI));
    form.appendChild(button("Create process", async function () {
      var c = await call("POST", "/api/service/entitlement-processes",
        { Name: nameI.value.trim(), Description: descI.value.trim() });
      if (c.ok) refresh(renderEntitlementProcesses, container);
      else notice(container, false, c.data.error || "Create failed.");
    }, true));
    container.appendChild(form);
  }

  async function renderProcessMilestones(container, pid, pname) {
    container.innerHTML = "";
    container.appendChild(el("div", "sc-head",
      "<h3>Milestones — " + esc(pname) + "</h3>"));
    container.appendChild(button("Back to processes", function () {
      refresh(renderEntitlementProcesses, container);
    }));
    var list = el("div", "sc-list");
    container.appendChild(list);
    var r = await call("GET",
      "/api/service/entitlement-processes/" + pid + "/milestones");
    if (r.ok) {
      if (!r.data.length) list.appendChild(el("p", "sc-empty", "No milestones yet."));
      r.data.forEach(function (m) {
        var card = el("div", "sc-card",
          "<strong>" + esc(m.Name) + "</strong>" +
          "<div class='sc-muted'>" + esc(String(m.TargetMinutes)) +
          " minutes · order " + esc(String(m.MilestoneOrder == null ? "—" : m.MilestoneOrder)) + "</div>");
        card.appendChild(button("Remove", async function () {
          var d = await call("DELETE",
            "/api/service/entitlement-processes/" + pid + "/milestones/" + m.id);
          if (d.ok) renderProcessMilestones(container, pid, pname);
          else notice(container, false, d.data.error || "Remove failed.");
        }));
        list.appendChild(card);
      });
    }
    var form = el("div", "sc-card");
    form.appendChild(el("h4", "", "Add milestone"));
    var nameI = textInput("", "Milestone name");
    var minsI = textInput("", "Target minutes");
    var orderI = textInput("", "Order");
    form.appendChild(fieldRow("Name", nameI));
    form.appendChild(fieldRow("Target minutes", minsI));
    form.appendChild(fieldRow("Order", orderI));
    form.appendChild(button("Add milestone", async function () {
      var c = await call("POST",
        "/api/service/entitlement-processes/" + pid + "/milestones", {
          Name: nameI.value.trim(),
          TargetMinutes: parseInt(minsI.value, 10) || 0,
          MilestoneOrder: parseInt(orderI.value, 10) || 0
        });
      if (c.ok) renderProcessMilestones(container, pid, pname);
      else notice(container, false, c.data.error || "Add failed.");
    }, true));
    container.appendChild(form);
  }

  /* ---------------------------------------------------------------- notes */
  async function renderNotes(container, parentType, parentId) {
    var box = el("div", "sc-card");
    box.appendChild(el("h4", "", "Notes"));
    var list = el("div", "sc-list");
    box.appendChild(list);

    async function load() {
      list.innerHTML = "";
      var r = await call("GET", "/api/service/notes?parent_type=" +
        encodeURIComponent(parentType) + "&parent_id=" +
        encodeURIComponent(parentId));
      if (!r.ok) {
        list.appendChild(el("p", "sc-err", esc(r.data.error || "Could not load notes.")));
        return;
      }
      if (!r.data.length) list.appendChild(el("p", "sc-empty", "No notes yet."));
      r.data.forEach(function (n) {
        var item = el("div", "sc-note",
          "<strong>" + esc(n.Title) + "</strong>" +
          (n.IsPrivate ? " <span class='sc-badge'>Private</span>" : "") +
          "<div>" + esc(n.Body || "") + "</div>");
        var row = el("div", "sc-row");
        row.appendChild(button("Delete", async function () {
          if (!confirm("Delete this note?")) return;
          var d = await call("DELETE", "/api/service/notes/" + n.Id);
          if (d.ok) load();
          else notice(box, false, d.data.error || "Delete failed.");
        }));
        item.appendChild(row);
        list.appendChild(item);
      });
    }
    await load();

    var titleI = textInput("", "Title");
    var bodyI = document.createElement("textarea");
    bodyI.placeholder = "Write a note";
    var privI = document.createElement("input");
    privI.type = "checkbox";
    box.appendChild(fieldRow("Title", titleI));
    box.appendChild(fieldRow("Body", bodyI));
    box.appendChild(fieldRow("Private (visible to you and admins only)", privI));
    box.appendChild(button("Add note", async function () {
      var c = await call("POST", "/api/service/notes", {
        Title: titleI.value.trim(), Body: bodyI.value,
        ParentType: parentType, ParentId: parentId,
        IsPrivate: privI.checked
      });
      if (c.ok) { titleI.value = ""; bodyI.value = ""; privI.checked = false; load(); }
      else notice(box, false, c.data.error || "Add failed.");
    }, true));
    container.appendChild(box);
  }

  /* ------------------------------------------------------- public surface */
  window.ServiceCore = {
    init: function (apiFn) { call = apiFn; },
    renderCaseTeams: renderCaseTeams,
    renderTeamDetail: renderTeamDetail,
    renderCaseTeamPanel: renderCaseTeamPanel,
    renderEntitlements: renderEntitlements,
    renderEntitlementProcesses: renderEntitlementProcesses,
    renderProcessMilestones: renderProcessMilestones,
    renderNotes: renderNotes
  };
})();
