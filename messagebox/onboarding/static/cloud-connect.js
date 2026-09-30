const start = document.getElementById("cloud-start");
const claim = document.getElementById("cloud-claim");
const status = document.getElementById("cloud-status");
const link = document.getElementById("cloud-link");
const qr = document.getElementById("cloud-qr");
const copy = document.getElementById("cloud-copy");
const expiry = document.getElementById("cloud-expiry");
const complete = document.getElementById("cloud-complete");
let currentLink = "";
let finishing = false;
let actionPending = false;
let actionVersion = 0;
let actionError = "";
let currentStatus = "";

function render(data) {
  if (data.status !== currentStatus && ["awaiting_button", "waiting_for_whatsapp", "claimed"].includes(data.status)) {
    actionError = "";
  }
  currentStatus = data.status;
  const waiting = data.status === "awaiting_button" || data.status === "waiting_for_whatsapp";
  claim.hidden = !waiting;
  start.hidden = waiting || data.status === "claimed";
  complete.hidden = data.status !== "claimed";
  if (waiting) {
    currentLink = data.whatsapp_url;
    link.href = currentLink;
    qr.src = `/api/cloud-claim/qr?t=${encodeURIComponent(data.expires_at)}`;
    expiry.textContent = `This link expires at ${new Date(data.expires_at * 1000).toLocaleTimeString()}.`;
  }
  status.textContent = actionError || {
    not_started: "Start a connection link when the box is on home Wi-Fi.",
    awaiting_button: "Send the prepared WhatsApp message, then press the physical box button once.",
    waiting_for_whatsapp: "Button press received. Waiting for the WhatsApp claim to complete.",
    claimed: "This box is connected. Continue trial setup in WhatsApp or the cloud dashboard.",
    expired: "The connection link expired. Get a new one to continue."
  }[data.status] || "Connection status is unavailable. Try again shortly.";
}

async function refresh() {
  if (finishing || actionPending) return;
  const version = actionVersion;
  try {
    const response = await fetch("/api/cloud-claim", { cache: "no-store" });
    if (!response.ok) throw new Error("unavailable");
    const data = await response.json();
    // A poll started before a click must not replace that action's result.
    if (version === actionVersion && !finishing && !actionPending) render(data);
  } catch {
    if (version === actionVersion && !finishing && !actionPending && !actionError) {
      status.textContent = "Connection status is unavailable. Try again shortly.";
    }
  }
}

start.addEventListener("click", async () => {
  start.disabled = true;
  actionPending = true;
  actionVersion++;
  actionError = "";
  status.textContent = "Creating your connection link…";
  try {
    const response = await fetch("/api/cloud-claim/start", { method: "POST", cache: "no-store" });
    if (!response.ok) throw new Error("unavailable");
    render(await response.json());
  } catch {
    actionError = "Could not start a connection link. Try again shortly.";
    status.textContent = actionError;
  } finally {
    actionPending = false;
    start.disabled = false;
  }
});
copy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(currentLink);
    status.textContent = "Connection link copied.";
  } catch {
    status.textContent = "Copy is unavailable here. Use Open WhatsApp instead.";
  }
});
complete.addEventListener("click", async () => {
  complete.disabled = true;
  actionPending = true;
  actionVersion++;
  actionError = "";
  status.textContent = "Finishing box setup…";
  try {
    const response = await fetch("/onboarding/complete", {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "intent=done"
    });
    if (!response.ok) throw new Error("unavailable");
    finishing = true;
    status.textContent = "Box setup is finishing. The local dashboard will open shortly.";
    window.setTimeout(() => { window.location.href = "/"; }, 5000);
  } catch {
    complete.disabled = false;
    actionError = "Could not finish setup. Try again shortly.";
    status.textContent = actionError;
  } finally {
    actionPending = false;
  }
});
refresh();
window.setInterval(refresh, 3000);
