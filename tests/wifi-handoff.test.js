const { expect, test } = require("bun:test");
const fs = require("node:fs");
const vm = require("node:vm");

async function handoff(states) {
  const nodes = {
    "handoff-title": { textContent: "Connecting…" },
    "handoff-status": { textContent: "Your box is joining your Wi-Fi. This page may lose its connection." },
    "handoff-next": { textContent: "When the box says it is connected, you can close this page. Now scan the QR code on the card to finish in WhatsApp." },
    pulse: { hidden: false },
  };
  let calls = 0;
  const context = vm.createContext({
    document: { getElementById: id => nodes[id], querySelector: () => nodes.pulse },
    window: { setTimeout: fn => fn() },
    AbortSignal,
    fetch: async (url, options) => {
      expect(url).toBe("/api/state");
      expect(options.cache).toBe("no-store");
      expect(options.signal).toBeInstanceOf(AbortSignal);
      const state = states[calls++];
      if (!state) throw new Error("hotspot lost");
      return { ok: !state.httpError, json: async () => state };
    },
  });
  const script = fs.readFileSync(`${__dirname}/../messagebox/onboarding/static/wifi-handoff.js`, "utf8");
  await vm.runInContext(script, context);
  return { nodes, calls };
}

test("verified Cloud Wi-Fi proof shows success and retains it after the hotspot drops", async () => {
  const { nodes, calls } = await handoff([
    { transport: "cloud", phase: "WIFI_CONNECTING" },
    { transport: "cloud", phase: "WIFI_ASSOCIATED" },
    { transport: "cloud", phase: "WHATSAPP_PENDING" },
  ]);
  expect(nodes["handoff-title"].textContent).toBe("✓ Connected!");
  expect(nodes["handoff-status"].textContent).toBe("You can close this page.");
  expect(nodes["handoff-next"].textContent).toBe("Now scan the QR code on the card to finish in WhatsApp.");
  expect(nodes.pulse.hidden).toBe(true);
  expect(calls).toBe(3); // No later request can overwrite success.
});

test("disconnect, HTTP error, or wacli phase never claims Cloud success", async () => {
  const { nodes, calls } = await handoff([
    { transport: "wacli", phase: "WHATSAPP_PENDING" },
    { httpError: true },
  ]);
  expect(nodes["handoff-title"].textContent).toBe("Connecting…");
  expect(nodes["handoff-next"].textContent).toContain("scan the QR code");
  expect(calls).toBe(60);
});

test("failed association gives a retry handoff without success", async () => {
  const { nodes, calls } = await handoff([{ transport: "cloud", phase: "WIFI_FAILED" }]);
  expect(nodes["handoff-title"].textContent).toBe("Could not connect");
  expect(nodes["handoff-status"].textContent).toContain("try again");
  expect(calls).toBe(1);
});
