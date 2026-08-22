const DOMAIN = "fortigate_policy";
const ICON_BASE = "/fortiap_presence_static/icons-color";

const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

const clone = (value) => JSON.parse(JSON.stringify(value));
const uid = (prefix) =>
  `${prefix}_${globalThis.crypto?.randomUUID?.() || Date.now().toString(36)}`;

class FortiApPresencePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._loaded = false;
    this._loading = true;
    this._saving = false;
    this._tab = "overview";
    this._data = null;
    this._draft = null;
    this._entries = [];
    this._discovered = [];
    this._error = "";
    this._notice = "";
  }

  set hass(value) {
    this._hass = value;
    if (!this._loaded && value) {
      this._loaded = true;
      void this._load();
    }
  }

  get hass() {
    return this._hass;
  }

  set panel(value) {
    this._panel = value;
  }

  connectedCallback() {
    this._render();
  }

  async _load(entryId) {
    this._loading = true;
    this._error = "";
    this._render();
    try {
      this._entries = await this.hass.callWS({
        type: `${DOMAIN}/panel/entries`,
      });
      const selected =
        entryId || this._data?.entry_id || this._entries[0]?.entry_id;
      if (!selected) {
        this._data = null;
        this._draft = null;
        return;
      }
      this._data = await this.hass.callWS({
        type: `${DOMAIN}/panel/get`,
        entry_id: selected,
      });
      this._draft = clone(this._data);
      this._discovered = clone(this._data.recent_clients || []);
    } catch (error) {
      this._error = error?.message || "Unable to load FortiAP configuration.";
    } finally {
      this._loading = false;
      this._render();
    }
  }

  async _discover() {
    if (!this._data) return;
    this._notice = "Discovering wireless clients…";
    this._error = "";
    this._render();
    try {
      const result = await this.hass.callWS({
        type: `${DOMAIN}/panel/discover`,
        entry_id: this._data.entry_id,
      });
      const tracked = new Set(this._draft.trackers.map((item) => item.mac));
      this._discovered = result.clients.filter((item) => !tracked.has(item.mac));
      this._notice = `Found ${result.clients.length} associated or recently named wireless clients.`;
    } catch (error) {
      this._notice = "";
      this._error = error?.message || "Wireless client discovery failed.";
    }
    this._render();
  }

  async _save() {
    if (!this._data || this._saving) return;
    this._syncInputs();
    this._saving = true;
    this._error = "";
    this._notice = "Verifying firewall policies and saving…";
    this._render();
    try {
      await this.hass.callWS({
        type: `${DOMAIN}/panel/save`,
        entry_id: this._data.entry_id,
        config: {
          trackers: this._draft.trackers.map((tracker) => ({
            mac: tracker.mac,
            name: tracker.name,
            allowed_ssids: tracker.allowed_ssids || [],
          })),
          users: this._draft.users,
          rules: this._draft.rules,
          policy_ids: this._draft.policies.map((policy) => policy.id),
          settings: this._draft.settings,
        },
      });
      this._notice = "Configuration saved and integration reloaded.";
      await this._load(this._data.entry_id);
    } catch (error) {
      this._error = error?.message || "Unable to save configuration.";
      this._notice = "";
    } finally {
      this._saving = false;
      this._render();
    }
  }

  _syncInputs() {
    if (!this.shadowRoot || !this._draft) return;
    this.shadowRoot.querySelectorAll("[data-model]").forEach((input) => {
      const [group, indexText, field] = input.dataset.model.split(".");
      const target = group === "settings" ? this._draft.settings : this._draft[group]?.[Number(indexText)];
      if (!target) return;
      let value = input.type === "checkbox" ? input.checked : input.value;
      if (input.dataset.kind === "number") value = Number(value);
      if (input.dataset.kind === "csv") {
        value = value
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean);
      }
      target[field || indexText] = value;
    });
  }

  _handleClick(event) {
    const action = event.target.closest("[data-action]");
    if (!action || !this._draft) return;
    this._syncInputs();
    const name = action.dataset.action;
    if (name === "tab") this._tab = action.dataset.tab;
    if (name === "save") void this._save();
    if (name === "reload") void this._load(this._data?.entry_id);
    if (name === "discover") void this._discover();
    if (name === "entry") void this._load(action.dataset.entryId);
    if (name === "remove-tracker") {
      const mac = action.dataset.mac;
      this._draft.trackers = this._draft.trackers.filter((item) => item.mac !== mac);
      this._draft.users.forEach((user) => {
        user.macs = user.macs.filter((item) => item !== mac);
      });
    }
    if (name === "add-tracker") {
      const client = this._discovered.find((item) => item.mac === action.dataset.mac);
      if (client) {
        this._draft.trackers.push({
          mac: client.mac,
          name: client.hostname || client.mac,
          allowed_ssids: [],
          state: client.ssid ? "home" : "unavailable",
          available: true,
          client,
        });
        if (client.ssid && !this._draft.known_ssids.includes(client.ssid)) {
          this._draft.known_ssids.push(client.ssid);
        }
        this._discovered = this._discovered.filter((item) => item.mac !== client.mac);
      }
    }
    if (name === "add-person") {
      const input = this.shadowRoot.querySelector("#new-person-name");
      const personName = input?.value.trim();
      if (!personName) {
        this._error = "Enter a person name before adding the person.";
        this._render();
        return;
      }
      this._error = "";
      this._draft.users.unshift({
        id: uid("person"),
        name: personName,
        macs: [],
        away_grace_period: 180,
      });
    }
    if (name === "remove-person") {
      const id = action.dataset.id;
      this._draft.users = this._draft.users.filter((item) => item.id !== id);
      this._draft.rules.forEach((rule) => {
        rule.users = rule.users.filter((item) => item !== id);
      });
    }
    if (name === "toggle-person-device") {
      const user = this._draft.users.find((item) => item.id === action.dataset.id);
      const mac = action.dataset.mac;
      if (user) {
        if (event.target.checked) {
          this._draft.users.forEach((item) => {
            item.macs = item.macs.filter((value) => value !== mac);
          });
          user.macs.push(mac);
        } else {
          user.macs = user.macs.filter((value) => value !== mac);
        }
      }
    }
    if (name === "add-policy") {
      const input = this.shadowRoot.querySelector("#new-policy-id");
      const id = input?.value.trim();
      if (id && /^\d+$/.test(id) && !this._draft.policies.some((item) => item.id === id)) {
        this._draft.policies.push({ id, name: "Will be verified on save", state: "pending" });
        input.value = "";
      }
    }
    if (name === "remove-policy") {
      const id = action.dataset.id;
      this._draft.policies = this._draft.policies.filter((item) => item.id !== id);
      this._draft.rules.forEach((rule) => {
        rule.policies = rule.policies.filter((item) => item !== id);
      });
    }
    if (name === "add-rule") {
      this._draft.rules.push({
        id: uid("rule"),
        name: "New rule",
        users: [],
        match: "any",
        presence: "away",
        action: "disable",
        policies: [],
        priority: 50,
        schedule: "",
      });
    }
    if (name === "remove-rule") {
      this._draft.rules = this._draft.rules.filter((item) => item.id !== action.dataset.id);
    }
    if (name === "toggle-rule-list") {
      const rule = this._draft.rules.find((item) => item.id === action.dataset.id);
      const field = action.dataset.field;
      const value = action.dataset.value;
      if (rule) {
        rule[field] = event.target.checked
          ? [...new Set([...rule[field], value])]
          : rule[field].filter((item) => item !== value);
      }
    }
    this._render();
  }

  _styles() {
    return `<style>
      :host{display:block;min-height:100vh;background:var(--primary-background-color);color:var(--primary-text-color);font-family:var(--paper-font-body1_-_font-family,system-ui,sans-serif);--panel-blue:#03a9f4;--panel-green:#2eaf68;--panel-purple:#9c6ade;--panel-orange:#f59e0b}
      *{box-sizing:border-box}.mdi-icon{display:block;width:1em;height:1em;flex:0 0 auto;object-fit:contain}.shell{max-width:1500px;margin:0 auto;padding:24px 28px 110px}.top{position:relative;overflow:hidden;display:flex;align-items:center;justify-content:space-between;gap:24px;margin-bottom:18px;padding:22px 24px;background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:18px;box-shadow:var(--ha-card-box-shadow,0 2px 8px rgba(0,0,0,.12))}.top:before{content:"";position:absolute;inset:0 auto 0 0;width:5px;background:var(--primary-color)}.brand{display:flex;align-items:center;gap:16px}.brand-mark{display:grid;place-items:center;width:52px;height:52px;border-radius:15px;background:color-mix(in srgb,var(--primary-color) 18%,var(--card-background-color));color:var(--primary-color)}.brand-mark .mdi-icon{width:30px;height:30px}.eyebrow{color:var(--primary-color);font-weight:750;font-size:12px;text-transform:uppercase;letter-spacing:.1em}h1{font-size:30px;line-height:1.15;margin:4px 0 7px}h2{font-size:20px;margin:0 0 5px}h3{font-size:16px;margin:0 0 4px}.muted{color:var(--secondary-text-color)}
      .tabs{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:8px;margin-bottom:20px;padding:7px;background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:14px;box-shadow:var(--ha-card-box-shadow,0 1px 3px rgba(0,0,0,.08));overflow:auto}.tab{display:flex;align-items:center;justify-content:center;gap:9px;border:1px solid transparent;background:transparent;color:var(--secondary-text-color);padding:11px 14px;font:inherit;font-weight:650;cursor:pointer;border-radius:9px;white-space:nowrap}.tab .mdi-icon{width:20px;height:20px}.tab:hover{background:var(--secondary-background-color);color:var(--primary-text-color)}.tab.active{color:var(--primary-color);background:color-mix(in srgb,var(--primary-color) 12%,var(--card-background-color));border-color:color-mix(in srgb,var(--primary-color) 35%,var(--divider-color))}
      .grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:18px}.card{grid-column:span 12;background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:16px;padding:20px;box-shadow:var(--ha-card-box-shadow,0 2px 7px rgba(0,0,0,.1))}.span-3{grid-column:span 3}.span-4{grid-column:span 4}.span-6{grid-column:span 6}.section-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}.section-title{display:flex;align-items:center;gap:11px}.section-icon{display:grid;place-items:center;width:38px;height:38px;border-radius:11px;background:color-mix(in srgb,var(--primary-color) 13%,var(--card-background-color));color:var(--primary-color)}.section-icon .mdi-icon{width:22px;height:22px}.metric-card{position:relative;overflow:hidden;display:flex;align-items:center;gap:15px;min-height:124px}.metric-card:after{content:"";position:absolute;inset:0 0 auto;height:3px;background:var(--metric-color)}.metric-card.blue{--metric-color:var(--panel-blue)}.metric-card.green{--metric-color:var(--panel-green)}.metric-card.purple{--metric-color:var(--panel-purple)}.metric-card.orange{--metric-color:var(--panel-orange)}.metric-icon{display:grid;place-items:center;flex:0 0 46px;width:46px;height:46px;border-radius:50%;color:var(--metric-color);background:color-mix(in srgb,var(--metric-color) 16%,var(--card-background-color))}.metric-icon .mdi-icon{width:25px;height:25px}.metric-label{font-size:13px;font-weight:650;color:var(--secondary-text-color)}.metric{font-size:30px;font-weight:750;line-height:1.05;margin:4px 0}.metric-detail{font-size:12px;color:var(--secondary-text-color);line-height:1.35}.status{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:5px 10px;font-size:12px;font-weight:700;background:var(--secondary-background-color)}.status .mdi-icon{width:14px;height:14px}.status.home,.status.enable,.status.ok{color:#20a05a;background:#20a05a1f}.status.not_home,.status.disable{color:#d97706;background:#f59e0b22}.status.unavailable,.status.pending{color:var(--secondary-text-color)}
      table{width:100%;border-collapse:collapse}th{text-align:left;color:var(--secondary-text-color);font-size:12px;text-transform:uppercase;letter-spacing:.04em;padding:0 10px 9px}td{border-top:1px solid var(--divider-color);padding:12px 10px;vertical-align:middle}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}.stack{display:grid;gap:12px}.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.grow{flex:1}.right{text-align:right}
      input,select{width:100%;min-height:42px;border:1px solid var(--divider-color);border-radius:9px;padding:9px 11px;background:var(--card-background-color);color:var(--primary-text-color);font:inherit}input[type=checkbox]{width:18px;min-height:18px;accent-color:var(--primary-color)}label{display:grid;gap:6px;font-size:13px;font-weight:650;color:var(--secondary-text-color)}.toggle{display:flex;align-items:center;gap:10px;color:var(--primary-text-color);font-weight:500}
      button{font:inherit}.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;border:1px solid var(--divider-color);background:var(--card-background-color);color:var(--primary-text-color);border-radius:9px;padding:9px 13px;font-weight:650;cursor:pointer;text-decoration:none}.btn .mdi-icon{width:18px;height:18px}.btn:hover{background:var(--secondary-background-color)}.btn.primary{background:var(--primary-color);border-color:var(--primary-color);color:var(--text-primary-color,#fff)}.btn.primary:hover{filter:brightness(1.05)}.btn.danger{color:var(--error-color)}.btn:disabled{opacity:.55;cursor:wait}.icon-btn{border:0;background:transparent;color:var(--error-color);cursor:pointer;padding:8px}
      .person,.rule{border:1px solid var(--divider-color);border-radius:12px;padding:16px}.checks{display:flex;gap:10px 18px;flex-wrap:wrap}.checks label{display:flex;align-items:center;gap:7px;color:var(--primary-text-color);font-weight:500}.form-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.add-row{flex-wrap:nowrap}.add-row input{width:260px}.table-scroll{max-height:520px;overflow:auto;border-radius:9px}.table-scroll thead{position:sticky;top:0;background:var(--card-background-color);z-index:1}.notice{border-radius:10px;padding:11px 14px;margin-bottom:16px;background:var(--primary-color);color:var(--text-primary-color,#fff)}.notice.error{background:var(--error-color)}
      .savebar{position:fixed;z-index:5;left:0;right:0;bottom:0;background:color-mix(in srgb,var(--card-background-color) 94%,transparent);backdrop-filter:blur(12px);border-top:1px solid var(--divider-color);padding:14px 28px}.save-inner{max-width:1444px;margin:auto;display:flex;justify-content:space-between;align-items:center;gap:16px}.save-note{display:flex;align-items:center;gap:10px}.save-note>.mdi-icon{width:24px;height:24px;color:var(--primary-color)}.empty{text-align:center;padding:42px;color:var(--secondary-text-color)}
      @media(max-width:900px){.shell{padding:18px 14px 110px}.span-3,.span-4,.span-6{grid-column:span 12}.form-grid{grid-template-columns:1fr 1fr}.top{display:block}.top .row{margin-top:16px}.tabs{grid-template-columns:repeat(4,minmax(145px,1fr))}table{display:block;overflow:auto}.savebar{padding:12px 14px}}
      @media(max-width:560px){.form-grid{grid-template-columns:1fr}h1{font-size:26px}.add-row{width:100%;flex-wrap:wrap}.add-row input{width:100%}}
    </style>`;
  }

  _overview() {
    const d = this._draft;
    const health = d.health || {};
    return `<div class="grid">
      ${this._metric("Tracked devices", d.trackers.length, "Selected MAC addresses only", "cellphone-link", "blue")}
      ${this._metric("People", d.users.length, "Multi-device presence groups", "account-group", "green")}
      ${this._metric("Policy rules", d.rules.length, "Unknown state blocks writes", "source-branch", "purple")}
      ${this._metric("Firewall policies", d.policies.length, "Status-only verified control", "shield-check", "orange")}
      <section class="card span-6"><div class="section-head"><div class="section-title"><span class="section-icon">${this._icon("router-wireless")}</span><div><h2>Connection</h2><div class="muted">FortiGate management path</div></div></div><span class="status ${health.wifi_available ? "ok" : "unavailable"}">${this._icon(health.wifi_available ? "check-circle" : "alert-circle")}${health.wifi_available ? "Wi-Fi API available" : "Wi-Fi API unavailable"}</span></div>
        <div class="form-grid"><div><div class="muted">Host</div><strong>${escapeHtml(d.connection.host)}</strong></div><div><div class="muted">VDOM</div><strong>${escapeHtml(d.connection.vdom)}</strong></div><div><div class="muted">HTTPS port</div><strong>${escapeHtml(d.connection.port)}</strong></div><div><div class="muted">FortiOS</div><strong>${escapeHtml(health.fortios_version || "Unknown")}</strong></div></div>
      </section>
      <section class="card span-6"><div class="section-title"><span class="section-icon">${this._icon("shield-lock-outline")}</span><div><h2>Safety behavior</h2><div class="muted">Conservative presence and policy control</div></div></div><div class="stack muted" style="margin-top:16px"><div>Arrival is immediate after a valid FortiAP association poll.</div><div>Departure waits ${escapeHtml(d.settings.wifi_away_grace_period)} seconds.</div><div>FortiGate failure produces unavailable, never away.</div><div>Policy writes change status only and are read back for verification.</div></div></section>
      <section class="card"><div class="section-head"><div class="section-title"><span class="section-icon">${this._icon("account-multiple-check")}</span><div><h2>People at a glance</h2><div class="muted">All assignments without opening an editor</div></div></div></div>${this._peopleSummary()}</section>
    </div>`;
  }

  _metric(title, value, detail, icon, tone) {
    return `<section class="card span-3 metric-card ${escapeHtml(tone)}"><span class="metric-icon">${this._icon(icon)}</span><div><div class="metric-label">${escapeHtml(title)}</div><div class="metric">${escapeHtml(value)}</div><div class="metric-detail">${escapeHtml(detail)}</div></div></section>`;
  }

  _icon(name) {
    const safeName = escapeHtml(name);
    const iconVersion = escapeHtml(this._draft?.version || "current");
    return `<img class="mdi-icon" src="${ICON_BASE}/${safeName}.svg?v=${iconVersion}" alt="" aria-hidden="true">`;
  }

  _peopleSummary() {
    if (!this._draft.users.length) return `<div class="empty">No people configured yet.</div>`;
    return `<table><thead><tr><th>Person</th><th>Assigned devices</th><th>Away grace</th></tr></thead><tbody>${this._draft.users.map((user) => `<tr><td><strong>${escapeHtml(user.name)}</strong></td><td>${escapeHtml(user.macs.map((mac) => this._trackerName(mac)).join(", ") || "None")}</td><td>${escapeHtml(user.away_grace_period)} seconds</td></tr>`).join("")}</tbody></table>`;
  }

  _devices() {
    const trackers = this._draft.trackers;
    return `<div class="grid">
      <section class="card"><div class="section-head"><div><h2>People</h2><div class="muted">Create a person, then assign one or more tracked devices. Any assigned device at home makes the person home.</div></div><div class="row add-row"><input id="new-person-name" placeholder="Person name" aria-label="New person name"><button class="btn primary" data-action="add-person">Add person</button></div></div><div class="stack">${this._draft.users.map((user, index) => this._personEditor(user, index)).join("") || `<div class="empty">No people configured. Enter a name above to create the first person.</div>`}</div></section>
      <section class="card"><div class="section-head"><div><h2>Tracked wireless devices</h2><div class="muted">Actual association state, friendly name, and optional SSID scope in one table</div></div><button class="btn" data-action="discover">Discover now</button></div>
        ${trackers.length ? `<table><thead><tr><th>State</th><th>Device</th><th>Current connection</th><th>Allowed SSIDs</th><th></th></tr></thead><tbody>${trackers.map((item, index) => `<tr><td><span class="status ${escapeHtml(item.state)}">${escapeHtml(item.state)}</span></td><td><input data-model="trackers.${index}.name" value="${escapeHtml(item.name)}"><div class="mono muted">${escapeHtml(item.mac)}</div></td><td><strong>${escapeHtml(item.client?.ssid || "—")}</strong><div class="muted">${escapeHtml(item.client?.ap_name || "No current FortiAP")}</div></td><td><input data-model="trackers.${index}.allowed_ssids" data-kind="csv" value="${escapeHtml((item.allowed_ssids || []).join(", "))}" placeholder="Any managed SSID"></td><td class="right"><button class="icon-btn" data-action="remove-tracker" data-mac="${escapeHtml(item.mac)}" title="Remove tracker">Remove</button></td></tr>`).join("")}</tbody></table>` : `<div class="empty">No selected trackers. Discover clients below.</div>`}
      </section>
      <section class="card"><div class="section-head"><div><h2>Discovered clients</h2><div class="muted">${escapeHtml(this._discovered.length)} available clients. Only devices you add become entities.</div></div></div><div class="table-scroll">${this._discoveredTable()}</div></section>
    </div>`;
  }

  _discoveredTable() {
    if (!this._discovered.length) return `<div class="empty">Select Discover now to query the FortiGate client catalog.</div>`;
    return `<table><thead><tr><th>Device</th><th>Network</th><th>Access point</th><th></th></tr></thead><tbody>${this._discovered.map((item) => `<tr><td><strong>${escapeHtml(item.hostname || "Unnamed client")}</strong><div class="mono muted">${escapeHtml(item.mac)}</div></td><td>${escapeHtml(item.ssid || item.ip || "—")}</td><td>${escapeHtml(item.ap_name || "—")}</td><td class="right"><button class="btn" data-action="add-tracker" data-mac="${escapeHtml(item.mac)}">Track</button></td></tr>`).join("")}</tbody></table>`;
  }

  _personEditor(user, index) {
    return `<div class="person"><div class="section-head"><div class="grow"><input data-model="users.${index}.name" value="${escapeHtml(user.name)}" aria-label="Person name"></div><button class="icon-btn" data-action="remove-person" data-id="${escapeHtml(user.id)}">Remove</button></div><div class="checks">${this._draft.trackers.map((tracker) => `<label><input type="checkbox" data-action="toggle-person-device" data-id="${escapeHtml(user.id)}" data-mac="${escapeHtml(tracker.mac)}" ${user.macs.includes(tracker.mac) ? "checked" : ""}>${escapeHtml(tracker.name)}</label>`).join("") || `<span class="muted">Add tracked devices first.</span>`}</div><div style="max-width:260px;margin-top:14px"><label>Away grace period (seconds)<input type="number" min="30" max="7200" data-model="users.${index}.away_grace_period" data-kind="number" value="${escapeHtml(user.away_grace_period)}"></label></div></div>`;
  }

  _policies() {
    return `<div class="grid"><section class="card"><div class="section-head"><div><h2>Firewall policies</h2><div class="muted">Optional. IDs are verified against FortiGate before saving.</div></div><div class="row"><input id="new-policy-id" inputmode="numeric" placeholder="Policy ID" style="width:140px"><button class="btn" data-action="add-policy">Add policy</button></div></div>${this._draft.policies.length ? `<table><thead><tr><th>Status</th><th>ID</th><th>Verified policy name</th><th></th></tr></thead><tbody>${this._draft.policies.map((policy) => `<tr><td><span class="status ${escapeHtml(policy.state)}">${escapeHtml(policy.state)}</span></td><td class="mono">${escapeHtml(policy.id)}</td><td>${escapeHtml(policy.name)}</td><td class="right"><button class="icon-btn" data-action="remove-policy" data-id="${escapeHtml(policy.id)}">Remove</button></td></tr>`).join("")}</tbody></table>` : `<div class="empty">Tracker-only mode. No firewall policy control is configured.</div>`}</section>
      <section class="card"><div class="section-head"><div><h2>Presence policy rules</h2><div class="muted">Rules are evaluated centrally; unknown presence never performs an away action</div></div><button class="btn" data-action="add-rule" ${!this._draft.users.length || !this._draft.policies.length ? "disabled" : ""}>Add rule</button></div><div class="stack">${this._draft.rules.map((rule, index) => this._ruleEditor(rule, index)).join("") || `<div class="empty">No policy rules configured.</div>`}</div></section></div>`;
  }

  _ruleEditor(rule, index) {
    return `<div class="rule"><div class="section-head"><div class="grow"><input data-model="rules.${index}.name" value="${escapeHtml(rule.name)}" aria-label="Rule name"></div><button class="icon-btn" data-action="remove-rule" data-id="${escapeHtml(rule.id)}">Remove</button></div><div class="form-grid"><label>User matching<select data-model="rules.${index}.match"><option value="any" ${rule.match === "any" ? "selected" : ""}>Any selected person</option><option value="all" ${rule.match === "all" ? "selected" : ""}>All selected people</option></select></label><label>Required presence<select data-model="rules.${index}.presence"><option value="home" ${rule.presence === "home" ? "selected" : ""}>Home</option><option value="away" ${rule.presence === "away" ? "selected" : ""}>Away</option></select></label><label>Policy action<select data-model="rules.${index}.action"><option value="enable" ${rule.action === "enable" ? "selected" : ""}>Enable</option><option value="disable" ${rule.action === "disable" ? "selected" : ""}>Disable</option></select></label><label>Priority<input type="number" min="0" max="100" data-kind="number" data-model="rules.${index}.priority" value="${escapeHtml(rule.priority)}"></label></div><div class="form-grid" style="margin-top:14px"><div><div class="muted">People</div><div class="checks">${this._draft.users.map((user) => `<label><input type="checkbox" data-action="toggle-rule-list" data-id="${escapeHtml(rule.id)}" data-field="users" data-value="${escapeHtml(user.id)}" ${rule.users.includes(user.id) ? "checked" : ""}>${escapeHtml(user.name)}</label>`).join("")}</div></div><div><div class="muted">Policies</div><div class="checks">${this._draft.policies.map((policy) => `<label><input type="checkbox" data-action="toggle-rule-list" data-id="${escapeHtml(rule.id)}" data-field="policies" data-value="${escapeHtml(policy.id)}" ${rule.policies.includes(policy.id) ? "checked" : ""}>${escapeHtml(policy.id)}</label>`).join("")}</div></div><label style="grid-column:span 2">Schedule entity (optional)<input data-model="rules.${index}.schedule" value="${escapeHtml(rule.schedule || "")}" placeholder="schedule.school_hours"></label></div></div>`;
  }

  _settings() {
    const s = this._draft.settings;
    const number = (key, title, min, max) => `<label>${escapeHtml(title)}<input type="number" min="${min}" max="${max}" data-kind="number" data-model="settings.${key}.${key}" value="${escapeHtml(s[key])}"></label>`;
    const toggle = (key, title, detail) => `<label class="toggle"><input type="checkbox" data-model="settings.${key}.${key}" ${s[key] ? "checked" : ""}><span><strong>${escapeHtml(title)}</strong><br><span class="muted">${escapeHtml(detail)}</span></span></label>`;
    return `<div class="grid"><section class="card span-6"><h2>Presence tracking</h2><div class="stack" style="margin-top:16px">${toggle("wifi_tracking_enabled", "Enable Wi-Fi presence tracking", "Selected trackers are loaded as Home Assistant entities")}${toggle("wifi_client_count_sensor", "Wi-Fi client count sensor", "Creates an optional associated-client count sensor")}<div class="form-grid">${number("wifi_poll_interval", "Wi-Fi polling interval (seconds)", 15, 120)}${number("wifi_away_grace_period", "Away grace period (seconds)", 30, 3600)}${number("recent_client_retention_days", "Discovery retention (days)", 1, 365)}</div></div></section><section class="card span-6"><h2>Policy control</h2><div class="stack" style="margin-top:16px">${toggle("policy_automation_enabled", "Automatic policy enforcement", "Applies configured rules after valid coordinator updates")}${toggle("policy_automation_dry_run", "Dry run", "Calculates decisions without writing to FortiGate")}<div class="form-grid">${number("poll_interval", "Policy polling interval (seconds)", 30, 3600)}${number("default_override_minutes", "Override duration (minutes)", 0, 1440)}</div></div></section><section class="card"><h2>Connection settings</h2><p class="muted">Host, VDOM, token, port, and TLS trust remain in Home Assistant's Reconfigure flow because they are connection credentials rather than operational settings.</p><div class="form-grid"><div><div class="muted">Host</div><strong>${escapeHtml(this._draft.connection.host)}</strong></div><div><div class="muted">VDOM</div><strong>${escapeHtml(this._draft.connection.vdom)}</strong></div><div><div class="muted">Port</div><strong>${escapeHtml(this._draft.connection.port)}</strong></div><div><div class="muted">TLS verification</div><strong>${this._draft.connection.verify_ssl ? "Enabled" : "Disabled"}</strong></div></div></section></div>`;
  }

  _trackerName(mac) {
    return this._draft.trackers.find((item) => item.mac === mac)?.name || mac;
  }

  _render() {
    if (!this.shadowRoot) return;
    if (this._loading) {
      this.shadowRoot.innerHTML = `${this._styles()}<div class="shell"><div class="empty">Loading FortiAP Presence Tracker…</div></div>`;
      return;
    }
    if (!this._draft) {
      this.shadowRoot.innerHTML = `${this._styles()}<div class="shell"><h1>FortiAP Presence Tracker</h1>${this._error ? `<div class="notice error">${escapeHtml(this._error)}</div>` : ""}<div class="card empty">No configured FortiGate entry was found. Add the integration from Settings → Devices & services.</div></div>`;
      return;
    }
    const views = {
      overview: this._overview(),
      devices: this._devices(),
      policies: this._policies(),
      settings: this._settings(),
    };
    const tabs = [["overview","Overview","view-dashboard-outline"],["devices","People & devices","account-multiple-outline"],["policies","Policies & rules","shield-account-outline"],["settings","Settings","cog-outline"]];
    this.shadowRoot.innerHTML = `${this._styles()}<div class="shell"><header class="top"><div class="brand"><span class="brand-mark">${this._icon("access-point-network")}</span><div><div class="eyebrow">FortiAP Presence Tracker ${escapeHtml(this._draft.version)}</div><h1>Wireless presence and policy control</h1><div class="muted">${escapeHtml(this._draft.title)} · ${escapeHtml(this._draft.connection.vdom)} VDOM</div></div></div><div class="row">${this._entries.length > 1 ? `<select data-action="entry-select">${this._entries.map((entry) => `<option value="${escapeHtml(entry.entry_id)}" ${entry.entry_id === this._draft.entry_id ? "selected" : ""}>${escapeHtml(entry.title)}</option>`).join("")}</select>` : ""}<a class="btn" href="/home" aria-label="Back to Home Assistant overview">${this._icon("home-assistant")}Home Assistant</a><button class="btn" data-action="reload">${this._icon("refresh")}Refresh</button></div></header>${this._error ? `<div class="notice error">${escapeHtml(this._error)}</div>` : ""}${this._notice ? `<div class="notice">${escapeHtml(this._notice)}</div>` : ""}<nav class="tabs" aria-label="FortiAP Presence sections">${tabs.map(([key,label,icon]) => `<button class="tab ${this._tab === key ? "active" : ""}" data-action="tab" data-tab="${key}" ${this._tab === key ? 'aria-current="page"' : ""}>${this._icon(icon)}${label}</button>`).join("")}</nav>${views[this._tab]}</div><div class="savebar"><div class="save-inner"><div class="save-note">${this._icon("content-save-check-outline")}<div><strong>Review changes before saving</strong><div class="muted">Policy IDs are verified and the integration reloads atomically.</div></div></div><button class="btn primary" data-action="save" ${this._saving ? "disabled" : ""}>${this._icon("content-save-outline")}${this._saving ? "Saving…" : "Save changes"}</button></div></div>`;
    this.shadowRoot.querySelectorAll("[data-action]").forEach((element) => {
      element.onclick = (event) => this._handleClick(event);
    });
    const entrySelect = this.shadowRoot.querySelector('[data-action="entry-select"]');
    if (entrySelect) entrySelect.onchange = () => void this._load(entrySelect.value);
  }
}

if (!customElements.get("fortiap-presence-panel")) {
  customElements.define("fortiap-presence-panel", FortiApPresencePanel);
}
