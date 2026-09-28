"use strict";

const views = [
  "home",
  "setup",
  "settings",
  "activity",
  "advanced",
  "wifi",
  "checking",
  "whatsapp",
  "code",
  "pairing-progress",
  "pairing-error",
  "ready",
  "recipients",
  "deferred",
  "voice-test",
  "voice-success",
  "recipient-manager",
  "people",
  "nfc",
  "nfc-choose",
  "nfc-mapped",
  "nfc-success",
  "nfc-unavailable",
  "complete",
  "failed",
];
const activePairingStates = new Set(["starting", "code_pending", "bootstrapping", "verifying"]);
let pollTimer = null;
let loadingState = false;
let lastView = "";
let recipientsData = null;
let managerOpen = false;
let nfcData = null;
let nfcPollTimer = null;
let peoplePairing = null;
let peopleShowHidden = false;
let peoplePollTimer = null;
let runtimePairing = null;
let runtimePairingGeneration = 0;
let currentState = null;
let currentSettings = null;

function acceptanceControl(tag, ...caseIds) {
  const control = document.createElement(tag);
  control.dataset.acceptanceCase = caseIds.join(" ");
  return control;
}

function showView(name) {
  for (const view of views) {
    const element = document.getElementById(`${view}-view`);
    if (element) element.hidden = view !== name;
  }
  if (lastView !== name) {
    lastView = name;
    document.querySelector(`#${name}-view h1`)?.focus({ preventScroll: true });
    window.scrollTo?.({ top: 0, behavior: "instant" });
  }
}

function showError(message) {
  const element = document.getElementById("page-error");
  if (!element) return;
  element.textContent = message;
  element.hidden = !message;
}

function rememberState(state) {
  currentState = currentState ? { ...currentState, ...state } : state;
  return currentState;
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    ...options,
  });
  const type = response.headers.get("content-type") || "";
  const data = type.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(data?.error || "Button Box did not respond.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function formRequest(url, fields = {}) {
  const body = new URLSearchParams(fields);
  return request(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    },
    body,
  });
}

function selectNetwork(network) {
  document.getElementById("ssid").value = network.ssid;
  const open = ["open", "unencrypted", "none"].includes(network.security.toLowerCase());
  const selector = `input[name="security"][value="${open ? "open" : "protected"}"]`;
  const radio = document.querySelector(selector);
  radio.checked = true;
  radio.dispatchEvent(new Event("change"));
  document.getElementById(open ? "ssid" : "wifi-password").focus();
}

async function scanNetworks() {
  const status = document.getElementById("scan-status");
  const list = document.getElementById("networks");
  status.textContent = "Scanning for networks...";
  list.replaceChildren();
  try {
    const data = await request("/api/networks");
    if (!data.networks.length) {
      status.textContent = "No networks found. Enter the Wi-Fi name below.";
      return;
    }
    status.textContent = `${data.networks.length} network${data.networks.length === 1 ? "" : "s"} found`;
    for (const network of data.networks) {
      const button = acceptanceControl("button", "BB-WIFI-01");
      button.type = "button";
      button.className = "network";
      button.setAttribute("role", "listitem");
      const name = document.createElement("span");
      name.textContent = network.ssid;
      const detail = document.createElement("span");
      detail.className = "network-detail";
      const strength = network.signal === null ? "" : ` · ${network.signal}%`;
      detail.textContent = `${network.security === "unencrypted" ? "Open" : "Protected"}${strength}`;
      button.append(name, detail);
      button.addEventListener("click", () => selectNetwork(network));
      list.append(button);
    }
  } catch (error) {
    status.textContent = "Scanning is unavailable. Enter the Wi-Fi name below.";
  }
}

function pairingErrorCopy(error, status) {
  if (status === "expired" || error === "PAIRING_INTERRUPTED") {
    return "The pairing code expired or the link was interrupted. No account was saved.";
  }
  const messages = {
    AUTHENTICATION_FAILED: "WhatsApp did not confirm the linked account. No account was saved.",
    WHATSAPP_UNREACHABLE: "The account linked, but WhatsApp connectivity could not be verified. No account was saved.",
    CONVERSATIONS_UNAVAILABLE: "The account linked, but setup could not finish safely. No account was saved.",
    STORE_CONFLICT: "A previous WhatsApp store needs attention before another account can be linked.",
    CLEANUP_FAILED: "Private pairing cleanup needs attention before you try again.",
    PAIRING_UNAVAILABLE: "WhatsApp pairing is temporarily unavailable. Try again in a moment.",
    UNLINK_FAILED: "WhatsApp logout failed. The current linked account was preserved.",
  };
  return messages[error] || "Pairing did not finish. No account was saved.";
}

function schedulePoll(status, recipientStatus = null) {
  window.clearTimeout(pollTimer);
  pollTimer = activePairingStates.has(status) || recipientStatus === "testing"
    ? window.setTimeout(currentState?.mode === "RUNTIME" ? loadRuntimeWhatsApp : loadState, 1500)
    : null;
}

async function loadRuntimeWhatsApp() {
  try {
    const whatsapp = await request("/api/whatsapp");
    const state = rememberState({
      mode: "RUNTIME",
      phase: whatsapp.status === "ready" ? "WHATSAPP_READY" : "WHATSAPP_PENDING",
      whatsapp,
    });
    if (whatsapp.status === "ready") {
      state.recipient_setup = await request("/api/recipients");
      state.nfc_setup = { status: "idle", mapped_count: 0 };
    }
    applyWhatsAppState(state, { manage: true });
  } catch (error) {
    showError(error.message);
  }
}

function setProof(id, complete) {
  const row = document.getElementById(id);
  row.classList.toggle("done", complete);
  row.querySelector("span").textContent = complete ? "✓" : "○";
}

function applyRecipientState(recipient, nfcSummary = null) {
  const summary = recipient || {
    status: "choose",
    default: null,
    proof: { received: false, played: false, replied: false },
  };
  schedulePoll("ready", summary.status);
  if (summary.status === "deferred") {
    showView("deferred");
    return;
  }
  if (summary.status === "error") {
    showView("ready");
    showError("Recipient setup is temporarily unavailable. Try again in a moment.");
    pollTimer = window.setTimeout(loadState, 3000);
    return;
  }
  if (summary.status === "testing") {
    managerOpen = false;
    document.getElementById("voice-recipient").textContent = summary.default?.label || "your recipient";
    setProof("proof-received", summary.proof.received);
    setProof("proof-played", summary.proof.played);
    setProof("proof-replied", summary.proof.replied);
    showView("voice-test");
    return;
  }
  if (summary.status === "complete") {
    if (nfcSummary && new Set([
      "waiting", "choose", "already_paired", "success", "unavailable",
    ]).has(nfcSummary.status)) {
      loadNfc();
      return;
    }
    document.getElementById("success-recipient").textContent = summary.default?.label || "your recipient";
    if (managerOpen && recipientsData) {
      renderRecipientManager(recipientsData);
      showView("recipient-manager");
    } else {
      showView("voice-success");
    }
    return;
  }
  showView("ready");
}

function applyWhatsAppState(state, { manage = false } = {}) {
  const whatsapp = state.whatsapp || {
    status: "failed",
    pairing_code: null,
    phone_hint: null,
    eligible_count: 0,
    safe_error: "PAIRING_UNAVAILABLE",
  };
  schedulePoll(whatsapp.status);
  if (state.phase === "WHATSAPP_READY" || whatsapp.status === "ready") {
    document.getElementById("linked-account").textContent = whatsapp.phone_hint
      ? `${whatsapp.phone_hint} is linked and connected.`
      : "WhatsApp is linked and connected.";
    const count = whatsapp.eligible_count;
    document.getElementById("eligible-count").textContent = count === 1
      ? "1 recent group or chat is ready for the next setup step."
      : `${count} recent groups or chats are ready for the next setup step.`;
    if (manage) {
      showView("ready");
    } else {
      applyRecipientState(state.recipient_setup, state.nfc_setup);
    }
    return;
  }
  switch (whatsapp.status) {
    case "idle":
      showView("whatsapp");
      break;
    case "code_pending":
      showView("code");
      if (document.getElementById("pairing-code").textContent !== whatsapp.pairing_code) {
        document.getElementById("copy-status").textContent = "";
        document.getElementById("copy-pairing-code").textContent = "Copy code";
      }
      document.getElementById("pairing-code").textContent = whatsapp.pairing_code || "New code pending";
      document.getElementById("code-status").textContent = "Waiting for you to enter this code in WhatsApp…";
      break;
    case "starting":
      showView("pairing-progress");
      document.getElementById("pairing-progress-copy").textContent = "Requesting a private pairing code…";
      break;
    case "bootstrapping":
      showView("pairing-progress");
      document.getElementById("pairing-progress-copy").textContent = "WhatsApp linked. Preparing the account safely…";
      break;
    case "verifying":
      showView("pairing-progress");
      document.getElementById("pairing-progress-copy").textContent = "Verifying the account and WhatsApp connection…";
      break;
    case "expired":
    case "failed":
    default:
      showView("pairing-error");
      document.getElementById("pairing-error-copy").textContent = pairingErrorCopy(
        whatsapp.safe_error,
        whatsapp.status,
      );
      document.getElementById("retry-pairing").textContent = whatsapp.safe_error === "CLEANUP_FAILED"
        ? "Finish account cleanup"
        : "Try again";
  }
}

