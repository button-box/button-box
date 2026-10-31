"use strict";

const form = document.getElementById("cloud-wifi-form");
const password = document.getElementById("cloud-wifi-password");
const openNetwork = document.getElementById("cloud-wifi-open");
const submit = document.getElementById("cloud-wifi-submit");
const status = document.getElementById("cloud-wifi-status");
let pending = false;

openNetwork.addEventListener("change", () => {
  password.disabled = openNetwork.checked;
  password.required = !openNetwork.checked;
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (pending) return;
  pending = true;
  submit.disabled = true;
  status.textContent = "Connecting to your Wi-Fi…";
  try {
    const response = await fetch("/api/wifi-change", {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ssid: document.getElementById("cloud-wifi-name").value,
        password: openNetwork.checked ? "" : password.value,
        security: openNetwork.checked ? "open" : "protected",
      }),
    });
    if (!response.ok) throw new Error("unavailable");
    status.textContent = "Wi-Fi change started. Join the same Wi-Fi on your phone to continue.";
  } catch {
    status.textContent = "Could not confirm the Wi-Fi change. Check the box's connection before trying again.";
  } finally {
    pending = false;
    submit.disabled = false;
  }
});
