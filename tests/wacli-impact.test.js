import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";

// Execute the exact metadata-only script with injected GitHub API boundaries.
const workflow = readFileSync(new URL("../.github/workflows/wacli-impact.yml", import.meta.url), "utf8");
const script = workflow.split("          script: |\n")[1]
  .split("\n").map(line => line.replace(/^ {12}/, "")).join("\n");
const run = new (Object.getPrototypeOf(async function () {}).constructor)("github", "context", "core", script);

async function check(files, body, labelExists = true) {
  const calls = { failures: [], labels: [], created: [], fetched: 0 };
  const github = {
    paginate: async () => files,
    rest: {
      pulls: {
        listFiles() {},
        get: async () => { calls.fetched++; return { data: { body } }; },
      },
      issues: {
        getLabel: async () => { if (!labelExists) throw { status: 404 }; },
        createLabel: async data => calls.created.push(data.name),
        addLabels: async data => calls.labels.push(...data.labels),
      },
    },
  };
  await run(github, { repo: { owner: "example", repo: "example" }, payload: { pull_request: { number: 1 } } },
    { setFailed: message => calls.failures.push(message) });
  return calls;
}

test("all documented shared areas require impact and receive the label", async () => {
  for (const filename of ["messagebox/button_send.py", "messagebox/contacts.py",
    "messagebox/listened_receipts.py", "messagebox/settings.py", "messagebox/nfc.py",
    "messagebox/onboarding/app.py", "messagebox/onboarding/static/home.js",
    "messagebox/sound_pack.py", "messagebox/acoustic_cue.py", "messagebox/ringtones.py",
    "sounds/voice/voice-listened.wav", "scripts/setup.sh", "scripts/provision.sh", "scripts/install/audio_config.py"]) {
    const result = await check([{ filename }], null);
    expect(result.failures).toHaveLength(1);
    expect(result.labels).toEqual(["wacli-impact"]);
  }
});

test("Cloud-only code and unrelated files do not require impact or labels", async () => {
  const result = await check([{ filename: "messagebox/cloud_runtime.py" }, { filename: "README.md" }], null);
  expect(result.failures).toEqual([]);
  expect(result.labels).toEqual([]);
  expect(result.fetched).toBe(0);
});

test("Markdown heading or bold section is accepted case-insensitively", async () => {
  for (const heading of ["## wacli impact", "### Wacli Impact ###", "**wacli impact**", "**Wacli impact:**", "**wacli impact**:"]) {
    const result = await check([{ filename: "messagebox/settings.py" }], `${heading}\n\nNone; contracts passed.`);
    expect(result.failures).toEqual([]);
    expect(result.labels).toEqual(["wacli-impact"]);
  }
});

test("mentions, comments and fenced examples do not count as a section", async () => {
  for (const body of ["This has wacli impact", "<!--\n## wacli impact\n-->", "```markdown\n## wacli impact\n```", "~~~\n## wacli impact\n~~~"]) {
    expect((await check([{ filename: "messagebox/settings.py" }], body)).failures).toHaveLength(1);
  }
});

test("renames out of a shared area still require impact", async () => {
  const result = await check([{ filename: "messagebox/replacement.py", previous_filename: "messagebox/contacts.py" }], "No section");
  expect(result.failures).toHaveLength(1);
  expect(result.labels).toEqual(["wacli-impact"]);
});

test("creates a missing label with the scoped default token", async () => {
  const result = await check([{ filename: "messagebox/settings.py" }], "## wacli impact\nNone", false);
  expect(result.created).toEqual(["wacli-impact"]);
  expect(result.labels).toEqual(["wacli-impact"]);
});

test("API file cap fails closed", async () => {
  const result = await check(Array.from({ length: 3000 }, () => ({ filename: "README.md" })), "## wacli impact");
  expect(result.failures).toHaveLength(1);
  expect(result.fetched).toBe(0);
});
