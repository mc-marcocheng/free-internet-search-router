"use strict";

const $ = (selector) => document.querySelector(selector);
let bearer = "";
let snapshot;
let draft;
let dirty = false;
let busy = false;
let currentView = "searxng";
let credentialAction;
const names = {
  searxng: "SearXNG",
  exa: "Exa",
  serpapi: "SerpAPI",
  tavily: "Tavily",
};
const labels = {
  http_url: "HTTP endpoint",
  url: "MCP endpoint",
  enabled: "Enabled",
  estimated_cost_usd: "Estimated cost per attempt (USD)",
  hourly_attempt_limit: "Hourly attempt limit",
  key_strategy: "Key selection",
  quota_scope: "Quota scope",
  keyless: "Keyless access",
  depth: "Search depth",
  engine: "Search engine",
  total_timeout: "Total timeout (seconds)",
  attempt_timeout: "Attempt timeout (seconds)",
  max_concurrent_searches: "Concurrent searches",
  max_attempts_per_request: "Attempts per request",
  hourly_cost_limit_usd: "Hourly cost limit (USD)",
  circuit_threshold: "Circuit failure threshold",
  cooldown_seconds: "Circuit cooldown (seconds)",
  key_max_attempts: "Key attempts per request",
  policy_version: "Policy version",
  profiles: "Profiles",
  policy: "Acceptance policy",
};
const hints = {
  key_strategy: [
    [
      "Round robin",
      "Rotates available keys and retries eligible failures with another key within the request budget.",
    ],
    [
      "Failover",
      "Keeps using a working key, switching after eligible failures.",
    ],
    ["Random", "Picks one available key per request, without key retries."],
    [
      "Least inflight",
      "Picks the least busy available key, without key retries.",
    ],
  ],
  quota_scope: [
    [
      "Independent",
      "Each key has its own quota. Exhaustion can fall back to another key.",
    ],
    [
      "Shared",
      "Keys share an account quota. Exhaustion blocks the pool; key rotation never bypasses provider-wide rate limits.",
    ],
  ],
  key_max_attempts: [
    [
      "Key attempts",
      "Includes the first key call. Set 1 to disable key retries; all attempts share the request deadline and budgets.",
    ],
  ],
  hourly_attempt_limit: [
    [
      "Attempt limit",
      "Counts upstream search calls, including key retries. An empty provider limit uses only the global ceiling.",
    ],
  ],
  estimated_cost_usd: [
    [
      "Cost estimate",
      "A conservative per-call estimate, not measured billing. Unknown estimates prevent calls when a dollar ceiling is enabled.",
    ],
  ],
  hourly_cost_limit_usd: [
    [
      "Cost ceiling",
      "Limits estimated hourly spend. Requires a conservative cost estimate for every provider used.",
    ],
  ],
  reader_backends: [
    [
      "Backend order",
      "The first backend runs first. Eligible failures can fall back once to the second backend within the same deadline.",
    ],
  ],
};

function icon(name) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  element.setAttribute("aria-hidden", "true");
  element.setAttribute("viewBox", "0 0 24 24");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `/admin/icons.svg#${name}`);
  element.append(use);
  return element;
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function button(label, symbol, action, iconOnly = false) {
  const element = node("button", iconOnly ? "icon-button" : "secondary");
  element.type = "button";
  element.title = label;
  element.setAttribute("aria-label", label);
  element.append(icon(symbol));
  if (!iconOnly) element.append(document.createTextNode(label));
  element.addEventListener("click", action);
  return element;
}

const humanize = (name) =>
  labels[name] ||
  name
    .replace(/^reader_/, "")
    .replaceAll("_", " ")
    .replace(/^./, (letter) => letter.toUpperCase());

function notice(message = "", kind = "") {
  $("#notice").textContent = message;
  $("#notice").className = `notice ${kind}`;
  $("#notice").hidden = !message;
}

function markDirty() {
  dirty = true;
  $("#save").disabled = busy;
  $("#save-state").textContent = "Unsaved changes";
}

function setBusy(value) {
  busy = value;
  $("#save").disabled = value || !dirty;
  $("#credential-submit").disabled = value;
  $("#refresh").disabled = value;
  for (const control of document.querySelectorAll(".key-action"))
    control.disabled = value || control.dataset.unsupported === "true";
}