function recipientRow(recipient, actions = []) {
  const row = document.createElement("li");
  row.className = "recipient-row";
  const copy = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = recipient.label;
  const meta = document.createElement("span");
  const tagCopy = recipient.card_count
    ? ` · ${recipient.card_count} tag${recipient.card_count === 1 ? "" : "s"}`
    : "";
  const identity = recipient.secondary_label && recipient.secondary_label !== recipient.label
    ? ` · ${recipient.secondary_label}`
    : "";
  const metadata = recipient.metadata_status === "unavailable"
    ? " · name unavailable — refresh WhatsApp to retry"
    : "";
  meta.className = "recipient-secondary";
  meta.textContent = recipient.is_default
    ? `${recipient.kind} · default${identity}${metadata}${tagCopy}`
    : `${recipient.kind}${identity}${metadata}${tagCopy}`;
  copy.append(name, meta);
  row.append(copy);
  if (actions.length) {
    const controls = document.createElement("div");
    controls.className = "recipient-actions";
    actions.forEach(({ action, label }) => {
    const caseIds = {
      "pair-card": ["BB-NFC-03"],
      "make-default": ["BB-RECIP-07"],
      remove: ["BB-RECIP-09", "BB-RECIP-10"],
      allow: ["BB-RECIP-05"],
      rename: ["BB-RECIP-11"],
    }[action] || ["BB-RECIP-01"];
    const button = acceptanceControl("button", ...caseIds);
    button.type = "button";
    button.className = action === "remove" ? "danger-button compact" : "compact";
    button.textContent = label;
    if (action === "pair-card") button.disabled = Boolean(runtimePairing?.pending);
    button.addEventListener("click", () => {
      if (action === "pair-card") beginRuntimeNfc(recipient.token, recipient.label, button);
      else if (action === "rename") beginRecipientRename(row, recipient);
      else mutateRecipient(action, recipient.token, button);
    });
      controls.append(button);
    });
    row.append(controls);
    if (runtimePairing?.token === recipient.token) {
      const status = document.createElement("div");
      status.className = "card-pairing-status";
      status.tabIndex = -1;
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      const message = document.createElement("p");
      message.className = "card-pairing-message";
      message.textContent = runtimePairing.message;
      status.append(message);
      if (runtimePairing.pending && runtimePairing.attempt) {
        const cancel = acceptanceControl("button", "BB-NFC-03");
        cancel.type = "button";
        cancel.className = "secondary compact";
        cancel.textContent = "Cancel pairing";
        cancel.addEventListener("click", () => runtimeNfcAction("/nfc/cancel-runtime"));
        status.append(cancel);
      }
      row.append(status);
    }
  }
  return row;
}

function beginRecipientRename(row, recipient) {
  if (row.querySelector(".recipient-rename")) return;
  const form = document.createElement("form");
  form.className = "recipient-rename";
  const label = document.createElement("label");
  label.textContent = "Name";
  const input = document.createElement("input");
  input.name = "name";
  input.type = "text";
  input.maxLength = 80;
  input.value = recipient.label === recipient.secondary_label ? "" : recipient.label;
  input.placeholder = "Leave empty to use the phone number";
  label.append(input);
  const controls = document.createElement("div");
  controls.className = "button-row";
  const save = acceptanceControl("button", "BB-RECIP-11");
  save.type = "submit";
  save.className = "compact";
  save.textContent = "Save";
  const cancel = acceptanceControl("button", "BB-RECIP-11");
  cancel.type = "button";
  cancel.className = "secondary compact";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => form.remove());
  controls.append(save, cancel);
  form.append(label, controls);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    save.disabled = true;
    try {
      recipientsData = await formRequest("/recipients/rename", {
        token: recipient.token,
        name: input.value,
      });
      renderRecipientManager(recipientsData);
      document.getElementById("manager-status").textContent = "Name saved.";
    } catch (error) {
      document.getElementById("manager-status").textContent = error.message;
      save.disabled = false;
    }
  });
  row.append(form);
  input.focus();
}

function renderRecipientPicker(data) {
  recipientsData = data;
  const list = document.getElementById("recipient-list");
  const choices = data.recipients.filter((recipient) => recipient.available);
  list.replaceChildren(...choices.map((recipient) => recipientRow(
    recipient,
    [{ action: "select", label: "Choose" }],
  )));
  document.getElementById("recipient-empty").hidden = choices.length !== 0;
}

function renderRecipientManager(data) {
  recipientsData = data;
  document.getElementById("unpair-presented-nfc").hidden = currentState?.mode !== "RUNTIME";
  document.getElementById("continue-nfc").textContent = currentState?.mode === "RUNTIME"
    ? "Pair an NFC card"
    : "Continue to NFC setup";
  const configured = data.recipients.filter((recipient) => recipient.configured);
  const available = data.recipients.filter((recipient) => recipient.available && !recipient.configured);
  document.getElementById("configured-recipient-list").replaceChildren(...configured.map((recipient) => {
    const actions = currentState?.mode === "RUNTIME"
      ? [{ action: "pair-card", label: "Pair card" }]
      : [];
    if (!recipient.is_default) actions.push(
      { action: "default", label: "Make default" },
      { action: "remove", label: "Remove" },
    );
    if (recipient.kind === "person") actions.unshift({
      action: "rename",
      label: recipient.label === recipient.secondary_label ? "Add name" : "Rename",
    });
    return recipientRow(recipient, actions);
  }));
  document.getElementById("available-recipient-list").replaceChildren(
    ...available.map((recipient) => recipientRow(
      recipient,
      [{ action: "add", label: "Allow" }],
    )),
  );
  document.getElementById("manager-empty").hidden = available.length !== 0;
}

function personRow(person, isDefault) {
  const row = document.createElement("li");
  row.className = "recipient-row";
  const copy = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = person.label;
  const meta = document.createElement("span");
  meta.className = "recipient-secondary";
  const cards = person.card_count
    ? `${person.card_count} card${person.card_count === 1 ? "" : "s"}`
    : "no card";
  meta.textContent = isDefault
    ? `${person.kind} \u00b7 default \u00b7 ${cards}`
    : `${person.kind} \u00b7 ${cards}`;
  copy.append(name, meta);
  row.append(copy);
  const controls = document.createElement("div");
  controls.className = "recipient-actions";
  const rename = acceptanceControl("button", "BB-RECIP-11");
  rename.type = "button";
  rename.className = "compact";
  rename.textContent = "Rename";
  rename.addEventListener("click", () => beginPersonRename(row, person));
  controls.append(rename);
  const pair = acceptanceControl("button", "BB-NFC-03");
  pair.type = "button";
  pair.className = "compact";
  pair.textContent = person.card_count ? "Pair another card" : "Pair card";
  pair.disabled = Boolean(peoplePairing?.pending);
  pair.addEventListener("click", () => beginPersonCardPairing(pair, person));
  controls.append(pair);
  if (!isDefault) {
    const makeDefault = acceptanceControl("button", "BB-RECIP-07");
    makeDefault.type = "button";
    makeDefault.className = "secondary compact";
    makeDefault.textContent = "Make default";
    makeDefault.addEventListener("click", () => personAction(makeDefault, {
      action: "default",
      jid: person.jid,
    }));
    controls.append(makeDefault);
  }
  row.append(controls);
  return row;
}

async function personAction(button, payload) {
  const status = document.getElementById("people-status");
  button.disabled = true;
  status.textContent = "Saving\u2026";
  try {
    await request("/api/contacts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await loadPeople();
  } catch (error) {
    status.textContent = error.message;
    button.disabled = false;
  }
}

function beginPersonRename(row, person) {
  if (row.querySelector(".recipient-rename")) return;
  const form = document.createElement("form");
  form.className = "recipient-rename";
  const label = document.createElement("label");
  label.textContent = "Name";
  const input = document.createElement("input");
  input.name = "name";
  input.type = "text";
  input.maxLength = 80;
  input.value = person.label;
  label.append(input);
  const controls = document.createElement("div");
  controls.className = "button-row";
  const save = acceptanceControl("button", "BB-RECIP-11");
  save.type = "submit";
  save.className = "compact";
  save.textContent = "Save";
  const cancel = acceptanceControl("button", "BB-RECIP-11");
  cancel.type = "button";
  cancel.className = "secondary compact";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => form.remove());
  controls.append(save, cancel);
  form.append(label, controls);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    personAction(save, { action: "rename", jid: person.jid, label: input.value });
  });
  row.append(form);
  input.focus();
}

function countryFlag(code) {
  // Regional indicator symbols: 'A' (65) maps to U+1F1E6.
  return String.fromCodePoint(...[...code].map((c) => 0x1f1a5 + c.charCodeAt(0)));
}

function countryList() {
  return window.BUTTON_BOX_COUNTRIES || [];
}

