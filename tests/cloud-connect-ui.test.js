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
      removeAttribute(name) { delete this[name]; },
      addEventListener(name, fn) { this.handlers[name] = fn; },
    });
    return nodes.get(id);
  };
  vm.runInNewContext(fs.readFileSync(`${__dirname}/../messagebox/onboarding/static/cloud-connect.js`, "utf8"), {
    document: { getElementById: node },
    window: { setInterval(fn) { poll = fn; } },
    fetch: (url, options) => new Promise(resolve => requests.push({ url, options, resolve })),
  });
  const respond = async (request, data, ok = true) => {
    request.resolve({ ok, json: async () => data });
    await new Promise(setImmediate);
  };
  return {
    node, requests, respond,
    beginPoll: () => poll(),
    async poll(data, ok = true) {
      const pending = poll();
      await respond(requests.at(-1), data, ok);
      await pending;
    },
  };
}

const waiting = {
  status: "awaiting_button",
  claim_id: "synthetic-claim-001",
  whatsapp_url: "https://wa.me/15555550123?text=claim%20synthetic",
  expires_at: 1_800_000_600,
};

test("failed connection start stays visible across unchanged and failed polls, then clears on retry", async () => {
  const h = harness();
  await h.respond(h.requests[0], { status: "not_started" });
  expect(h.node("cloud-status").textContent).toBe("");
  const start = h.node("cloud-start");
  const attempt = start.handlers.click();
  expect(start.disabled).toBe(true);
  await h.respond(h.requests.at(-1), {}, false);
  await attempt;
  const failure = h.node("cloud-status").textContent;
  expect(failure).toBe("Could not connect to WhatsApp. Try again in a moment.");
  expect(start.disabled).toBe(false);

  await h.poll({ status: "not_started" });
  expect(h.node("cloud-status").textContent).toBe(failure);
  await h.poll({}, false);
  expect(h.node("cloud-status").textContent).toBe(failure);

  const retry = start.handlers.click();
  expect(h.node("cloud-status").textContent).toBe("Creating your connection link…");
  await h.respond(h.requests.at(-1), waiting);
  await retry;
  expect(h.node("cloud-status").textContent).toContain("press the button on your box once");
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
  expect(h.node("cloud-status").textContent).toContain("press the button on your box once");
  expect(h.requests.filter(request => request.url === "/api/cloud-claim/start")).toHaveLength(1);
});

test("clock correction explains the wait and lets the owner retry without losing the expiry guard", async () => {
  const h = harness();
  await h.respond(h.requests[0], { status: "not_started" });
  const start = h.node("cloud-start");
  const attempt = start.handlers.click();
  await h.respond(h.requests.at(-1), { error: "clock_not_ready" }, false);
  await attempt;
  expect(h.node("cloud-status").textContent).toBe("Your box is setting its clock. Try again in a moment.");
  expect(start.disabled).toBe(false);
  await h.poll({ status: "not_started" });
  expect(h.node("cloud-status").textContent).toContain("setting its clock");
  const retry = start.handlers.click();
  await h.respond(h.requests.at(-1), waiting);
  await retry;
  expect(h.node("cloud-link").href).toBe(waiting.whatsapp_url);
  expect(h.node("cloud-status").textContent).toContain("Open WhatsApp");
});

test("a poll already in flight cannot replace a successful connection start", async () => {
  const h = harness();
  const oldPoll = h.requests[0];
  const attempt = h.node("cloud-start").handlers.click();
  await h.respond(h.requests.at(-1), waiting);
  await attempt;
  await h.respond(oldPoll, { status: "not_started" });
  expect(h.node("cloud-status").textContent).toContain("press the button on your box once");
  expect(h.node("cloud-start").hidden).toBe(true);
  expect(h.node("cloud-claim").hidden).toBe(false);
});

test("claimed shows the ready message without a completion request", async () => {
  const h = harness();
  await h.respond(h.requests[0], { status: "claimed" });
  await h.poll({ status: "claimed" });
  expect(h.node("cloud-status").textContent).toBe("Your box is ready. Go back to WhatsApp.");
  expect(h.node("cloud-start").hidden).toBe(true);
  expect(h.node("cloud-claim").hidden).toBe(true);
  expect(h.node("cloud-title").textContent).toBe("All set!");
  expect(h.node("cloud-intro").hidden).toBe(true);
  expect(h.requests.some(request => request.url === "/onboarding/complete")).toBe(false);
  const page = fs.readFileSync(`${__dirname}/../messagebox/onboarding/static/cloud-connect.html`, "utf8");
  expect(page.includes('id="cloud-complete"')).toBe(false);
});

test("cancel hides the link, binds the exact claim and rejects an old pending poll", async () => {
  const h = harness();
  await h.respond(h.requests[0], waiting);
  expect(h.node("cloud-cancel").hidden).toBe(false);
  const oldPoll = h.beginPoll();
  const pollRequest = h.requests.at(-1);
  const attempt = h.node("cloud-cancel").handlers.click();
  const request = h.requests.at(-1);
  expect(request.url).toBe("/api/cloud-claim/cancel");
  expect(request.options.body).toBe("claim_id=synthetic-claim-001");
  expect(h.node("cloud-cancel").disabled).toBe(true);
  expect(h.node("cloud-claim").hidden).toBe(true);
  expect(h.node("cloud-qr").src).toBeUndefined();
  await h.respond(request, { status: "cancelled" });
  await attempt;
  await h.respond(pollRequest, waiting);
  await oldPoll;
  expect(h.node("cloud-cancel").hidden).toBe(true);
  expect(h.node("cloud-start").hidden).toBe(false);
  expect(h.node("cloud-status").textContent).toContain("Connection cancelled");
});

test("uncertain cancellation stays retryable after status readback and does not start another claim", async () => {
  const h = harness();
  await h.respond(h.requests[0], { ...waiting, status: "waiting_for_whatsapp" });
  const attempt = h.node("cloud-cancel").handlers.click();
  const count = h.requests.length;
  await h.node("cloud-start").handlers.click();
  expect(h.requests.length).toBe(count);
  await h.respond(h.requests.at(-1), {}, false);
  await attempt;
  const error = h.node("cloud-status").textContent;
  await h.poll({ status: "cancellation_pending", claim_id: waiting.claim_id });
  expect(h.node("cloud-status").textContent).toBe(error);
  expect(h.node("cloud-start").hidden).toBe(true);
  expect(h.node("cloud-claim").hidden).toBe(true);
  expect(h.node("cloud-cancel").textContent).toBe("Retry cancellation");
  const retry = h.node("cloud-cancel").handlers.click();
  expect(h.requests.at(-1).options.body).toBe("claim_id=synthetic-claim-001");
  await h.respond(h.requests.at(-1), { status: "claimed" });
  await retry;
  expect(h.node("cloud-status").textContent).toContain("connected before cancellation finished");
  expect(h.node("cloud-cancel").hidden).toBe(true);
  expect(h.requests.some(request => request.url === "/onboarding/complete")).toBe(false);
});