async function request(path, method = "GET", body) {
  const response = await fetch(`/admin/api/${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${bearer}`,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
    credentials: "omit",
  });
  const result = await response.json();
  if (!response.ok) {
    const messages = {
      unauthorized: "Authentication required.",
      administrator_required: "An administrator token is required.",
      configuration_conflict:
        "The configuration changed elsewhere. Refresh before saving.",
      invalid_configuration:
        "Some settings are invalid. Check the values and try again.",
      valid_administrator_required:
        "At least one valid administrator must remain.",
      valid_router_token_required:
        "Router tokens must contain at least 24 characters. At least one token must remain.",
      duplicate_credential: "This key is already configured.",
      credential_required: "Enter a credential.",
      key_name_required: "Enter a key name.",
      invalid_token_operation:
        "That token name cannot be used for this operation.",
    };
    if (response.status === 401) signOut();
    throw new Error(
      messages[result.error] || "The operation could not be completed.",
    );
  }
  return result;
}

function accept(document) {
  snapshot = document;
  draft = structuredClone(document.configuration);
  dirty = false;
  $("#save-state").textContent = "All changes saved";
  $("#save").disabled = true;
  render();
  if (document.activation_error)
    notice(
      "Settings are saved, but activation failed. The previous runtime remains active.",
      "error",
    );
  else if (document.restart_required.length)
    notice(
      `Restart required: ${document.restart_required.map(humanize).join(", ")}.`,
      "warning",
    );
  else if (document.active_revision !== document.revision)
    notice("Configuration activation pending.", "warning");
  else notice();
}