let countryChoice = "US";
let countryHighlight = 0;

function selectedCountry() {
  return countryList().find((entry) => entry[0] === countryChoice)
    || ["US", "United States", "1", [10], { 10: [[3, 3, 4], "($1) $2-$3"] }];
}

function countryDisplay([code, name, dial]) {
  return `${countryFlag(code)}  ${name}  +${dial}`;
}

// Countries this household reaches most often, kept above the alphabet.
const PINNED_COUNTRIES = ["US", "GB", "DE", "PT", "AU"];

function countryMatches(query) {
  const needle = query.trim().toLowerCase();
  const all = countryList();
  const digits = needle.replace(/\D/g, "");
  const hits = (entry) => {
    if (!needle) return true;
    const name = entry[1].toLowerCase();
    return name.includes(needle)
      || entry[0].toLowerCase() === needle
      || (digits && entry[2].startsWith(digits));
  };
  const pinned = PINNED_COUNTRIES
    .map((code) => all.find((entry) => entry[0] === code))
    .filter((entry) => entry && hits(entry));
  const isPinned = new Set(pinned.map((entry) => entry[0]));
  const starts = [];
  const contains = [];
  for (const entry of all) {
    if (isPinned.has(entry[0]) || !hits(entry)) continue;
    const name = entry[1].toLowerCase();
    if (!needle || name.startsWith(needle) || (digits && entry[2].startsWith(digits))) {
      starts.push(entry);
    } else {
      contains.push(entry);
    }
  }
  return { list: pinned.concat(starts, contains).slice(0, 60), pinned: pinned.length };
}

function renderCountryOptions(query) {
  const list = document.getElementById("people-country-list");
  const { list: matches, pinned } = countryMatches(query);
  countryHighlight = Math.min(countryHighlight, Math.max(matches.length - 1, 0));
  list.replaceChildren(...matches.map((entry, index) => {
    const item = document.createElement("div");
    item.setAttribute("role", "option");
    item.dataset.code = entry[0];
    item.setAttribute("aria-selected", String(index === countryHighlight));
    // A rule under the last pinned country, without an extra node that would
    // break the keyboard index into this list.
    if (pinned && index === pinned - 1 && matches.length > pinned) {
      item.classList.add("combo-pinned-end");
    }
    item.textContent = `${countryFlag(entry[0])}  ${entry[1]}  `;
    const dial = document.createElement("span");
    dial.className = "combo-dial";
    dial.textContent = `+${entry[2]}`;
    item.append(dial);
    item.addEventListener("mousedown", (event) => {
      event.preventDefault();
      chooseCountry(entry[0]);
    });
    return item;
  }));
  list.hidden = matches.length === 0;
  document.getElementById("people-country-search")
    .setAttribute("aria-expanded", String(!list.hidden));
  return matches;
}

function closeCountryList() {
  const list = document.getElementById("people-country-list");
  list.hidden = true;
  document.getElementById("people-country-search").setAttribute("aria-expanded", "false");
}

function chooseCountry(code) {
  countryChoice = code;
  document.getElementById("people-country").value = code;
  document.getElementById("people-country-search").value = countryDisplay(selectedCountry());
  closeCountryList();
  renderNumberPreview();
}

function populateCountries() {
  const region = (navigator.language || "").split("-")[1];
  const known = countryList().some((entry) => entry[0] === region);
  chooseCountry(known ? region : "US");
}

function nationalDigits(value) {
  return (value || "").replace(/\D/g, "");
}

function applyTemplate(template, parts) {
  return template.replace(/\$(\d)/g, (_match, index) => parts[Number(index) - 1] || "");
}

const leadingPatterns = new Map();

function leadingMatches(pattern, digits) {
  if (!pattern) return true;
  let expression = leadingPatterns.get(pattern);
  if (!expression) {
    expression = new RegExp(`^(?:${pattern})`);
    leadingPatterns.set(pattern, expression);
  }
  return expression.test(digits);
}

function absorb(lows, highs, length) {
  const sizes = lows.slice();
  let slack = length - sizes.reduce((total, size) => total + size, 0);
  if (slack < 0) return null;
  for (let index = sizes.length - 1; index >= 0 && slack > 0; index -= 1) {
    const take = Math.min(highs[index] - lows[index], slack);
    sizes[index] += take;
    slack -= take;
  }
  return slack === 0 ? sizes : null;
}

function formatFor(entry, digits) {
  const formats = entry[4] || [];
  // libphonenumber picks the first format whose leading digits match and whose
  // groups fit; that is what keeps a mobile off a landline pattern.
  for (const [lows, highs, template, leading] of formats) {
    if (!leadingMatches(leading, digits)) continue;
    const sizes = absorb(lows, highs, digits.length);
    if (sizes) return [sizes, template];
  }
  // Still being typed: group by the shape the finished number will use.
  for (const [lows, , template, leading] of formats) {
    if (leadingMatches(leading, digits)) return [lows, template];
  }
  return null;
}

function formatNational(entry, digits) {
  const chosen = formatFor(entry, digits);
  if (!chosen) return digits.replace(/(\d{3})(?=\d)/g, "$1 ");
  const [groups, template] = chosen;
  const parts = [];
  let index = 0;
  for (const size of groups) {
    if (index >= digits.length) break;
    parts.push(digits.slice(index, index + size));
    index += size;
  }
  let text = applyTemplate(template, parts).replace(/[\s\-().]+$/, "");
  if (index < digits.length) text += ` ${digits.slice(index)}`;
  return text;
}

function placeholderFor(entry) {
  const example = entry[5] || "";
  if (!example) return `Number without +${entry[2]}`;
  return formatNational(entry, example);
}

function nationalProblem(entry, digits) {
  if (!digits) return "Enter a phone number.";
  const lengths = entry[3] || [];
  if (lengths.length && !lengths.includes(digits.length)) {
    const low = Math.min(...lengths);
    const high = Math.max(...lengths);
    const expected = low === high ? `${low} digits` : `${low} to ${high} digits`;
    return `${entry[1]} numbers have ${expected}; that is ${digits.length}.`;
  }
  if (digits.length < 4 || digits.length > 15) {
    return "That does not look like a phone number.";
  }
  return "";
}

function peopleAddKind() {
  return document.querySelector('[name="people_kind"]:checked').value;
}

function renderPeopleAddKind() {
  const group = peopleAddKind() === "group";
  document.getElementById("people-person-fields").hidden = group;
  document.getElementById("people-group-fields").hidden = !group;
  document.getElementById("people-add-name").placeholder = group ? "Family" : "Oma";
  document.getElementById("people-add-help").textContent = group
    ? "Button Box must already be in the group and have seen a message in it."
    : "Any WhatsApp number. Button Box cannot check it exists until the first message is sent.";
  renderNumberPreview();
}

function renderNumberPreview() {
  if (peopleAddKind() === "group") return;
  const country = selectedCountry();
  const field = document.getElementById("people-add-id");
  field.placeholder = placeholderFor(country);
  const digits = nationalDigits(field.value);
  field.value = formatNational(country, digits);
  const preview = document.getElementById("people-add-preview");
  const problem = nationalProblem(country, digits);
  preview.textContent = digits
    ? (problem || `Will be saved as +${country[2]} ${formatNational(country, digits)}`)
    : "";
}

function openAddContact() {
  populateCountries();
  document.getElementById("people-add-error").textContent = "";
  renderPeopleAddKind();
  document.getElementById("add-contact-dialog").showModal();
}

async function submitPeopleAdd(event) {
  event.preventDefault();
  const error = document.getElementById("people-add-error");
  const submit = document.getElementById("people-add-submit");
  const group = peopleAddKind() === "group";
  const name = document.getElementById("people-add-name").value.trim();
  let phone;
  if (group) {
    phone = nationalDigits(document.getElementById("people-group-id").value);
    if (!phone) {
      error.textContent = "Enter the group ID.";
      return;
    }
  } else {
    const country = selectedCountry();
    const digits = nationalDigits(document.getElementById("people-add-id").value);
    const problem = nationalProblem(country, digits);
    if (problem) {
      error.textContent = problem;
      return;
    }
    phone = country[2] + digits;
  }
  submit.disabled = true;
  error.textContent = "Adding\u2026";
  try {
    await request("/api/contacts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "add",
        kind: group ? "group" : "person",
        phone,
        label: name || (group ? "Group" : `+${phone}`),
      }),
    });
    document.getElementById("add-contact-dialog").close();
    document.getElementById("people-add-id").value = "";
    document.getElementById("people-group-id").value = "";
    document.getElementById("people-add-name").value = "";
    await loadPeople();
  } catch (requestError) {
    error.textContent = /discovered/i.test(requestError.message)
      ? (group
        ? "Button Box has not seen that group yet. Send a message in it, then try again."
        : requestError.message)
      : requestError.message;
  } finally {
    submit.disabled = false;
  }
}

