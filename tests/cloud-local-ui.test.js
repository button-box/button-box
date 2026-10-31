const { expect, test } = require("bun:test");
const fs = require("node:fs");
const vm = require("node:vm");

function harness() {
  const nodes = new Map();
  const calls = [];
  let respond;
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      value: "", checked: false, disabled: false, required: true, handlers: {},
      addEventListener(name, fn) { this.handlers[name] = fn; },
    });
    return nodes.get(id);
  };
  vm.runInNewContext(fs.readFileSync(`${__dirname}/../messagebox/onboarding/static/cloud-local.js`, "utf8"), {
    document: { getElementById: node },
    fetch: (url, options) => {
      calls.push({url, options});
      return new Promise(resolve => { respond = resolve; });
    },
  });
  return { node, calls, respond: value => respond(value), submit: () => node("cloud-wifi-form").handlers.submit({preventDefault() {}}) };
}

test("Cloud local Wi-Fi change uses the existing route and rejects a second pending submit", async () => {
  const h = harness();
  h.node("cloud-wifi-name").value = "Synthetic home";
  h.node("cloud-wifi-password").value = "synthetic-password";
  const first = h.submit();
  await h.submit();
  expect(h.calls).toHaveLength(1);
  expect(h.calls[0].url).toBe("/api/wifi-change");
  expect(JSON.parse(h.calls[0].options.body)).toEqual({ssid:"Synthetic home", password:"synthetic-password", security:"protected"});
  h.respond({ok:true});
  await first;
  expect(h.node("cloud-wifi-status").textContent).toContain("Join the same Wi-Fi");
});

test("open Wi-Fi omits a retained password and an unconfirmed change stays recoverable", async () => {
  const h = harness();
  h.node("cloud-wifi-password").value = "synthetic-password";
  h.node("cloud-wifi-open").checked = true;
  h.node("cloud-wifi-open").handlers.change();
  expect(h.node("cloud-wifi-password").required).toBe(false);
  const pending = h.submit();
  expect(JSON.parse(h.calls[0].options.body)).toEqual({ssid:"", password:"", security:"open"});
  h.respond({ok:false});
  await pending;
  expect(h.node("cloud-wifi-submit").disabled).toBe(false);
  expect(h.node("cloud-wifi-status").textContent).toContain("Could not confirm");
});
