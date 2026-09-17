const { expect, test } = require("bun:test");
const fs = require("node:fs");
const vm = require("node:vm");

function harness() {
  const nodes = new Map();
  const calls = [];
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, {
      textContent: "", hidden: false, disabled: false, handlers: {},
      parentElement: { classList: { toggle() {} } },
      addEventListener(name, fn) { this.handlers[name] = fn; },
      focus() { this.focused = true; },
    });
    return nodes.get(id);
  };
  const h = { node, calls, fail: false, copied: null, scrolls: 0 };
  const context = vm.createContext({
    document: { getElementById: node, querySelector: () => node("heading"), querySelectorAll: () => [] },
    window: {
      clearTimeout() {}, setTimeout() {}, addEventListener() {},
      scrollTo() { h.scrolls++; }, isSecureContext: false,
      ButtonBoxClipboard: { async copyText(text) { if (h.fail) throw Error("denied"); h.copied = text; } },
    },
    navigator: {}, location: { hash: "#home" },
    fetch: async (url, options) => {
      if (url === "/api/state") return new Promise(() => {});
      calls.push({ url, options });
      return { ok: !h.fail, status: h.fail ? 503 : 202,
        headers: { get: () => "application/json" },
        json: async () => h.fail ? { error: "Box unavailable" } : { queued: true } };
    },
  });
  vm.runInContext(fs.readFileSync(`${__dirname}/../messagebox/onboarding/static/app.js`, "utf8"), context);
  h.run = (code) => vm.runInContext(code, context);
  return h;
}

test("Home never treats a linked account alone as completed first-message setup", () => {
  const h = harness();
  h.run('renderHome({mode:"RUNTIME",health:{runtime:"running"},setup:{wifi:"complete",whatsapp:"complete",recipient:"complete",first_message:"attention"}})');
  expect(h.node("home-attention").hidden).toBe(false);
  expect(h.node("home-runtime").textContent).toBe("Needs attention");
  h.run('renderHome({mode:"RUNTIME",health:{runtime:"running"},setup:{wifi:"complete",whatsapp:"complete",recipient:"complete",first_message:"complete"}})');
  expect(h.node("home-attention").hidden).toBe(true);
  expect(h.node("home-summary").textContent).toContain("Connected and set up");
});

test("Home reports runtime attention when the button service is down", () => {
  const h = harness();
  h.run('renderHome({mode:"RUNTIME",health:{runtime:"attention"},setup:{wifi:"complete",whatsapp:"complete",recipient:"complete",first_message:"complete"}})');
  expect(h.node("home-attention").hidden).toBe(false);
  expect(h.node("home-runtime").textContent).toBe("Needs attention");
  expect(h.node("home-summary").textContent).not.toContain("Connected and set up");
});

test("Footer never copies missing or invalid identity; reassignment clears stale feedback", async () => {
  const h = harness();
  for (const id of [null, "hostname", "BOX-0", "<script>", 42]) {
    h.run(`renderIdentity({box_id:${JSON.stringify(id)}})`);
    expect(h.node("box-id").textContent).toBe("Not assigned");
    await h.node("copy-box-id").handlers.click();
    expect(h.copied).toBe(null);
  }
  h.run('renderIdentity({box_id:"BOX-42"})');
  await h.node("copy-box-id").handlers.click();
  expect(h.copied).toBe("BOX-42");
  expect(h.node("box-id-status").textContent).toBe("Box ID copied.");
  h.run('renderIdentity({box_id:null})');
  expect(h.node("copy-box-id").disabled).toBe(true);
  expect(h.node("box-id-status").textContent).toBe("");
});

test("Copy failure leaves selectable ID and manual-copy guidance", async () => {
  const h = harness(); h.fail = true;
  h.run('renderIdentity({box_id:"BOX-42"})');
  await h.node("copy-box-id").handlers.click();
  expect(h.node("box-id").textContent).toBe("BOX-42");
  expect(h.node("box-id-status").textContent).toContain("manually");
});

test("Ringtone is runtime-only, reports requested not played, and surfaces failure", async () => {
  const h = harness();
  h.run('currentState = {mode:"SETUP"}');
  await h.node("ring-now").handlers.click();
  expect(h.calls).toHaveLength(0);
  h.run('currentState = {mode:"RUNTIME"}');
  await h.node("ring-now").handlers.click();
  expect(h.calls[0]).toEqual({ url: "/api/ring", options: { cache: "no-store", method: "POST" } });
  expect(h.node("ring-now-status").textContent).toContain("requested");
  h.fail = true;
  await h.node("ring-now").handlers.click();
  expect(h.node("ring-now-status").textContent).toBe("Box unavailable");
});

test("View changes reset scroll once, polling preserves it; skip link does not change route", () => {
  const h = harness();
  h.run('showView("home"); showView("home"); showView("setup")');
  expect(h.scrolls).toBe(2);
  let prevented = false;
  h.node("skip-link").handlers.click({ preventDefault() { prevented = true; } });
  expect(prevented).toBe(true);
  expect(h.node("main").focused).toBe(true);
});