document.getElementById("people-add-form").addEventListener("submit", submitPeopleAdd);
document.getElementById("people-add-open").addEventListener("click", openAddContact);
document.getElementById("people-unpair-card").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const status = document.getElementById("people-status");
  button.disabled = true;
  status.textContent = "Reading the card\u2026";
  try {
    // The box unpairs whichever card it is currently reading, so nothing here
    // identifies a card and the wrong one cannot be removed.
    await formRequest("/nfc/unpair-presented");
    await loadPeople();
    status.textContent = "That card is unpaired.";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
document.getElementById("people-show-hidden").addEventListener("click", () => {
  peopleShowHidden = !peopleShowHidden;
  loadPeople();
});
document.getElementById("people-add-cancel").addEventListener("click", () => {
  document.getElementById("add-contact-dialog").close();
});
const countrySearch = document.getElementById("people-country-search");
countrySearch.addEventListener("input", () => {
  countryHighlight = 0;
  renderCountryOptions(countrySearch.value);
});
function openCountryList() {
  // Select everything so typing replaces the country rather than editing it
  // into nonsense. focus alone is not enough: the click that follows would
  // place a caret and drop the selection.
  countrySearch.select();
  countryHighlight = 0;
  renderCountryOptions("");
}

countrySearch.addEventListener("focus", openCountryList);
countrySearch.addEventListener("click", openCountryList);
countrySearch.addEventListener("blur", () => {
  // Restore the chosen country: a half-typed query is not a selection.
  countrySearch.value = countryDisplay(selectedCountry());
  closeCountryList();
});
countrySearch.addEventListener("keydown", (event) => {
  const list = document.getElementById("people-country-list");
  if (event.key === "Escape") {
    closeCountryList();
    return;
  }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const count = list.children.length;
    if (!count) return;
    countryHighlight = (countryHighlight + (event.key === "ArrowDown" ? 1 : count - 1)) % count;
    renderCountryOptions(countrySearch.value);
    list.children[countryHighlight]?.scrollIntoView({ block: "nearest" });
    return;
  }
  if (event.key === "Enter" && !list.hidden) {
    event.preventDefault();
    const code = list.children[countryHighlight]?.dataset.code;
    if (code) chooseCountry(code);
  }
});
document.getElementById("people-add-id").addEventListener("input", renderNumberPreview);
for (const id of ["people-kind-person", "people-kind-group"]) {
  document.getElementById(id).addEventListener("change", renderPeopleAddKind);
}

async function loadPeople() {
  const status = document.getElementById("people-status");
  // Show the view before awaiting, so a failed load reports the reason here
  // instead of leaving the caller on the previous page with no explanation.
  showView("people");
  status.textContent = "Loading\u2026";
  try {
    const data = await request("/api/contacts");
    const people = Object.entries(data.contacts || {})
      .map(([jid, contact]) => ({ jid, ...contact }))
      .sort((a, b) => a.label.localeCompare(b.label));
    document.getElementById("people-list").replaceChildren(
      ...people.map((person) => personRow(person, person.jid === data.default_recipient)),
    );
    document.getElementById("people-empty").hidden = people.length !== 0;
    const configured = new Set(people.map((person) => person.jid));
    const all = (data.discovered || []).filter((chat) => !configured.has(chat.jid));
    const available = all.filter((chat) => !chat.dismissed);
    const hiddenChats = all.filter((chat) => chat.dismissed);
    const shown = peopleShowHidden ? all : available;
    document.getElementById("people-discovered").replaceChildren(
      ...shown.map((chat) => discoveredRow(chat)),
    );
    // Hide the whole section when there is genuinely nothing to suggest, so an
    // empty heading never sits above an empty notice.
    document.getElementById("people-suggestions").hidden = all.length === 0;
    document.getElementById("people-discovered-empty").hidden = shown.length !== 0;
    const toggle = document.getElementById("people-show-hidden");
    toggle.hidden = hiddenChats.length === 0;
    toggle.textContent = peopleShowHidden
      ? "Hide dismissed chats"
      : `Show ${hiddenChats.length} dismissed`;
    if (!peoplePairing) status.textContent = "";
  } catch (error) {
    status.textContent = error.message;
  }
}

function discoveredRow(chat) {
  const hidden = Boolean(chat.dismissed);
  const row = document.createElement("li");
  row.className = "recipient-row";
  const copy = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = chat.label;
  const meta = document.createElement("span");
  meta.className = "recipient-secondary";
  meta.textContent = chat.kind;
  copy.append(name, meta);
  row.append(copy);
  const controls = document.createElement("div");
  controls.className = "recipient-actions";
  const allow = acceptanceControl("button", "BB-RECIP-05");
  allow.type = "button";
  allow.className = "compact";
  allow.textContent = "Add";
  allow.addEventListener("click", () => personAction(allow, {
    action: "add",
    jid: chat.jid,
    label: chat.label,
  }));
  controls.append(allow);
  const toggle = acceptanceControl("button", "BB-RECIP-05");
  toggle.type = "button";
  toggle.className = hidden ? "secondary compact" : "compact";
  toggle.textContent = hidden ? "Unhide" : "Not this one";
  toggle.addEventListener("click", () => personAction(toggle, {
    action: hidden ? "restore" : "dismiss",
    jid: chat.jid,
  }));
  controls.append(toggle);
  row.append(controls);
  return row;
}

async function beginPersonCardPairing(button, person) {
  if (peoplePairing?.pending) return;
  const status = document.getElementById("people-status");
  button.disabled = true;
  peoplePairing = { jid: person.jid, label: person.label, pending: true };
  status.textContent = `Starting card pairing for ${person.label}\u2026`;
  try {
    const result = await formRequest("/nfc/enroll", { jid: person.jid });
    if (!result.attempt) throw new Error("Pairing was not confirmed. Try again.");
    peoplePairing.attempt = result.attempt;
    document.getElementById("people-cancel-pairing").hidden = false;
    status.textContent = `Hold a card over Button Box for ${person.label}. You have two minutes.`;
    pollPersonCard();
  } catch (error) {
    peoplePairing = null;
    status.textContent = error.message;
    button.disabled = false;
  }
}

async function pollPersonCard() {
  window.clearTimeout(peoplePollTimer);
  if (!peoplePairing?.attempt) return;
  const status = document.getElementById("people-status");
  const label = peoplePairing.label;
  try {
    const state = await request(
      `/api/nfc-runtime?attempt=${encodeURIComponent(peoplePairing.attempt)}`,
    );
    if (state.status === "waiting") {
      status.textContent = state.healthy
        ? `Hold a card over Button Box for ${label}. Waiting for a scan\u2026`
        : "Waiting for the NFC reader. Check its connection if this continues.";
      peoplePollTimer = window.setTimeout(() => pollPersonCard(), 800);
      return;
    }
    peoplePairing = null;
    document.getElementById("people-cancel-pairing").hidden = true;
    await loadPeople();
    status.textContent = state.status === "success"
      ? `Card linked to ${label}.`
      : "Pairing ended without confirmation. Try pairing the card again.";
  } catch (_error) {
    status.textContent = "Cannot check pairing. Reconnecting\u2026";
    peoplePollTimer = window.setTimeout(() => pollPersonCard(), 2000);
  }
}

async function loadRecipients({ refresh = false, manager = false } = {}) {
  const status = document.getElementById(manager ? "manager-status" : "recipient-status");
  status.textContent = refresh ? "Refreshing WhatsApp…" : "Loading…";
  try {
    const data = refresh
      ? await formRequest("/recipients/refresh")
      : await request("/api/recipients");
    if (manager) renderRecipientManager(data);
    else renderRecipientPicker(data);
    status.textContent = refresh ? "WhatsApp refreshed." : "";
    if (manager && runtimePairing?.pending && runtimePairing.attempt) {
      window.clearTimeout(nfcPollTimer);
      nfcPollTimer = window.setTimeout(() => pollRuntimeNfc(), 0);
    }
    return data;
  } catch (error) {
    status.textContent = error.message;
    throw error;
  }
}

async function continueRecipientSetup() {
  try {
    const data = await loadRecipients();
    if (["testing", "complete"].includes(data.status)) {
      applyRecipientState(data);
    } else {
      showView("recipients");
    }
  } catch (error) {
    showError(error.message);
  }
}

async function changeTestRecipient() {
  window.clearTimeout(pollTimer);
  try {
    await loadRecipients();
    showView("recipients");
  } catch (error) {
    showError(error.message);
  }
}

async function mutateRecipient(action, token, button = null) {
  if (button) button.disabled = true;
  showError("");
  try {
    const data = await formRequest(`/recipients/${action}`, { token });
    recipientsData = data;
    if (action === "select") {
      rememberState({ recipient_setup: data });
      history.replaceState(null, "", "#continue");
      applyRecipientState(data);
    } else {
      renderRecipientManager(data);
      document.getElementById("manager-status").textContent = action === "default"
        ? "Default changed."
        : "Saved.";
    }
  } catch (error) {
    showError(error.message);
  } finally {
    if (button) button.disabled = false;
  }
}

