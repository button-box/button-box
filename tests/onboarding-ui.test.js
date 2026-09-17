const { expect, test } = require("bun:test");
const fs = require("node:fs");
const vm = require("node:vm");

function harness(fail = false) {
  const nodes = new Map();
  const calls = [];
  const handlers = {};
  const element = () => ({
    hidden: false, disabled: false, textContent: "", children: [], dataset: {}, attributes: {}, handlers: {},
    addEventListener(name, fn) { this.handlers[name] = fn; },
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name] ?? null; },
    removeAttribute(name) { delete this.attributes[name]; },
    focus() { this.focused = true; },
    querySelector(selector) {
      for (const child of this.children) {
        if (selector.startsWith(".") && child.className === selector.slice(1)) return child;
        if (selector === 'button[type="submit"]' && child.type === "submit") return child;
        const match = child.querySelector?.(selector);
        if (match) return match;
      }
      return null;
    },
  });
  const node = (id) => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  const view = { status: "choose", mapped_count: 2, recipients: [] };
  const context = vm.createContext({
    document: { getElementById: node, querySelector: () => null, querySelectorAll: () => [], createElement: element },
    window: { addEventListener(name, fn) { handlers[name] = fn; }, clearTimeout() {}, setTimeout() { return 1; } },
    URLSearchParams, FormData: class {
      constructor(form) { this.form = form; }
      get(key) { return this.form[key]; }
    },
    fetch: async (url, options) => {
      if (url === "/api/state") return new Promise(() => {});
      calls.push({ url, options });
      return { ok: !fail, status: fail ? 409 : 200, headers: { get: () => "application/json" }, json: async () => fail ? { error: "Number not allowed" } : url === "/api/nfc" ? view : { recipients: [] } };
    },
  });
  vm.runInContext(fs.readFileSync(`${__dirname}/../messagebox/onboarding/static/app.js`, "utf8"), context);
  const form = node("nfc-allow-form");
  const submit = element();
  form.phone = "+15555550123";
  form.name = "Trusted person";
  form.querySelector = () => submit;
  form.reset = () => { form.phone = ""; form.name = ""; };
  return { context, node, calls, form, submit, handlers };
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

test("navigation refetches server state after setup-to-runtime handoff", async () => {
  const h = harness();
  vm.runInContext('let refreshed = false; loadState = async () => { refreshed = true; };', h.context);
  await h.handlers.hashchange();
  expect(vm.runInContext("refreshed", h.context)).toBe(true);
});

test("pairing submit exposes pending state and restores its control after success or rejection", async () => {
  const h = harness();
  const form = h.node("whatsapp-form");
  const button = h.node("start-whatsapp-pairing");
  const phone = h.node("whatsapp-phone");
  button.type = "submit";
  button.textContent = "Get pairing code";
  form.children = [button];
  phone.value = "+15555550123";

  const success = deferred();
  h.context.fetch = async (url, options) => {
    h.calls.push({ url, options });
    return success.promise;
  };
  const successRequest = form.handlers.submit({ preventDefault() {}, currentTarget: form });

  expect(button.disabled).toBe(true);
  expect(button.getAttribute("aria-busy")).toBe("true");
  expect(button.textContent).toBe("Working…");
  expect(h.calls.map(call => call.url)).toEqual(["/whatsapp/pair/start"]);

  success.resolve({
    ok: true,
    status: 200,
    headers: { get: () => "application/json" },
    json: async () => ({ mode: "SETUP", phase: "WHATSAPP_PENDING", whatsapp: { status: "starting" } }),
  });
  await successRequest;
  expect(button.disabled).toBe(false);
  expect(button.getAttribute("aria-busy")).toBe(null);
  expect(button.textContent).toBe("Get pairing code");

  const rejection = deferred();
  h.context.fetch = async (url, options) => {
    h.calls.push({ url, options });
    return rejection.promise;
  };
  const rejectedRequest = form.handlers.submit({ preventDefault() {}, currentTarget: form });
  expect(button.disabled).toBe(true);
  expect(button.getAttribute("aria-busy")).toBe("true");
  expect(button.textContent).toBe("Working…");

  rejection.resolve({
    ok: false,
    status: 409,
    headers: { get: () => "application/json" },
    json: async () => ({ error: "Pairing already in progress" }),
  });
  await rejectedRequest;
  expect(button.disabled).toBe(false);
  expect(button.getAttribute("aria-busy")).toBe(null);
  expect(button.textContent).toBe("Get pairing code");
  expect(h.node("page-error").textContent).toBe("Pairing already in progress");
  expect(phone.focused).toBe(true);
  expect(h.calls.map(call => call.url)).toEqual(["/whatsapp/pair/start", "/whatsapp/pair/start"]);
});

