const { expect, test } = require("bun:test");
const fs = require("node:fs");
const vm = require("node:vm");

function harness(fail = false) {
  const nodes = new Map();
  const calls = [];
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
    window: { addEventListener() {}, clearTimeout() {}, setTimeout() { return 1; } },
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
  return { context, node, calls, form, submit };
}

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
