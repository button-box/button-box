const { expect, test } = require("bun:test");
const fs = require("node:fs");
const vm = require("node:vm");

function harness() {
  const nodes = new Map();
  const requests = [];
  let poll;
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      hidden: false, disabled: false, textContent: "", handlers: {},
      addEventListener(name, fn) { this.handlers[name] = fn; },
    });
    return nodes.get(id);
  };
  vm.runInNewContext(fs.readFileSync(`${__dirname}/../messagebox/onboarding/static/cloud-connect.js`, "utf8"), {
    document: { getElementById: node },
    window: { setInterval(fn) { poll = fn; }, setTimeout() {} },
    fetch: (url, options) => new Promise(resolve => requests.push({ url, options, resolve })),
  });
  const respond = async (request, data, ok = true) => {
    request.resolve({ ok, json: async () => data });
    await new Promise(setImmediate);
  };
  return {
    node, requests, respond,
    async poll(data, ok = true) {
      const pending = poll();
      await respond(requests.at(-1), data, ok);
      await pending;
    },
  };
}

const waiting = {
  status: "awaiting_button",
  whatsapp_url: "https://wa.me/15555550123?text=claim%20synthetic",
  expires_at: 1_800_000_600,
};

test("failed connection start stays visible across unchanged and failed polls, then clears on retry", async () => {
  const h = harness();
  await h.respond(h.requests[0], { status: "not_started" });
  const start = h.node("cloud-start");
  const attempt = start.handlers.click();
  expect(start.disabled).toBe(true);
  await h.respond(h.requests.at(-1), {}, false);
  await attempt;
  const failure = h.node("cloud-status").textContent;
  expect(failure).toBe("Could not start a connection link. Try again shortly.");
  expect(start.disabled).toBe(false);

  await h.poll({ status: "not_started" });
  expect(h.node("cloud-status").textContent).toBe(failure);
  await h.poll({}, false);
  expect(h.node("cloud-status").textContent).toBe(failure);

  const retry = start.handlers.click();
  expect(h.node("cloud-status").textContent).toBe("Creating your connection link…");
  await h.respond(h.requests.at(-1), waiting);
  await retry;
  expect(h.node("cloud-status").textContent).toContain("press the physical box button once");
  expect(h.node("cloud-claim").hidden).toBe(false);
  expect(h.node("cloud-link").href).toBe(waiting.whatsapp_url);
});

test("a later confirmed claim state clears an uncertain start failure without a second start", async () => {
  const h = harness();
  await h.respond(h.requests[0], { status: "not_started" });
  const attempt = h.node("cloud-start").handlers.click();
  await h.respond(h.requests.at(-1), {}, false);
  await attempt;
  await h.poll(waiting);
  expect(h.node("cloud-status").textContent).toContain("press the physical box button once");
  expect(h.requests.filter(request => request.url === "/api/cloud-claim/start")).toHaveLength(1);
});

test("a poll already in flight cannot replace a successful connection start", async () => {
  const h = harness();
  const oldPoll = h.requests[0];
  const attempt = h.node("cloud-start").handlers.click();
  await h.respond(h.requests.at(-1), waiting);
  await attempt;
  await h.respond(oldPoll, { status: "not_started" });
  expect(h.node("cloud-status").textContent).toContain("press the physical box button once");
  expect(h.node("cloud-start").hidden).toBe(true);
  expect(h.node("cloud-claim").hidden).toBe(false);
});

test("failed completion remains visible while the box stays claimed and can be retried", async () => {
  const h = harness();
  await h.respond(h.requests[0], { status: "claimed" });
  const complete = h.node("cloud-complete");
  const attempt = complete.handlers.click();
  await h.respond(h.requests.at(-1), {}, false);
  await attempt;
  await h.poll({ status: "claimed" });
  expect(h.node("cloud-status").textContent).toBe("Could not finish setup. Try again shortly.");
  expect(complete.disabled).toBe(false);
  const retry = complete.handlers.click();
  await h.respond(h.requests.at(-1), { status: "complete" });
  await retry;
  expect(h.node("cloud-status").textContent).toContain("Box setup is finishing");
});