async function refresh() {
  if (dirty && !confirm("Discard unsaved changes?")) return;
  setBusy(true);
  try {
    accept(await request("configuration"));
  } catch (error) {
    notice(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function save() {
  if (busy || !$("#configuration-form").reportValidity()) return false;
  setBusy(true);
  try {
    const result = await request("configuration", "PUT", {
      revision: snapshot.revision,
      configuration: draft,
    });
    accept(result);
    if (!result.activation_error && !result.restart_required.length)
      notice("Saved.");
    return true;
  } catch (error) {
    notice(error.message, "error");
    return false;
  } finally {
    setBusy(false);
  }
}

function navigation() {
  const navigation = $("#navigation");
  navigation.replaceChildren(node("div", "nav-caption", "PROVIDERS"));
  for (const [name, provider] of Object.entries(draft.providers)) {
    const item = node(
      "button",
      `nav-item ${currentView === name ? "active" : ""}`,
    );
    item.type = "button";
    item.append(
      node("span", `dot ${provider.enabled ? "enabled" : ""}`),
      document.createTextNode(names[name] || name),
    );
    item.addEventListener("click", () => {
      if (currentView !== name) notice();
      currentView = name;
      render();
    });
    if (currentView === name) item.setAttribute("aria-current", "page");
    navigation.append(item);
  }
  navigation.append(node("div", "nav-caption", "WORKSPACE"));
  for (const [view, label, symbol] of [
    ["routing", "Routing", "route"],
    ["limits", "Limits & budgets", "gauge"],
    ["reader", "Reader", "file-text"],
    ["access", "Access", "lock-keyhole"],
    ["advanced", "Advanced", "sliders-horizontal"],
  ]) {
    const item = button(label, symbol, () => {
      if (currentView !== view) notice();
      currentView = view;
      render();
    });
    item.className = `nav-item ${currentView === view ? "active" : ""}`;
    if (currentView === view) item.setAttribute("aria-current", "page");
    navigation.append(item);
  }
}

function section(title, extra) {
  const section = node("section", "section");
  const heading = node("div", "section-heading");
  heading.append(node("h2", "", title));
  if (extra) heading.append(extra);
  section.append(heading);
  $("#view").append(section);
  return section;
}

function field(target, key, schema = {}) {
  const value = target[key];
  const actual = schema.anyOf?.find((entry) => entry.type !== "null") || schema;
  const wrapper = node("div", "field");
  const id = `field-${key}`;
  const label = node("label", "", humanize(key));
  label.htmlFor = id;
  let input;
  if (key === "reader_backends") {
    input = node("select");
    for (const [backends, title] of [
      [["httpx", "cloakbrowser"], "HTTPX, then CloakBrowser"],
      [["cloakbrowser", "httpx"], "CloakBrowser, then HTTPX"],
      [["httpx"], "HTTPX only"],
      [["cloakbrowser"], "CloakBrowser only"],
    ]) {
      const option = node("option", "", title);
      option.value = JSON.stringify(backends);
      input.append(option);
    }
    input.value = JSON.stringify(value);
  } else if (actual.enum) {
    input = node("select");
    for (const choice of actual.enum) {
      const option = node("option", "", humanize(choice));
      option.value = choice;
      input.append(option);
    }
    input.value = value;
  } else if (typeof value === "boolean" || actual.type === "boolean") {
    input = node("input");
    input.type = "checkbox";
    input.checked = value;
    input.setAttribute("role", "switch");
  } else if (value !== null && typeof value === "object") {
    input = node("textarea");
    input.value = JSON.stringify(value, null, 2);
    wrapper.classList.add("wide");
  } else {
    input = node("input");
    const numeric =
      typeof value === "number" ||
      ["number", "integer"].includes(actual.type) ||
      (value === null && key.endsWith("usd"));
    input.type = numeric ? "number" : "text";
    if (numeric) {
      input.step = actual.type === "integer" ? "1" : "any";
      if (actual.minimum !== undefined) input.min = actual.minimum;
      if (actual.maximum !== undefined) input.max = actual.maximum;
    }
    input.value = value ?? "";
    if (value === null) input.placeholder = "Not set";
    if (key === "url" || key.endsWith("_url")) wrapper.classList.add("wide");
  }
  input.id = id;
  input.addEventListener("input", () => {
    input.setCustomValidity("");
    try {
      target[key] =
        input.type === "checkbox"
          ? input.checked
          : input.tagName === "TEXTAREA" || key === "reader_backends"
            ? JSON.parse(input.value)
            : input.type === "number"
              ? input.value === ""
                ? null
                : Number(input.value)
              : input.value === "" && value === null
                ? null
                : input.value;
      markDirty();
      if (key === "enabled") navigation();
    } catch {
      input.setCustomValidity("Enter valid JSON.");
      markDirty();
    }
  });
  const heading = node("div", "field-label");
  heading.append(label);
  if (hints[key]) {
    const help = node("details", "field-help");
    const summary = node("summary");
    summary.setAttribute("aria-label", `${humanize(key)} help`);
    summary.title = hints[key]
      .map(([title, description]) => `${title}: ${description}`)
      .join("\n");
    summary.append(icon("circle-help"));
    const popover = node("div", "help-popover");
    popover.id = `help-${key}`;
    input.setAttribute("aria-describedby", popover.id);
    for (const [title, description] of hints[key]) {
      const paragraph = node("p");
      paragraph.append(
        node("strong", "", `${title}: `),
        document.createTextNode(description),
      );
      popover.append(paragraph);
    }
    help.append(summary, popover);
    heading.append(help);
  }
  wrapper.append(heading, input);
  return wrapper;
}

async function openCredential(action) {
  if (busy) return;
  if (
    dirty &&
    (!confirm("Save pending settings before changing credentials?") ||
      !(await save()))
  )
    return;
  credentialAction = action;
  const entity = action.field === "service_key" ? `${names[action.provider] || action.provider} service key` : action.provider
    ? action.name || `${names[action.provider] || action.provider} key`
    : "token";
  const title = `${humanize(action.operation)} ${entity}`;
  $("#credential-title").textContent = title;
  $("#credential-submit").textContent = humanize(action.operation);
  $("#credential-submit").className =
    action.operation === "remove" ? "primary danger" : "primary";
  $("#credential-error").textContent = "";
  $("#credential-value").value = "";
  const needsSecret = ["add", "replace"].includes(action.operation);
  $("#credential-value").required = needsSecret;
  $("#credential-value").minLength = action.provider ? 1 : 24;
  $("#credential-value-field").hidden = !needsSecret;
  $("#key-name-field").hidden =
    !action.provider || Boolean(action.field) || action.operation === "remove";
  $("#key-name").required = !$("#key-name-field").hidden;
  $("#key-name").value =
    action.name ||
    (action.provider ? `Key ${snapshot.credentials[action.provider] + 1}` : "");
  $("#token-name-field").hidden = Boolean(
    action.provider || action.operation !== "add",
  );
  $("#token-name").required = !$("#token-name-field").hidden;
  $("#token-name").value = action.token_name || "";
  $("#credential-dialog").showModal();
}

function updateKeyHealth() {
  for (const element of document.querySelectorAll(".key-health")) {
    const status = snapshot.key_status?.[element.dataset.provider]?.[
      Number(element.dataset.index) - 1
    ];
    element.replaceChildren();
    if (!status) {
      element.append(node("span", "", "Status unavailable"));
      continue;
    }
    const test = status.test;
    const testText = test
      ? `${humanize(test.status)} · ${new Date(test.checked_at).toLocaleString()}${test.status === "working" ? "" : ` · ${humanize(test.reason)}`}`
      : "Not tested";
    const testStatus = node("span", `key-test-status key-test-${test?.status || "unknown"}`);
    testStatus.title = testText;
    testStatus.setAttribute("role", "img");
    testStatus.setAttribute("aria-label", testText);
    testStatus.append(icon(test?.status === "working" ? "circle-check" : test?.status === "failed" ? "circle-x" : test ? "circle-minus" : "circle-help"));
    element.append(testStatus);
    const quota = status.quota;
    const dollars = (value) => value == null ? "Unknown" : new Intl.NumberFormat("en-US", {
      style: "currency", currency: "USD", maximumFractionDigits: 4,
    }).format(value);
    const quotaText = quota
      ? `${quota.unit === "usd" ? `${dollars(quota.remaining)}${quota.budget_usd != null ? ` / ${dollars(quota.budget_usd)} budget` : ""}` : `${quota.remaining ?? "Unknown"} ${quota.unit === "unknown" ? "(unit unknown)" : quota.unit}`} · ${status.quota_stale ? "Stale" : "Fresh"}`
      : status.quota_configuration_error ? "Quota setup required" : status.quota_discovery_supported ? "Quota unknown" : "Quota unsupported";
    const quotaLabel = node("span", "", quotaText);
    const quotaDetails = [quota ? `${quota.scope} quota remaining. Observed ${new Date(quota.observed_at).toLocaleString()}` : quotaText];
    if (quota?.source === "configured_budget")
      quotaDetails.push(`Assumed UTC calendar-month budget, not Exa credit. Used ${dollars(quota.used_usd)}`);
    if (status.quota_configuration_error)
      quotaDetails.push(humanize(status.quota_configuration_error));
    if (quota?.reset_at)
      quotaDetails.push(`${quota.source === "configured_budget" ? "Budget renews" : "Resets"} ${new Date(quota.reset_at).toLocaleString()}`);
    else if (quota?.renewal_date)
      quotaDetails.push(`Renews ${quota.renewal_date} (date only)`);
    if (status.quota_refresh_status && !["not_checked", "completed"].includes(status.quota_refresh_status)) {
      quotaDetails.push(`Quota refresh: ${humanize(status.quota_refresh_status)}`);
      quotaLabel.classList.add("key-quota-warning");
      quotaLabel.append(icon("triangle-alert"));
    }
    quotaLabel.title = quotaDetails.join(". ");
    quotaLabel.setAttribute("aria-label", `${quotaText}. ${quotaLabel.title}`);
    element.append(quotaLabel);
    if (status.cooldown_remaining_seconds > 0) {
      const cooldown = node("span", "key-cooldown", `${Math.ceil(status.cooldown_remaining_seconds)}s`);
      cooldown.prepend(icon("clock"));
      cooldown.title = `Cooldown: ${humanize(status.last_reason || "Unavailable")}`;
      cooldown.setAttribute("aria-label", `${cooldown.title}, ${Math.ceil(status.cooldown_remaining_seconds)} seconds`);
      element.append(cooldown);
    }
  }
}

async function runKeyAction(provider, index, name, action) {
  if (busy) return;
  if (dirty) {
    notice("Save pending settings before running a key check.", "warning");
    return;
  }
  const estimate = snapshot.configuration.providers[provider].estimated_cost_usd;
  if (action === "test" && (estimate === null || estimate > 1)) {
    notice("Set and save a conservative per-attempt cost estimate of at most $1 before testing.", "warning");
    return;
  }
  const message = action === "test"
    ? `Test ${name} with one live search? Estimated cost ceiling: $${estimate}. Actual billing may differ. Search attempt and cost budgets apply.`
    : `Refresh quota for ${name}? This makes one live provider request and uses the hourly quota-probe allowance.`;
  if (!confirm(message)) return;
  setBusy(true);
  notice(action === "test" ? `Testing ${name}...` : `Refreshing quota for ${name}...`);
  try {
    const result = await request("key-actions", "POST", {
      revision: snapshot.revision, provider, index, action,
      confirm_live: true, max_cost_usd: action === "test" ? estimate : null,
    });
    const document = await request("configuration");
    if (!bearer) return;
    if (document.revision === snapshot.revision) {
      snapshot.key_status = document.key_status;
      updateKeyHealth();
    } else if (!dirty) accept(document);
    const success = ["working", "refreshed"].includes(result.status);
    if (currentView === provider)
      notice(`${name}: ${humanize(result.status)}${success ? "" : ` · ${humanize(result.reason)}`}`, success ? "" : "warning");
  } catch (error) {
    if (bearer && currentView === provider) notice(error.message, "error");
  } finally {
    setBusy(false);
  }
}

function providerView(name) {
  const configuration = draft.providers[name];
  const count = snapshot.credentials[name];
  const schema = snapshot.provider_schemas[name];
  const status = node(
    "span",
    `status ${!count && name !== "searxng" && !configuration.keyless ? "neutral" : ""}`,
    count
      ? `${count} key${count === 1 ? "" : "s"} configured`
      : name === "searxng"
        ? "Native HTTP"
        : configuration.keyless
          ? "Keyless"
          : "No keys",
  );
  const details = section("Provider settings", status);
  const fields = node("div", "fields");
  for (const key of Object.keys(configuration)) {
    if (
      (name === "exa" && ["usage_key_ids", "monthly_budget_usd"].includes(key)) ||
      schema.properties[key]?.writeOnly ||
      schema.properties[key]?.anyOf?.some((entry) => entry.writeOnly)
    )
      continue;
    fields.append(field(configuration, key, schema.properties[key]));
  }
  details.append(fields);
  if (name === "exa") {
    const configured = snapshot.provider_credentials?.exa?.service_key;
    const controls = node("div", "row-actions");
    controls.append(button(configured ? "Replace service key" : "Add service key", "key-round",
      () => openCredential({operation: configured ? "replace" : "add", provider: name, field: "service_key"})));
    if (configured)
      controls.append(button("Remove service key", "trash-2",
        () => openCredential({operation: "remove", provider: name, field: "service_key"}), true));
    section("Quota service key", controls);
  }
  if (!schema.properties.keys) return;
  const keys = section(
    "Key pool",
    button("Add key", "plus", () =>
      openCredential({ operation: "add", provider: name }),
    ),
  );
  const list = node("div", "data-list");
  if (!count) list.append(node("div", "empty", "No keys configured"));
  for (const metadata of snapshot.key_metadata[name]) {
    const { index, name: keyName, preview } = metadata;
    const row = node("div", "data-row key-row");
    row.append(
      node("span", "row-index", String(index).padStart(2, "0")),
      node("span", "row-name", keyName),
      node("span", "mask", preview),
    );
    const actions = node("div", "row-actions");
    actions.append(
      button(
        `Rename key ${index}`,
        "pencil",
        () =>
          openCredential({
            operation: "rename",
            provider: name,
            index,
            name: keyName,
          }),
        true,
      ),
      button(
        `Replace key ${index}`,
        "key-round",
        () =>
          openCredential({
            operation: "replace",
            provider: name,
            index,
            name: keyName,
          }),
        true,
      ),
      button(
        `Remove key ${index}`,
        "trash-2",
        () =>
          openCredential({
            operation: "remove",
            provider: name,
            index,
            name: keyName,
          }),
        true,
      ),
    );
    const health = node("div", "key-health");
    health.dataset.provider = name;
    health.dataset.index = index;
    health.setAttribute("role", "status");
    row.append(health, actions);
    for (const [action, label, symbol] of [
      ["test", "Test", "play"],
      ["refresh_quota", "Refresh quota for", "refresh-cw"],
    ]) {
      const control = button(`${label} ${keyName}`, symbol,
        () => runKeyAction(name, index, keyName, action), true);
      control.classList.add("key-action");
      const keyStatus = snapshot.key_status?.[name]?.[index - 1];
      control.dataset.unsupported = String(action === "refresh_quota" && (!keyStatus?.quota_discovery_supported || Boolean(keyStatus.quota_configuration_error)));
      if (action === "refresh_quota" && keyStatus?.quota_configuration_error)
        control.title = `${control.title}: ${humanize(keyStatus.quota_configuration_error)}`;
      control.disabled = control.dataset.unsupported === "true";
      actions.prepend(control);
    }
    list.append(row);
  }
  keys.append(list);
  updateKeyHealth();
}

function routingView() {
  const chain = section("Default provider order");
  const list = node("div", "data-list");
  draft.provider_order.forEach((name, index) => {
    const row = node("div", "data-row");
    row.append(
      node("span", "row-index", String(index + 1).padStart(2, "0")),
      node("span", "row-name", names[name] || name),
    );
    const actions = node("div", "row-actions");
    for (const [symbol, delta] of [
      ["arrow-up", -1],
      ["arrow-down", 1],
    ]) {
      const move = button(
        `Move ${names[name] || name} ${delta < 0 ? "up" : "down"}`,
        symbol,
        () => {
          [draft.provider_order[index], draft.provider_order[index + delta]] = [
            draft.provider_order[index + delta],
            draft.provider_order[index],
          ];
          markDirty();
          render();
        },
        true,
      );
      move.disabled =
        index + delta < 0 || index + delta >= draft.provider_order.length;
      actions.append(move);
    }
    actions.append(
      button(
        `Remove ${name} from default order`,
        "minus",
        () => {
          draft.provider_order.splice(index, 1);
          markDirty();
          render();
        },
        true,
      ),
    );
    row.append(actions);
    list.append(row);
  });
  chain.append(list);
  const available = Object.keys(draft.providers).filter(
    (name) => !draft.provider_order.includes(name),
  );
  if (available.length) {
    const add = node("div", "chain-add");
    const select = node("select");
    select.setAttribute("aria-label", "Provider to add");
    available.forEach((name) => {
      const option = node("option", "", names[name] || name);
      option.value = name;
      select.append(option);
    });
    add.append(
      select,
      button("Add provider", "plus", () => {
        draft.provider_order.push(select.value);
        markDirty();
        render();
      }),
    );
    chain.append(add);
  }
  for (const key of ["profiles", "policy", "policy_version"])
    section(humanize(key)).append(field(draft, key));
}

function accessView() {
  const tokens = section(
    "Router tokens",
    button("Add token", "plus", () => openCredential({ operation: "add" })),
  );
  const list = node("div", "data-list");
  for (const name of snapshot.tokens) {
    const row = node("div", "data-row");
    row.append(
      node("span", "row-name", name),
      node("span", "mask", "************"),
    );
    const role = node("label", "role-label");
    const checkbox = node("input", "role-toggle");
    checkbox.type = "checkbox";
    checkbox.checked = draft.administrators.includes(name);
    checkbox.setAttribute("aria-label", `Administrator access for ${name}`);
    checkbox.addEventListener("change", () => {
      draft.administrators = checkbox.checked
        ? [...draft.administrators, name]
        : draft.administrators.filter((entry) => entry !== name);
      markDirty();
    });
    role.append(checkbox, document.createTextNode("Administrator"));
    row.append(role);
    const actions = node("div", "row-actions");
    actions.append(
      button(
        `Replace token ${name}`,
        "key-round",
        () => openCredential({ operation: "replace", token_name: name }),
        true,
      ),
      button(
        `Remove token ${name}`,
        "trash-2",
        () => openCredential({ operation: "remove", token_name: name }),
        true,
      ),
    );
    row.append(actions);
    list.append(row);
  }
  tokens.append(list);
}

function render() {
  if (
    !draft.providers[currentView] &&
    !["routing", "limits", "reader", "access", "advanced"].includes(currentView)
  )
    currentView = Object.keys(draft.providers)[0];
  navigation();
  $("#view").replaceChildren();
  $("#view-title").textContent =
    names[currentView] ||
    {
      routing: "Routing",
      limits: "Limits & budgets",
      reader: "Reader",
      access: "Access",
      advanced: "Advanced",
    }[currentView] ||
    currentView;
  $("#view-kicker").textContent = draft.providers[currentView]
    ? "PROVIDERS"
    : "WORKSPACE";
  if (draft.providers[currentView]) providerView(currentView);
  else if (currentView === "routing") routingView();
  else if (currentView === "access") accessView();
  else if (currentView === "advanced") {
    const wrapper = field({ configuration: draft }, "configuration");
    const textarea = wrapper.querySelector("textarea");
    textarea.classList.add("full-editor");
    textarea.addEventListener("input", () => {
      try {
        draft = JSON.parse(textarea.value);
      } catch {
        textarea.setCustomValidity("Enter valid JSON.");
      }
    });
    section("Configuration JSON").append(wrapper);
  } else {
    const keys =
      currentView === "reader"
        ? Object.keys(draft).filter((key) => key.startsWith("reader_"))
        : [
            "total_timeout",
            "attempt_timeout",
            "max_concurrent_searches",
            "max_attempts_per_request",
            "hourly_attempt_limit",
            "hourly_cost_limit_usd",
            "circuit_threshold",
            "cooldown_seconds",
            "key_max_attempts",
            "key_quota_cooldown_seconds",
          ];
    const fields = node("div", "fields");
    keys.forEach((key) => fields.append(field(draft, key)));
    section(
      currentView === "reader"
        ? "Acquisition & extraction"
        : "Request & upstream limits",
    ).append(fields);
  }
}

function signOut() {
  bearer = "";
  dirty = false;
  snapshot = undefined;
  draft = undefined;
  $("#workspace").hidden = true;
  $("#header-actions").hidden = true;
  $("#login-screen").hidden = false;
  $("#login-token").value = "";
  $("#credential-value").value = "";
  if ($("#credential-dialog").open) $("#credential-dialog").close();
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  bearer = $("#login-token").value;
  $("#login-token").value = "";
  const submit = event.currentTarget.querySelector("button");
  submit.disabled = true;
  try {
    const document = await request("configuration");
    $("#login-error").textContent = "";
    $("#login-screen").hidden = true;
    $("#workspace").hidden = false;
    $("#header-actions").hidden = false;
    accept(document);
  } catch (error) {
    bearer = "";
    $("#login-error").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
$("#credential-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  setBusy(true);
  const value = $("#credential-value").value;
  $("#credential-value").value = "";
  const update = { ...credentialAction, revision: snapshot.revision };
  if (!credentialAction.provider && credentialAction.operation === "add")
    update.token_name = $("#token-name").value;
  if (credentialAction.provider && !credentialAction.field && credentialAction.operation !== "remove")
    update.name = $("#key-name").value;
  if (["add", "replace"].includes(credentialAction.operation))
    update.value = value;
  try {
    const document = await request("credentials", "POST", update);
    $("#credential-dialog").close();
    accept(document);
  } catch (error) {
    $("#credential-error").textContent = error.message;
  } finally {
    setBusy(false);
  }
});
$("#configuration-form").addEventListener("submit", (event) => {
  event.preventDefault();
  save();
});
$("#save").addEventListener("click", save);
$("#refresh").addEventListener("click", refresh);
$("#sign-out").addEventListener("click", () => {
  if (!dirty || confirm("Discard unsaved changes and sign out?")) signOut();
});
for (const selector of ["#close-dialog", "#cancel-dialog"])
  $(selector).addEventListener("click", () => {
    if (!busy) $("#credential-dialog").close();
  });
$("#credential-dialog").addEventListener("close", () => {
  $("#credential-value").value = "";
});
window.addEventListener("beforeunload", (event) => {
  if (dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});
setInterval(async () => {
  if (!bearer || busy || $("#credential-dialog").open) return;
  try {
    const document = await request("configuration");
    if (document.revision !== snapshot.revision) {
      if (dirty)
        notice(
          "The configuration changed elsewhere. Refresh before saving.",
          "warning",
        );
      else accept(document);
    } else if (bearer) {
      snapshot.key_status = document.key_status;
      updateKeyHealth();
    }
  } catch (error) {
    if (bearer) notice(error.message, "error");
  }
}, 5000);
