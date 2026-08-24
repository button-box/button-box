"use strict";

const views = [
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

function showView(name) {
  for (const view of views) {
    const element = document.getElementById(`${view}-view`);
    if (element) element.hidden = view !== name;
  }
  if (lastView !== name) {
    lastView = name;
    document.querySelector(`#${name}-view h1`)?.focus({ preventScroll: true });
  }
}

function showError(message) {
  const element = document.getElementById("page-error");
  if (!element) return;
  element.textContent = message;
  element.hidden = !message;
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    ...options,
  });
  const type = response.headers.get("content-type") || "";
  const data = type.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const error = new Error(data?.error || "The Message Box did not respond.");
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
      const button = document.createElement("button");
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
    ? window.setTimeout(loadState, 1500)
    : null;
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

function applyWhatsAppState(state) {
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
    applyRecipientState(state.recipient_setup, state.nfc_setup);
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
  meta.textContent = recipient.is_default
    ? `${recipient.kind} · default${tagCopy}`
    : `${recipient.kind}${tagCopy}`;
  copy.append(name, meta);
  row.append(copy);
  if (actions.length) {
    const controls = document.createElement("div");
    controls.className = "recipient-actions";
    actions.forEach(({ action, label }) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = action === "remove" ? "danger-button compact" : "compact";
    button.textContent = label;
    button.addEventListener("click", () => mutateRecipient(action, recipient.token, button));
      controls.append(button);
    });
    row.append(controls);
  }
  return row;
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
  const configured = data.recipients.filter((recipient) => recipient.configured);
  const available = data.recipients.filter((recipient) => recipient.available && !recipient.configured);
  document.getElementById("configured-recipient-list").replaceChildren(
    ...configured.map((recipient) => recipientRow(recipient, recipient.is_default ? [] : [
      { action: "default", label: "Make default" },
      { action: "remove", label: "Remove" },
    ])),
  );
  document.getElementById("available-recipient-list").replaceChildren(
    ...available.map((recipient) => recipientRow(
      recipient,
      [{ action: "add", label: "Allow" }],
    )),
  );
  document.getElementById("manager-empty").hidden = available.length !== 0;
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
    return data;
  } catch (error) {
    status.textContent = error.message;
    throw error;
  }
}

async function mutateRecipient(action, token, button = null) {
  if (button) button.disabled = true;
  showError("");
  try {
    const data = await formRequest(`/recipients/${action}`, { token });
    recipientsData = data;
    if (action === "select") {
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
  const phone = new FormData(form).get("phone");
  const manager = action === "add";
  const status = document.getElementById(manager ? "manager-status" : "recipient-status");
  button.disabled = true;
  showError("");
  status.textContent = "Saving…";
  try {
    const data = await formRequest(`/recipients/${action}-number`, { phone });
    recipientsData = data;
    form.reset();
    if (manager) {
      renderRecipientManager(data);
      status.textContent = "Number allowed.";
    } else {
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

function scheduleNfcPoll(active = true) {
  window.clearTimeout(nfcPollTimer);
  nfcPollTimer = active ? window.setTimeout(loadNfc, 700) : null;
}

function nfcRecipientRow(recipient) {
  const row = recipientRow(recipient);
  const button = document.createElement("button");
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
        ? "The box could not join that Wi-Fi network. Check its name and password."
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

async function loadState() {
  if (loadingState) return;
  loadingState = true;
  try {
    applyState(await request("/api/state"));
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
  button.disabled = true;
  showError("");
  try {
    const phone = document.getElementById("whatsapp-phone").value;
    applyState(await formRequest("/whatsapp/pair/start", { phone }));
  } catch (error) {
    showError(error.message);
    document.getElementById("whatsapp-phone").focus();
  } finally {
    button.disabled = false;
  }
}

async function cancelPairing() {
  showError("");
  try {
    applyState(await formRequest("/whatsapp/pair/cancel"));
  } catch (error) {
    showError(error.message);
  }
}

async function copyText(text, button, status, successMessage) {
  try {
    if (window.isSecureContext && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const field = document.createElement("textarea");
      field.value = text;
      field.readOnly = true;
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.append(field);
      field.select();
      const copied = document.execCommand("copy");
      field.remove();
      if (!copied) throw new Error("copy unavailable");
    }
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
    applyState(await formRequest("/whatsapp/unlink", { confirm: "unlink" }));
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
document.getElementById("retry-pairing").addEventListener("click", () => {
  showView("whatsapp");
  document.getElementById("whatsapp-phone").focus();
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
document.getElementById("continue-recipients").addEventListener("click", async () => {
  try {
    await loadRecipients();
    showView("recipients");
  } catch (error) {
    showError(error.message);
  }
});
document.getElementById("refresh-recipients").addEventListener("click", () => loadRecipients({ refresh: true }));
document.getElementById("defer-recipients").addEventListener("click", deferRecipients);
document.getElementById("manual-default-form").addEventListener("submit", (event) => {
  mutateRecipientNumber(event, "select");
});
document.getElementById("resume-recipients").addEventListener("click", async () => {
  try {
    await loadRecipients();
    showView("recipients");
  } catch (error) {
    showError(error.message);
  }
});
document.getElementById("open-recipient-manager").addEventListener("click", async () => {
  managerOpen = true;
  try {
    await loadRecipients({ manager: true });
    showView("recipient-manager");
  } catch (error) {
    showError(error.message);
  }
});
document.getElementById("manager-refresh").addEventListener("click", () => loadRecipients({ refresh: true, manager: true }));
document.getElementById("manual-allow-form").addEventListener("submit", (event) => {
  mutateRecipientNumber(event, "add");
});
document.getElementById("continue-nfc").addEventListener("click", openNfc);
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
loadState();
