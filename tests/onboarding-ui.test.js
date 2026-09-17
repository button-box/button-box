const { expect, test } = require("bun:test");
const fs = require("node:fs");
const vm = require("node:vm");

function harness(fail = false) {
  const nodes = new Map();
  const calls = [];
  const handlers = {};
  const element = () => ({
    hidden: false, disabled: false, textContent: "", children: [], dataset: {}, handlers: {},
    addEventListener(name, fn) { this.handlers[name] = fn; },
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; }, focus() {},
  });
  const node = (id) => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  const view = { status: "choose", mapped_count: 2, recipients: [] };
  const context = vm.createContext({
    document: { getElementById: node, querySelector: () => null, querySelectorAll: () => [], createElement: element },
    window: { addEventListener(name, fn) { handlers[name] = fn; }, clearTimeout() {}, setTimeout() { return 1; } },
    URLSearchParams, FormData: class { constructor(form) { this.form = form; } get() { return this.form.phone; } },
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
  form.querySelector = () => submit;
  form.reset = () => { form.phone = ""; };
  return { context, node, calls, form, submit, handlers };
}

test("navigation refetches server state after setup-to-runtime handoff", async () => {
  const h = harness();
  vm.runInContext('let refreshed = false; loadState = async () => { refreshed = true; };', h.context);
  await h.handlers.hashchange();
  expect(vm.runInContext("refreshed", h.context)).toBe(true);
});

test("inline allow uses existing API without cancel, default change, or automatic assignment", async () => {
  const h = harness();
  await h.form.handlers.submit({ preventDefault() {}, currentTarget: h.form });
  expect(h.calls.map(c => c.url)).toEqual(["/recipients/add-number", "/api/nfc"]);
  expect(h.calls[0].options.body.get("phone")).toBe("+15555550123");
  expect(h.node("nfc-choose-view").hidden).toBe(false);
  expect(h.node("nfc-allow-status").textContent).toContain("Choose it above");
  expect(h.submit.disabled).toBe(false);
});

test("polls preserve typed number and API validation failure is retryable inline", async () => {
  const h = harness(true);
  vm.runInContext('renderNfc({status:"choose", mapped_count:2, recipients:[]})', h.context);
  expect(h.form.phone).toBe("+15555550123");
  await h.form.handlers.submit({ preventDefault() {}, currentTarget: h.form });
  expect(h.form.phone).toBe("+15555550123");
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
