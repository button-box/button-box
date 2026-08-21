"use strict";

const views = [
  "wifi",
  "checking",
  "whatsapp",
  "code",
  "pairing-progress",
  "pairing-error",
  "ready",
  "failed",
];
const activePairingStates = new Set(["starting", "code_pending", "bootstrapping", "verifying"]);
let pollTimer = null;
let loadingState = false;
let lastView = "";

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

function schedulePoll(status) {
  window.clearTimeout(pollTimer);
  pollTimer = activePairingStates.has(status) ? window.setTimeout(loadState, 1500) : null;
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
    showView("ready");
    document.getElementById("linked-account").textContent = whatsapp.phone_hint
      ? `${whatsapp.phone_hint} is linked and connected.`
      : "WhatsApp is linked and connected.";
    const count = whatsapp.eligible_count;
    document.getElementById("eligible-count").textContent = count === 1
      ? "1 recent group or chat is ready for the next setup step."
      : `${count} recent groups or chats are ready for the next setup step.`;
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
loadState();