async function mutateRecipientNumber(event, action) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const fields = new FormData(form);
  const phone = fields.get("phone");
  const name = fields.get("name") || "";
  const manager = action === "add";
  const status = document.getElementById(manager ? "manager-status" : "recipient-status");
  button.disabled = true;
  showError("");
  status.textContent = "Saving…";
  try {
    const data = await formRequest(`/recipients/${action}-number`, { phone, name });
    recipientsData = data;
    form.reset();
    if (manager) {
      renderRecipientManager(data);
      status.textContent = "Number allowed.";
    } else {
      rememberState({ recipient_setup: data });
      history.replaceState(null, "", "#continue");
      applyRecipientState(data);
    }
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function deferRecipients() {
  showError("");
  try {
    applyRecipientState(await formRequest("/recipients/defer"));
  } catch (error) {
    showError(error.message);
  }
}

function setRuntimePairingMessage(message) {
  document.getElementById("manager-status").textContent = message;
  if (!runtimePairing) return;
  runtimePairing.message = message;
  const list = document.getElementById("configured-recipient-list");
  const status = list.querySelector(".card-pairing-message");
  if (status) status.textContent = message;
}

async function beginRuntimeNfc(token, label, button) {
  if (runtimePairing?.pending) return;
  const generation = ++runtimePairingGeneration;
  window.clearTimeout(nfcPollTimer);
  runtimePairing = { token, label, pending: true, message: `Starting card pairing for ${label}…` };
  renderRecipientManager(recipientsData);
  document.getElementById("configured-recipient-list").querySelector(".card-pairing-status")?.focus();
  button.disabled = true;
  try {
    const result = await formRequest("/nfc/enroll", { token });
    if (generation !== runtimePairingGeneration) return;
    runtimePairing.attempt = result.attempt;
    if (!result.attempt) throw new Error("Pairing was not confirmed. Try again.");
    renderRecipientManager(recipientsData);
    document.getElementById("cancel-runtime-nfc").hidden = false;
    setRuntimePairingMessage(`Hold a card over Button Box for ${label}. You have two minutes.`);
    pollRuntimeNfc(generation);
  } catch (error) {
    if (generation === runtimePairingGeneration) {
      runtimePairing.pending = false;
      setRuntimePairingMessage(error.message);
      renderRecipientManager(recipientsData);
    }
  }
}

async function pollRuntimeNfc(generation = runtimePairingGeneration) {
  window.clearTimeout(nfcPollTimer);
  if (!runtimePairing?.attempt) return;
  try {
    const state = await request(`/api/nfc-runtime?attempt=${encodeURIComponent(runtimePairing.attempt)}`);
    if (generation !== runtimePairingGeneration) return;
    if (state.status === "waiting") {
      setRuntimePairingMessage(state.healthy
        ? `Hold a card over Button Box for ${runtimePairing.label}. Waiting for a scan…`
        : "Waiting for the NFC reader. Check its connection if this continues.");
      nfcPollTimer = window.setTimeout(() => pollRuntimeNfc(generation), 800);
    } else {
      runtimePairing.pending = false;
      document.getElementById("cancel-runtime-nfc").hidden = true;
      setRuntimePairingMessage(state.status === "success"
        ? `Card linked to ${runtimePairing.label} ✓`
        : "Pairing ended without confirmation. Try pairing the card again.");
      const data = await request("/api/recipients");
      if (generation === runtimePairingGeneration) renderRecipientManager(data);
    }
  } catch (_error) {
    if (generation !== runtimePairingGeneration) return;
    setRuntimePairingMessage("Cannot check pairing. Reconnecting…");
    nfcPollTimer = window.setTimeout(() => pollRuntimeNfc(generation), 2000);
  }
}

async function runtimeNfcAction(path) {
  ++runtimePairingGeneration;
  window.clearTimeout(nfcPollTimer);
  const status = document.getElementById("manager-status");
  try {
    await formRequest(path);
    if (runtimePairing) runtimePairing.pending = false;
    window.clearTimeout(nfcPollTimer);
    document.getElementById("cancel-runtime-nfc").hidden = true;
    await loadRecipients({ manager: true });
    status.textContent = path.includes("unpair") ? "Presented card unpaired." : "Card pairing cancelled.";
    setRuntimePairingMessage(status.textContent);
  } catch (error) {
    setRuntimePairingMessage(error.message);
    if (runtimePairing?.pending) {
      nfcPollTimer = window.setTimeout(() => pollRuntimeNfc(), 2000);
    }
  }
}

function scheduleNfcPoll(active = true) {
  window.clearTimeout(nfcPollTimer);
  nfcPollTimer = active ? window.setTimeout(loadNfc, 700) : null;
}

function nfcRecipientRow(recipient) {
  const row = recipientRow(recipient);
  const button = acceptanceControl("button", "BB-NFC-03");
  button.type = "button";
  button.className = "compact";
  button.textContent = "Choose";
  button.addEventListener("click", async () => {
    button.disabled = true;
    document.getElementById("nfc-choose-status").textContent = "Saving…";
    try {
      renderNfc(await formRequest("/nfc/assign", { token: recipient.token }));
    } catch (error) {
      document.getElementById("nfc-choose-status").textContent = error.message;
      button.disabled = false;
    }
  });
  row.append(button);
  return row;
}

async function allowNfcRecipient(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const status = document.getElementById("nfc-allow-status");
  if (button.disabled) return;
  button.disabled = true;
  status.textContent = "Saving…";
  try {
    // Reuse the same allowed-recipient boundary; do not select a default,
    // restart NFC, or assign the captured tag without an explicit Choose.
    recipientsData = await formRequest("/recipients/add-number", {
      phone: new FormData(form).get("phone"),
      name: new FormData(form).get("name") || "",
    });
    form.reset();
    const data = await request("/api/nfc");
    renderNfc(data);
    status.textContent = "Number allowed. Choose it above to pair this tag.";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function renderNfc(data) {
  nfcData = data;
  const mapped = data.mapped_count;
  const countCopy = mapped === 1 ? "1 tag paired." : `${mapped} tags paired.`;
  switch (data.status) {
    case "waiting": {
      showView("nfc");
      document.getElementById("nfc-waiting-status").textContent = data.remove_tag
        ? "Remove the last tag before presenting another."
        : "Waiting for a tag…";
      document.getElementById("nfc-count").textContent = countCopy;
      const finish = document.getElementById("finish-nfc");
      finish.textContent = mapped ? "Done" : "Skip NFC setup";
      finish.dataset.intent = mapped ? "done" : "skip";
      scheduleNfcPoll();
      break;
    }
    case "choose":
      showView("nfc-choose");
      document.getElementById("nfc-recipient-list").replaceChildren(
        ...data.recipients.map(nfcRecipientRow),
      );
      document.getElementById("nfc-choose-status").textContent = data.sound_warning
        ? "Tag detected, but the read sound could not play."
        : "";
      scheduleNfcPoll();
      break;
    case "already_paired":
      showView("nfc-mapped");
      document.getElementById("nfc-mapped-recipient").textContent = data.recipient?.label || "a recipient";
      scheduleNfcPoll();
      break;
    case "success":
      showView("nfc-success");
      document.getElementById("nfc-success-recipient").textContent = data.recipient?.label || "your recipient";
      document.getElementById("nfc-sound-warning").hidden = !data.sound_warning;
      scheduleNfcPoll(false);
      break;
    case "unavailable":
      showView("nfc-unavailable");
      document.getElementById("nfc-unavailable-copy").textContent = mapped
        ? "Check the reader connection and try again. Your saved tag mappings are preserved."
        : "Check the reader connection and try again. You can also skip NFC setup; the default recipient will still work.";
      document.getElementById("skip-unavailable-nfc").textContent = mapped
        ? "Done"
        : "Skip NFC setup";
      document.getElementById("skip-unavailable-nfc").dataset.intent = mapped
        ? "done"
        : "skip";
      scheduleNfcPoll(false);
      break;
    case "idle":
    default:
      scheduleNfcPoll(false);
  }
}

async function loadNfc() {
  try {
    renderNfc(await request("/api/nfc"));
  } catch (error) {
    showError(error.message);
    showView("nfc-unavailable");
    scheduleNfcPoll(false);
  }
}

async function openNfc() {
  showError("");
  try {
    renderNfc(await formRequest("/nfc/start"));
  } catch (error) {
    showError(error.message);
    showView("nfc-unavailable");
  }
}

async function nfcAction(path) {
  showError("");
  try {
    renderNfc(await formRequest(path));
  } catch (error) {
    showError(error.message);
  }
}

async function backToRecipients() {
  window.clearTimeout(nfcPollTimer);
  try {
    await formRequest("/nfc/cancel");
    managerOpen = true;
    await loadRecipients({ manager: true });
    showView("recipient-manager");
  } catch (error) {
    showError(error.message);
  }
}

async function completeOnboarding(intent) {
  window.clearTimeout(nfcPollTimer);
  showError("");
  try {
    await formRequest("/onboarding/complete", { intent });
    showView("complete");
  } catch (error) {
    showError(error.message);
  }
}

function applyState(state) {
  showError("");
  const change = document.getElementById("change-network");
  const failureRecheck = document.getElementById("failure-recheck");
  const tryAgain = document.getElementById("try-again");
  change.hidden = state.mode !== "HOME";
  failureRecheck.hidden = state.mode !== "HOME";
  tryAgain.hidden = state.mode !== "HOTSPOT";
  switch (state.phase) {
    case "WIFI_SELECT":
      showView("wifi");
      if (!document.getElementById("networks").children.length) scanNetworks();
      break;
    case "WIFI_CONNECTING":
      showView("checking");
      break;
    case "WIFI_ASSOCIATED":
      if (state.safe_error) {
        showView("failed");
        document.getElementById("failure-copy").textContent = "Wi-Fi connected, but internet is unavailable. Check again or choose another network.";
      } else {
        showView("checking");
      }
      break;
    case "WIFI_FAILED":
      showView("failed");
      document.getElementById("failure-copy").textContent = state.safe_error === "ASSOCIATION_FAILED"
        ? "Button Box could not join that Wi-Fi network. Check its name and password."
        : "Wi-Fi connected, but internet is unavailable. Check again or choose another network.";
      break;
    case "WHATSAPP_PENDING":
    case "WHATSAPP_READY":
      applyWhatsAppState(state);
      break;
    default:
      showView("wifi");
  }
}

function taskStatus(label, status, route = "#continue") {
  const item = document.createElement("li");
  item.className = "task-row";
  const link = acceptanceControl("a", "BB-SETUP-01");
  link.href = route;
  link.textContent = label;
  const badge = document.createElement("span");
  badge.className = `task-status ${status}`;
  badge.textContent = status === "complete" ? "Complete" : status === "optional" ? "Optional" : "To do";
  item.append(link, badge);
  return item;
}

function setupProgress(state) {
  if (state.mode === "RUNTIME") return state.setup;
  const wifiComplete = ["WHATSAPP_PENDING", "WHATSAPP_READY"].includes(state.phase);
  const whatsappComplete = state.phase === "WHATSAPP_READY" || state.whatsapp?.status === "ready";
  const recipient = state.recipient_setup || {};
  const proof = recipient.proof || {};
  return {
    wifi: wifiComplete ? "complete" : "attention",
    whatsapp: whatsappComplete ? "complete" : "attention",
    recipient: recipient.default ? "complete" : "attention",
    first_message: proof.received && proof.played && proof.replied ? "complete" : "attention",
    nfc: (state.nfc_setup?.mapped_count || 0) > 0 ? "complete" : "optional",
  };
}

function renderSetup(state) {
  const progress = setupProgress(state);
  const activeSetup = state.mode !== "RUNTIME";
  document.getElementById("required-tasks").replaceChildren(
    taskStatus("Connect Wi-Fi", progress.wifi, activeSetup ? "#continue" : "#advanced"),
    taskStatus("Link WhatsApp", progress.whatsapp, "#whatsapp"),
    taskStatus("Choose a default recipient", progress.recipient, activeSetup ? "#continue" : "#advanced"),
    taskStatus("Receive, play, record, and send a test message", progress.first_message, activeSetup ? "#continue" : "#activity"),
  );
  document.getElementById("optional-tasks").replaceChildren(
    taskStatus("Pair NFC cards", progress.nfc, activeSetup ? "#continue" : "#advanced"),
    taskStatus("Personalize button, sounds, and quiet hours", "optional", "#settings"),
  );
}

function renderHome(state) {
  const progress = setupProgress(state);
  const runtimeRunning = state.mode === "RUNTIME" && state.health?.runtime === "running";
  const ready = runtimeRunning
    && [progress.wifi, progress.whatsapp, progress.recipient, progress.first_message]
      .every((status) => status === "complete");
  document.getElementById("home-attention").hidden = ready;
  document.getElementById("home-summary").textContent = ready && state.mode === "RUNTIME"
    ? "Connected and set up for voice messages. Say hello to someone you love."
    : "A few small steps to bring your people closer. Pick up where you left off.";
  document.getElementById("home-wifi").textContent = progress.wifi === "complete"
    ? `Connected${state.health?.network_name ? ` · ${state.health.network_name}` : ""}`
    : "Needs attention";
  document.getElementById("home-whatsapp").textContent = progress.whatsapp === "complete" ? "Linked" : "Needs attention";
  document.getElementById("home-runtime").textContent = state.mode === "RUNTIME"
    ? (ready ? "Ready" : "Needs attention")
    : "Setup in progress";
  for (const [id, complete] of [
    ["home-wifi", progress.wifi === "complete"],
    ["home-whatsapp", progress.whatsapp === "complete"],
    ["home-runtime", ready && state.mode === "RUNTIME"],
  ]) {
    const tile = document.getElementById(id).parentElement;
    tile.classList.toggle("good", complete);
    tile.classList.toggle("attention", !complete);
  }
}

function renderIdentity(state) {
  const id = typeof state.box_id === "string" && /^BOX-[1-9][0-9]{0,8}$/.test(state.box_id)
    ? state.box_id : null;
  const label = document.getElementById("box-id");
  const button = document.getElementById("copy-box-id");
  if (label.textContent !== (id || "Not assigned")) {
    button.textContent = "Copy";
    document.getElementById("box-id-status").textContent = "";
  }
  label.textContent = id || "Not assigned";
  button.disabled = !id;
}

function populateSettings(payload) {
  currentSettings = payload.settings;
  const value = currentSettings;
  document.querySelector(`[name="recording_mode"][value="${value.recording_mode}"]`).checked = true;
  document.querySelector(`[name="after_listening"][value="${value.after_listening}"]`).checked = true;
  document.getElementById("max-recording").value = String(value.max_recording_seconds);
  document.getElementById("ringtone").value = value.ringtone_id;
  document.getElementById("master-volume").value = String(value.master_volume_percent);
  document.getElementById("volume-output").value = `${value.master_volume_percent}%`;
  document.getElementById("arrival-signal").value = value.arrival_signal;
  document.getElementById("quiet-enabled").checked = value.quiet_hours.enabled;
  document.getElementById("quiet-start").value = value.quiet_hours.start;
  document.getElementById("quiet-end").value = value.quiet_hours.end;
  document.getElementById("timezone").value = value.timezone;
  document.getElementById("nfc-beep").checked = value.nfc_confirmation_beep;
  document.getElementById("settings-attention").hidden = !payload.attention;
  const suggested = Intl.DateTimeFormat().resolvedOptions().timeZone;
  document.getElementById("timezone-help").textContent = suggested && suggested !== value.timezone
    ? `This phone suggests ${suggested}. Confirm the time zone before saving.`
    : "Confirm this time zone so quiet hours follow local time.";
}

async function loadSettings() {
  const status = document.getElementById("settings-status");
  status.textContent = "Loading settings…";
  try {
    populateSettings(await request("/api/settings"));
    status.textContent = "";
  } catch (error) {
    status.textContent = error.message;
  }
}

function settingsCandidate() {
  return {
    timezone: document.getElementById("timezone").value.trim(),
    recording_mode: document.querySelector('[name="recording_mode"]:checked').value,
    after_listening: document.querySelector('[name="after_listening"]:checked').value,
    max_recording_seconds: Number(document.getElementById("max-recording").value),
    ringtone_id: document.getElementById("ringtone").value,
    master_volume_percent: Number(document.getElementById("master-volume").value),
    arrival_signal: document.getElementById("arrival-signal").value,
    quiet_hours: {
      enabled: document.getElementById("quiet-enabled").checked,
      start: document.getElementById("quiet-start").value,
      end: document.getElementById("quiet-end").value,
    },
    nfc_confirmation_beep: document.getElementById("nfc-beep").checked,
  };
}

async function saveSettings(event) {
  event.preventDefault();
  const status = document.getElementById("settings-status");
  const candidate = settingsCandidate();
  if (candidate.recording_mode === "hold_release" && currentSettings?.recording_mode !== "hold_release") {
    const accepted = window.confirm("Press and hold sends immediately when the button is released. There is no playback review. Save this mode?");
    if (!accepted) return;
  }
  status.textContent = "Saving…";
  try {
    const payload = await request("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ revision: currentSettings.revision, settings: candidate }),
    });
    populateSettings(payload);
    status.textContent = "Settings saved. Changes apply at the next idle interaction.";
  } catch (error) {
    status.textContent = error.status === 409 ? `${error.message} Your unsaved choices were not overwritten.` : error.message;
  }
}

function formatDuration(seconds) {
  if (seconds == null) return "—";
  return seconds < 60 ? `${Math.round(seconds)}s` : `${(seconds / 60).toFixed(1)}m`;
}

function activityMessageList(items, kind) {
  const container = document.createElement("div");
  container.className = "activity-list";
  if (!items.length) {
    container.textContent = kind === "queue" ? "Nothing waiting." : kind === "played" ? "Nothing played recently." : "Empty.";
    return container;
  }
  for (const item of items) {
    const row = document.createElement("article");
    row.className = "activity-row";
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    const mediaLabel = kind === "played" ? item.media_kind === "video_soundtrack" ? "Video soundtrack · " : "Voice message · " : "";
    title.textContent = `${mediaLabel}${item.sender} · ${item.chat}`;
    const meta = document.createElement("span");
    const timeLabel = kind === "played" ? "Played " : "";
    meta.textContent = `${timeLabel}${new Date(item.ts * 1000).toLocaleString()} · ${formatDuration(item.dur)}`;
    copy.append(title, meta);
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "none";
    const query = kind === "played" ? "?played=1" : kind === "hold" ? "?hold=1" : kind === "trash" ? "?trash=1" : "";
    if (kind !== "played" || item.available) audio.src = `/audio/${encodeURIComponent(item.token)}${query}`;
    const actions = document.createElement("div");
    actions.className = "button-row";
    const operations = kind === "queue" ? [["hold", "Hold"], ["delete", "Trash"]]
      : kind === "hold" ? [["resume", "Reinstate"]]
        : kind === "played" ? [["requeue", item.queued ? "In queue" : item.available ? "Add to queue" : "Unavailable"]]
          : [["reinstate", "Reinstate"]];
    for (const [operation, label] of operations) {
      const button = acceptanceControl("button", kind === "played" ? "BB-RQ-001" : "BB-ACT-02");
      button.type = "button";
      button.className = "secondary compact";
      button.textContent = label;
      button.disabled = kind === "played" && (item.queued || !item.available);
      button.addEventListener("click", async () => {
        button.disabled = true;
        await moveMessage(operation, item.token);
      });
      actions.append(button);
    }
    row.append(copy, audio, actions);
    container.append(row);
  }
  return container;
}

async function moveMessage(operation, token) {
  try {
    await request(`/api/${operation}?f=${encodeURIComponent(token)}`, { method: "POST" });
    await loadActivity();
  } catch (error) {
    showError(error.message);
  }
}

async function loadActivity() {
  try {
    const data = await request("/api/data");
    const cards = [["Sent", data.cards.sent_total], ["Received", data.cards.recv_total], ["Played", data.cards.plays], ["Rings", data.cards.rings]];
    document.getElementById("activity-summary").replaceChildren(...cards.map(([label, value]) => {
      const card = document.createElement("div");
      card.className = "status-card";
      const name = document.createElement("span"); name.textContent = label;
      const count = document.createElement("strong"); count.textContent = String(value);
      card.append(name, count); return card;
    }));
    const timeline = document.getElementById("activity-timeline");
    timeline.replaceChildren(...data.interactions.map((item) => {
      const row = document.createElement("article"); row.className = "activity-row";
      const title = document.createElement("strong"); title.textContent = item.outcome_label;
      const meta = document.createElement("span"); meta.textContent = `${item.flow === "standalone" ? "New message · " : item.flow === "reply" ? "Reply · " : ""}${new Date(item.ts * 1000).toLocaleString()}`;
      row.append(title, meta); return row;
    }));
    if (!data.interactions.length) timeline.textContent = "No activity yet. Events will appear here as you use Button Box.";
    document.getElementById("activity-queue").replaceChildren(activityMessageList(data.queue, "queue"));
    document.getElementById("activity-played").replaceChildren(activityMessageList(data.recently_played, "played"));
    document.getElementById("activity-hold").replaceChildren(activityMessageList(data.hold, "hold"));
    document.getElementById("activity-trash").replaceChildren(activityMessageList(data.trash, "trash"));
  } catch (error) {
    document.getElementById("activity-timeline").textContent = error.message;
  }
}

async function loadAdvanced() {
  const health = document.getElementById("advanced-health");
  health.replaceChildren();
  const runtime = document.createElement("div"); runtime.className = "status-card";
  const runtimeStatus = currentState?.mode !== "RUNTIME" ? "Setup mode"
    : currentState.health?.runtime === "running" ? "Running" : "Needs attention";
  runtime.innerHTML = `<span>Runtime</span><strong>${runtimeStatus}</strong>`;
  const version = document.createElement("div"); version.className = "status-card";
  const versionLabel = document.createElement("span"); versionLabel.textContent = "Software";
  const versionValue = document.createElement("strong"); versionValue.textContent = currentState?.health?.software_version || "Installed";
  version.append(versionLabel, versionValue);
  health.append(runtime, version);
  if (currentState?.mode !== "RUNTIME") {
    document.getElementById("listener-profiles").textContent = "Listener profiles become available after setup.";
    return;
  }
  try {
    const contacts = await request("/api/contacts");
    const profiles = Object.entries(contacts.listeners || {});
    document.getElementById("listener-profiles").replaceChildren(...(profiles.length ? profiles.map(([jid, profile]) => {
      const row = document.createElement("article"); row.className = "activity-row";
      const name = document.createElement("strong"); name.textContent = profile.name;
      const meta = document.createElement("span"); meta.textContent = profile.listened_clip ? "Custom listened sound" : "Default listened sound";
      const actions = document.createElement("div"); actions.className = "button-row";
      const edit = acceptanceControl("button", "BB-ADV-01"); edit.type = "button"; edit.className = "secondary compact"; edit.textContent = "Edit";
      edit.addEventListener("click", () => {
        document.getElementById("listener-jid").value = jid;
        document.getElementById("listener-name").value = profile.name;
        document.getElementById("listener-clip").value = profile.listened_clip || "";
        document.getElementById("listener-name").focus();
      });
      const remove = acceptanceControl("button", "BB-ADV-01"); remove.type = "button"; remove.className = "danger-button compact"; remove.textContent = "Remove";
      remove.addEventListener("click", () => mutateListener({ action: "remove", jid }));
      actions.append(edit, remove);
      row.append(name, meta, actions); return row;
    }) : [document.createTextNode("No listener profiles.")]));
  } catch (error) {
    document.getElementById("listener-profiles").textContent = error.message;
  }
}

async function mutateListener(payload) {
  const status = document.getElementById("listener-status");
  status.textContent = "Saving…";
  try {
    await request("/api/listeners", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    status.textContent = "Saved.";
    await loadAdvanced();
  } catch (error) {
    status.textContent = error.message;
  }
}

async function saveListener(event) {
  event.preventDefault();
  await mutateListener({
    action: "upsert",
    jid: document.getElementById("listener-jid").value.trim(),
    name: document.getElementById("listener-name").value.trim(),
    listened_clip: document.getElementById("listener-clip").value.trim(),
  });
}

async function changeWifi(event) {
  event.preventDefault();
  const status = document.getElementById("wifi-change-status");
  const security = document.querySelector('[name="new_wifi_security"]:checked').value;
  const payload = {
    ssid: document.getElementById("new-wifi-name").value,
    password: security === "open" ? "" : document.getElementById("new-wifi-password").value,
    security,
  };
  status.textContent = "Starting the network checks…";
  try {
    const result = await request("/api/wifi-change", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    status.textContent = result.message;
  } catch (error) {
    status.textContent = error.message;
  }
}

async function ringNow() {
  const status = document.getElementById("ring-now-status");
  if (currentState?.mode !== "RUNTIME") {
    status.textContent = "Ring is available after setup is complete.";
    return;
  }
  try {
    await request("/api/ring", { method: "POST" });
    status.textContent = "Ringtone requested. Listen for it when the box is idle.";
  } catch (error) {
    status.textContent = error.message;
  }
}

async function route() {
  renderIdentity(currentState);
  const routeName = location.hash.slice(1) || "home";
  const navRoute = ["continue", "whatsapp", "recipient-picker", "recipients", "people"].includes(routeName)
    ? (currentState.mode === "RUNTIME" ? "advanced" : "setup") : routeName;
  document.querySelectorAll("[data-route]").forEach((link) => {
    link.setAttribute("aria-current", link.dataset.route === navRoute ? "page" : "false");
  });
  window.clearTimeout(pollTimer);
  window.clearTimeout(nfcPollTimer);
  if (currentState.mode === "RUNTIME" && routeName === "continue") {
    location.replace("#home");
    return;
  }
  if (routeName === "whatsapp") {
    if (currentState.mode === "RUNTIME") {
      await loadRuntimeWhatsApp();
    } else {
      applyWhatsAppState(currentState, { manage: true });
    }
    return;
  }
  if (routeName === "people") {
    await loadPeople();
    return;
  }
  if (routeName === "recipient-picker") {
    await loadRecipients();
    showView("recipients");
    return;
  }
  if (routeName === "recipients") {
    managerOpen = true;
    await loadRecipients({ manager: true });
    showView("recipient-manager");
    return;
  }
  if (routeName === "continue") {
    applyState(currentState);
    return;
  }
  const canonical = new Set(["home", "setup", "settings", "activity", "advanced"]);
  const selected = canonical.has(routeName) ? routeName : "home";
  showView(selected);
  if (selected === "home") renderHome(currentState);
  if (selected === "setup") renderSetup(currentState);
  if (selected === "settings") await loadSettings();
  if (selected === "activity") await loadActivity();
  if (selected === "advanced") await loadAdvanced();
}

async function loadState() {
  if (loadingState) return;
  loadingState = true;
  try {
    currentState = await request("/api/state");
    showError("");
    await route();
  } catch (error) {
    showError(error.message);
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(loadState, 1500);
  } finally {
    loadingState = false;
  }
}

async function pairWhatsApp(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  const label = button.textContent;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "Working…";
  showError("");
  try {
    const phone = document.getElementById("whatsapp-phone").value;
    applyState(rememberState(await formRequest("/whatsapp/pair/start", { phone })));
  } catch (error) {
    showError(error.message);
    document.getElementById("whatsapp-phone").focus();
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.textContent = label;
  }
}

async function cancelPairing() {
  showError("");
  try {
    applyState(rememberState(await formRequest("/whatsapp/pair/cancel")));
  } catch (error) {
    showError(error.message);
  }
}

async function copyText(text, button, status, successMessage) {
  try {
    await window.ButtonBoxClipboard.copyText(text, {
      secure: window.isSecureContext,
      clipboard: navigator.clipboard,
      document,
    });
    button.textContent = "Copied";
    status.textContent = successMessage;
  } catch (error) {
    status.textContent = "Press and hold the text to copy it manually.";
  }
}

function copySetupUrl() {
  const button = document.getElementById("copy-setup-url");
  return copyText(
    document.getElementById("setup-url").textContent.trim(),
    button,
    document.getElementById("setup-url-copy-status"),
    "Setup URL copied.",
  );
}

function copyPairingCode() {
  const code = document.getElementById("pairing-code").textContent.trim();
  if (!code || code === "New code pending") return;
  return copyText(
    code,
    document.getElementById("copy-pairing-code"),
    document.getElementById("copy-status"),
    "Code copied. Switch to WhatsApp and paste it.",
  );
}

async function unlinkWhatsApp(event) {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  showError("");
  try {
    applyState(rememberState(await formRequest("/whatsapp/unlink", { confirm: "unlink" })));
    document.getElementById("unlink-form").hidden = true;
    document.getElementById("whatsapp-phone").value = "+";
    document.getElementById("whatsapp-phone").focus();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
  }
}

document.getElementById("scan-again").addEventListener("click", scanNetworks);
document.getElementById("wifi-form").addEventListener("submit", () => {
  const button = document.getElementById("connect-wifi");
  button.disabled = true;
  button.textContent = "Connecting...";
  document.getElementById("scan-status").textContent = "Saving Wi-Fi details and preparing to switch networks...";
});
document.getElementById("try-again").addEventListener("click", () => {
  showView("wifi");
  scanNetworks();
});
for (const button of document.querySelectorAll("[data-recheck]")) {
  button.addEventListener("click", loadState);
}
for (const radio of document.querySelectorAll('input[name="security"]')) {
  radio.addEventListener("change", () => {
    const open = document.querySelector('input[name="security"]:checked').value === "open";
    const input = document.getElementById("wifi-password");
    document.getElementById("password-field").hidden = open;
    input.required = !open;
    if (open) input.value = "";
  });
}
document.getElementById("whatsapp-form").addEventListener("submit", pairWhatsApp);
document.getElementById("copy-setup-url").addEventListener("click", copySetupUrl);
document.getElementById("copy-pairing-code").addEventListener("click", copyPairingCode);
document.getElementById("cancel-pairing").addEventListener("click", cancelPairing);
document.getElementById("cancel-progress").addEventListener("click", cancelPairing);
document.getElementById("retry-pairing").addEventListener("click", async (event) => {
  if (currentState?.whatsapp?.safe_error !== "CLEANUP_FAILED") {
    showView("whatsapp");
    document.getElementById("whatsapp-phone").focus();
    return;
  }
  const button = event.currentTarget;
  button.disabled = true;
  showError("");
  try {
    applyState(rememberState(await formRequest("/whatsapp/unlink", { confirm: "unlink" })));
    document.getElementById("whatsapp-phone").value = "+";
    document.getElementById("whatsapp-phone").focus();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
  }
});
document.getElementById("show-unlink").addEventListener("click", () => {
  const form = document.getElementById("unlink-form");
  form.hidden = false;
  form.querySelector('button[type="submit"]').focus();
});
document.getElementById("keep-account").addEventListener("click", () => {
  document.getElementById("unlink-form").hidden = true;
  document.getElementById("show-unlink").focus();
});
document.getElementById("unlink-form").addEventListener("submit", unlinkWhatsApp);
document.getElementById("continue-recipients").addEventListener("click", () => {
  location.hash = "recipient-picker";
});
document.getElementById("refresh-recipients").addEventListener("click", () => loadRecipients({ refresh: true }));
document.getElementById("defer-recipients").addEventListener("click", deferRecipients);
document.getElementById("manual-default-form").addEventListener("submit", (event) => {
  mutateRecipientNumber(event, "select");
});
document.getElementById("resume-recipients").addEventListener("click", continueRecipientSetup);
document.getElementById("change-test-recipient").addEventListener("click", changeTestRecipient);
document.getElementById("open-recipient-manager").addEventListener("click", async () => {
  location.hash = "recipients";
});
document.getElementById("manager-refresh").addEventListener("click", () => loadRecipients({ refresh: true, manager: true }));
document.getElementById("manual-allow-form").addEventListener("submit", (event) => {
  mutateRecipientNumber(event, "add");
});
document.getElementById("nfc-allow-form").addEventListener("submit", allowNfcRecipient);
document.getElementById("continue-nfc").addEventListener("click", () => {
  if (currentState?.mode === "RUNTIME") {
    document.getElementById("manager-status").textContent = "Choose Pair card beside a recipient.";
  } else {
    openNfc();
  }
});
document.getElementById("unpair-presented-nfc").addEventListener("click", () => runtimeNfcAction("/nfc/unpair-presented"));
document.getElementById("cancel-runtime-nfc").addEventListener("click", () => runtimeNfcAction("/nfc/cancel-runtime"));
document.getElementById("back-from-nfc").addEventListener("click", backToRecipients);
document.getElementById("back-from-unavailable").addEventListener("click", backToRecipients);
document.getElementById("cancel-nfc-tag").addEventListener("click", backToRecipients);
document.getElementById("cancel-mapped-tag").addEventListener("click", backToRecipients);
document.getElementById("retry-nfc").addEventListener("click", () => nfcAction("/nfc/retry"));
document.getElementById("reassign-nfc").addEventListener("click", () => nfcAction("/nfc/reassign"));
document.getElementById("keep-nfc-pairing").addEventListener("click", () => nfcAction("/nfc/next"));
document.getElementById("pair-another-nfc").addEventListener("click", () => nfcAction("/nfc/next"));
document.getElementById("finish-nfc").addEventListener("click", (event) => {
  completeOnboarding(event.currentTarget.dataset.intent || "skip");
});
document.getElementById("skip-unavailable-nfc").addEventListener("click", (event) => {
  completeOnboarding(event.currentTarget.dataset.intent || "skip");
});
document.getElementById("done-nfc").addEventListener("click", () => completeOnboarding("done"));
document.getElementById("settings-form").addEventListener("submit", saveSettings);
document.getElementById("master-volume").addEventListener("input", (event) => {
  document.getElementById("volume-output").value = `${event.currentTarget.value}%`;
});
document.getElementById("preview-ringtone").addEventListener("click", async () => {
  const status = document.getElementById("settings-status");
  status.textContent = "Playing preview…";
  try {
    await request("/api/ringtone-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ringtone_id: document.getElementById("ringtone").value }),
    });
    status.textContent = "Preview playing.";
  } catch (error) {
    status.textContent = error.status === 404 ? "Ringtone preview is available after setup." : error.message;
  }
});
document.getElementById("ring-now").addEventListener("click", ringNow);
document.getElementById("copy-box-id").addEventListener("click", () => {
  const button = document.getElementById("copy-box-id");
  if (button.disabled) return;
  return copyText(document.getElementById("box-id").textContent, button,
    document.getElementById("box-id-status"), "Box ID copied.");
});
document.getElementById("skip-link").addEventListener("click", (event) => {
  event.preventDefault();
  document.getElementById("main").focus();
});
document.getElementById("listener-form").addEventListener("submit", saveListener);
document.getElementById("wifi-change-form").addEventListener("submit", changeWifi);
document.querySelectorAll('[name="new_wifi_security"]').forEach((radio) => {
  radio.addEventListener("change", () => {
    const protectedNetwork = document.querySelector('[name="new_wifi_security"]:checked').value === "protected";
    const password = document.getElementById("new-wifi-password");
    password.required = protectedNetwork;
    password.disabled = !protectedNetwork;
    if (!protectedNetwork) password.value = "";
  });
});
document.getElementById("people-cancel-pairing").addEventListener("click", async () => {
  window.clearTimeout(peoplePollTimer);
  const status = document.getElementById("people-status");
  peoplePairing = null;
  document.getElementById("people-cancel-pairing").hidden = true;
  try {
    await formRequest("/nfc/cancel-runtime");
    status.textContent = "Card pairing cancelled.";
  } catch (error) {
    status.textContent = error.message;
  }
  await loadPeople();
});
document.getElementById("manage-whatsapp").addEventListener("click", () => {
  location.hash = "whatsapp";
});
window.addEventListener("hashchange", () => {
  // Completion replaces the setup server with runtime. A cached HOME state
  // must not keep navigation (including Activity) stuck in the old mode.
  loadState();
});
loadState();
