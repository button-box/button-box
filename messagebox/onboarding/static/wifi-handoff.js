"use strict";

// Everything is embedded before the radio switch; a lost request leaves the next step visible.
async function checkWifiHandoff() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch("/api/state", {
        cache: "no-store", signal: AbortSignal.timeout(3000),
      });
      if (response.ok) {
        const state = await response.json();
        if (state.transport === "cloud" && ["WHATSAPP_PENDING", "WHATSAPP_READY"].includes(state.phase)) {
          document.getElementById("handoff-title").textContent = "✓ Connected!";
          document.getElementById("handoff-status").textContent = "You can close this page.";
          document.getElementById("handoff-next").textContent = "Now scan the QR code on the card to finish in WhatsApp.";
          document.querySelector(".pulse").hidden = true;
          return;
        }
        if (state.phase === "WIFI_FAILED") {
          document.getElementById("handoff-title").textContent = "Could not connect";
          document.getElementById("handoff-status").textContent = "Rejoin the setup hotspot and try again.";
          document.getElementById("handoff-next").textContent = "Finish in WhatsApp after the box connects to your Wi-Fi.";
          document.querySelector(".pulse").hidden = true;
          return;
        }
      }
    } catch {
      // Disconnection is expected on the single radio. It is not proof of success.
    }
    await new Promise(resolve => window.setTimeout(resolve, 1000));
  }
}

checkWifiHandoff();