test("inline allow uses existing API without cancel, default change, or automatic assignment", async () => {
  const h = harness();
  await h.form.handlers.submit({ preventDefault() {}, currentTarget: h.form });
  expect(h.calls.map(c => c.url)).toEqual(["/recipients/add-number", "/api/nfc"]);
  expect(h.calls[0].options.body.get("phone")).toBe("+15555550123");
  expect(h.calls[0].options.body.get("name")).toBe("Trusted person");
  expect(h.node("nfc-choose-view").hidden).toBe(false);
  expect(h.node("nfc-allow-status").textContent).toContain("Choose it above");
  expect(h.submit.disabled).toBe(false);
});

test("polls preserve typed number and API validation failure is retryable inline", async () => {
  const h = harness(true);
  vm.runInContext('renderNfc({status:"choose", mapped_count:2, recipients:[]})', h.context);
  expect(h.form.phone).toBe("+15555550123");
  expect(h.form.name).toBe("Trusted person");
  await h.form.handlers.submit({ preventDefault() {}, currentTarget: h.form });
  expect(h.form.phone).toBe("+15555550123");
  expect(h.form.name).toBe("Trusted person");
  expect(h.node("nfc-allow-status").textContent).toBe("Number not allowed");
  expect(h.submit.disabled).toBe(false);
  expect(h.calls).toHaveLength(1);
});

test("recently played renders safe state and queues once from an explicit click", async () => {
  const h = harness();
  vm.runInContext("loadActivity = async () => {};", h.context);
  const list = vm.runInContext(`activityMessageList([{
    sender: "Family member", chat: "Family group", ts: 1000, dur: 3,
    media_kind: "video_soundtrack", available: true, queued: false, token: "opaque-token"
  }], "played")`, h.context);
  const row = list.children[0];
  const title = row.children[0].children[0];
  const audio = row.children[1];
  const button = row.children[2].children[0];

  expect(title.textContent).toBe("Video soundtrack · Family member · Family group");
  expect(audio.src).toBe("/audio/opaque-token?played=1");
  expect(button.textContent).toBe("Add to queue");
  await button.handlers.click();
  expect(button.disabled).toBe(true);
  expect(h.calls.at(-1).url).toBe("/api/requeue?f=opaque-token");
  expect(h.calls.at(-1).options.method).toBe("POST");
});

test("recently played disables missing and already queued media", () => {
  const h = harness();
  const list = vm.runInContext(`activityMessageList([
    {sender:"A", chat:"Group", ts:1000, dur:1, media_kind:"voice_message", available:false, queued:false, token:"missing"},
    {sender:"B", chat:"Group", ts:1001, dur:1, media_kind:"voice_message", available:true, queued:true, token:"queued"}
  ], "played")`, h.context);

  expect(list.children[0].children[1].src).toBe(undefined);
  expect(list.children[0].children[2].children[0].textContent).toBe("Unavailable");
  expect(list.children[0].children[2].children[0].disabled).toBe(true);
  expect(list.children[1].children[2].children[0].textContent).toBe("In queue");
  expect(list.children[1].children[2].children[0].disabled).toBe(true);
});

test("recipient rename submits the opaque token and new display name", async () => {
  const h = harness();
  h.calls.length = 0;
  vm.runInContext(`
    currentState = {mode: "SETUP"};
    renderRecipientManager({recipients: [{
      token: "recipient-token-0001", label: "Trusted person",
      secondary_label: "+15555550123", metadata_status: "ready",
      configured: true, available: true, is_default: true,
      kind: "person", card_count: 1
    }]});
  `, h.context);
  const row = h.node("configured-recipient-list").children[0];
  const rename = row.children[1].children.find(child => child.textContent === "Rename");
  rename.handlers.click();
  const form = row.querySelector(".recipient-rename");
  const input = form.children[0].children[0];
  input.value = "Renamed person";

  await form.handlers.submit({ preventDefault() {} });

  expect(h.calls).toHaveLength(1);
  expect(h.calls[0].url).toBe("/recipients/rename");
  expect(h.calls[0].options.body.get("token")).toBe("recipient-token-0001");
  expect(h.calls[0].options.body.get("name")).toBe("Renamed person");
  expect(h.node("manager-status").textContent).toBe("Name saved.");
});
