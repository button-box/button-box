const start = document.getElementById("cloud-start");
const claim = document.getElementById("cloud-claim");
const status = document.getElementById("cloud-status");
const link = document.getElementById("cloud-link");
const qr = document.getElementById("cloud-qr");
const copy = document.getElementById("cloud-copy");
const expiry = document.getElementById("cloud-expiry");
const complete = document.getElementById("cloud-complete");
const cancel = document.getElementById("cloud-cancel");
let currentLink = "";
let currentClaim = "";
let finishing = false;
let actionPending = false;
let actionVersion = 0;
let actionError = "";
let currentStatus = "";

function render(data) {
  if (data.status !== currentStatus && ["awaiting_button", "waiting_for_whatsapp", "claimed", "cancelled"].includes(data.status)) {
    actionError = "";
  }
  currentStatus = data.status;
  const waiting = data.status === "awaiting_button" || data.status === "waiting_for_whatsapp";
  const cancelling = data.status === "cancellation_pending";
  currentClaim = data.claim_id || "";
  claim.hidden = !waiting;
  cancel.hidden = !(waiting || cancelling) || !currentClaim;
  cancel.textContent = cancelling ? "Retry cancellation" : "Cancel connection";
  start.hidden = waiting || cancelling || data.status === "claimed";
  complete.hidden = data.status !== "claimed";
  if (waiting) {
    currentLink = data.whatsapp_url;
    link.href = currentLink;
    qr.src = `/api/cloud-claim/qr?t=${encodeURIComponent(data.expires_at)}`;
    expiry.textContent = `This link expires at ${new Date(data.expires_at * 1000).toLocaleTimeString()}.`;
  } else {
    currentLink = "";
    link.href = "/cloud-connect";
    qr.removeAttribute("src");
  }
  status.textContent = actionError || ({
    not_started: "",
    awaiting_button: "Open WhatsApp and send the message. Then press the button on your box once.",
    waiting_for_whatsapp: "Button press received. Send the message in WhatsApp to finish connecting.",
    claimed: "You're connected! Continue in WhatsApp to finish setup.",
    cancelled: "Connection cancelled. Get a new connection link when you're ready.",
    cancellation_pending: "Cancellation is not confirmed yet. Retry cancellation before starting a new connection.",
    expired: "The connection link expired. Get a new one to continue."
  }[data.status] ?? "Connection status is unavailable. Try again shortly.");
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
  if (actionPending || finishing) return;
  start.disabled = true;
  actionPending = true;
  actionVersion++;
  actionError = "";
  status.textContent = "Creating your connection link…";
  try {
    const response = await fetch("/api/cloud-claim/start", { method: "POST", cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error === "clock_not_ready" ? "clock_not_ready" : "unavailable");
    render(data);
  } catch (error) {
    actionError = error.message === "clock_not_ready"
      ? "Your box is setting its clock. Try again in a moment."
      : "Could not connect to WhatsApp. Try again in a moment.";
    status.textContent = actionError;
  } finally {
    actionPending = false;
    start.disabled = false;
  }
});
cancel.addEventListener("click", async () => {
  if (actionPending || finishing || !currentClaim) return;
  const claimId = currentClaim;
  cancel.disabled = true;
  actionPending = true;
  actionVersion++;
  actionError = "";
  render({ status: "cancellation_pending", claim_id: claimId });
  status.textContent = "Cancelling your connection link…";
  try {
    const response = await fetch("/api/cloud-claim/cancel", {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `claim_id=${encodeURIComponent(claimId)}`
    });
    if (!response.ok) throw new Error("unavailable");
    const data = await response.json();
    if (!["cancelled", "claimed"].includes(data.status)) throw new Error("unavailable");
    render(data);
    if (data.status === "claimed") {
      status.textContent = "This box connected before cancellation finished. Its connection has been kept.";
    }
  } catch {
    actionError = "Cancellation is not confirmed. Retry cancellation; the connection link has been kept for recovery.";
    status.textContent = actionError;
  } finally {
    actionPending = false;
    cancel.disabled = false;
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
  if (actionPending || finishing) return;
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
