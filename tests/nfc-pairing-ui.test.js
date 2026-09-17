const { test, expect } = require("bun:test");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(`${__dirname}/../messagebox/onboarding/static/app.js`, "utf8");
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end));
const tick = () => new Promise(resolve => setImmediate(resolve));
function harness() {
  const nodes = new Map(); const calls = []; const timers = [];
  function element() { return {children: [], dataset: {}, handlers: {}, textContent: "", disabled: false,
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute() {}, focus() { this.focused = true; },
    addEventListener(name, fn) { this.handlers[name] = fn; },
    querySelector(selector) { for (const c of this.children) { if (`.${c.className}` === selector) return c; const match = c.querySelector?.(selector); if (match) return match; } return null; },
  }; }
  const node = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  const recipient = {token:"person-a", label:"Grandma", configured:true, available:true, is_default:true, kind:"person", card_count:1};
  const h = {node, calls, timers, response:{status:"waiting",healthy:true}, enroll: async () => ({attempt:"attempt-a"})};
  const context = vm.createContext({ document:{getElementById:node,createElement:element},
    window:{clearTimeout(){},setTimeout(fn){timers.push(fn);return timers.length;}}, encodeURIComponent,
    request: async url => {calls.push(url);return url === "/api/recipients" ? {recipients:[recipient]} : h.response;},
    formRequest: async (url,payload) => {calls.push({url,payload});return url === "/nfc/enroll" ? h.enroll() : {};},
  });
  vm.runInContext('let runtimePairing=null, runtimePairingGeneration=0, nfcPollTimer=null, currentState={mode:"RUNTIME"}, recipientsData='+JSON.stringify({recipients:[recipient]})+';', context);
  vm.runInContext(section("function acceptanceControl", "function showView") + section("function recipientRow", "function renderRecipientPicker") + section("function renderRecipientManager", "async function mutateRecipient") + section("function setRuntimePairingMessage", "function scheduleNfcPoll"),context);
  h.run = code => vm.runInContext(code,context);
  h.start = () => h.run('beginRuntimeNfc("person-a", "Grandma", {})');
  h.status = () => node("configured-recipient-list").querySelector(".card-pairing-message")?.textContent;
  return h;
}
test("Pair card immediately shows local feedback, then scan instruction and inline cancel", async () => {
  const h=harness(); let resolve; h.enroll=()=>new Promise(r=>{resolve=r;});
  const started=h.start();
  expect(h.status()).toContain("Starting card pairing");
  expect(h.node("configured-recipient-list").querySelector(".card-pairing-status").focused).toBe(true);
  resolve({attempt:"attempt-a"}); await started; await tick();
  expect(h.status()).toContain("Hold a card");
  const panel=h.node("configured-recipient-list").querySelector(".card-pairing-status");
  const cancel=panel.children.find(c=>c.textContent==="Cancel pairing");
  await cancel.handlers.click();
  expect(h.calls.some(c=>c.url==="/nfc/cancel-runtime")).toBe(true);
  expect(h.status()).toBe("Card pairing cancelled.");
});
test("idle is not success and failed requests make retry available", async () => {
  const h=harness(); h.response={status:"idle"}; await h.start(); await tick();
  expect(h.status()).toContain("without confirmation");
  expect(h.run("runtimePairing.pending")).toBe(false);
  h.enroll=async()=>{throw Error("Reader unavailable");}; await h.start();
  expect(h.status()).toBe("Reader unavailable");
  expect(h.run("runtimePairing.pending")).toBe(false);
});
test("only the matching receipt confirms success; duplicate clicks do not enroll twice", async () => {
  const h=harness(); let resolve; h.enroll=()=>new Promise(r=>{resolve=r;});
  const first=h.start(); await h.start();
  expect(h.calls.filter(c=>c.url==="/nfc/enroll")).toHaveLength(1);
  h.response={status:"success"}; resolve({attempt:"attempt-a"}); await first; await tick();
  expect(h.calls).toContain("/api/nfc-runtime?attempt=attempt-a");
  expect(h.status()).toBe("Card linked to Grandma ✓");
});
test("late poll cannot replace a cancelled attempt", async () => {
  const h=harness(); let resolve; h.response=new Promise(r=>{resolve=r;});
  await h.start(); await h.run('runtimeNfcAction("/nfc/cancel-runtime")');
  resolve({status:"success"}); await tick();
  expect(h.status()).toBe("Card pairing cancelled.");
});
test("returning to recipient manager resumes a pending attempt", async () => {
  const h=harness(); await h.start(); await tick();
  const before=h.timers.length; await h.run('loadRecipients({manager:true})');
  expect(h.timers.length).toBeGreaterThan(before);
  expect(h.status()).toContain("Hold a card");
});
