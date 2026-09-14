const { describe, expect, test } = require("bun:test");
const { copyText } = require("../messagebox/onboarding/static/clipboard.js");

describe("copyText", () => {
  test("uses the secure clipboard when available", async () => {
    const written = [];
    await copyText("setup-url", {
      secure: true,
      clipboard: { writeText: async (value) => written.push(value) },
      document: null,
    });
    expect(written).toEqual(["setup-url"]);
  });

  test("falls back to a temporary selected field and removes it", async () => {
    const field = { style: {}, selectCalled: false, removed: false };
    field.select = () => { field.selectCalled = true; };
    field.remove = () => { field.removed = true; };
    const document = {
      createElement: () => field,
      body: { append: () => {} },
      execCommand: (command) => command === "copy",
    };
    await copyText("pairing-code", { secure: false, clipboard: null, document });
    expect(field.value).toBe("pairing-code");
    expect(field.selectCalled).toBeTrue();
    expect(field.removed).toBeTrue();
  });

  test("removes the fallback field when copying fails", async () => {
    const field = { style: {}, select: () => {}, removed: false };
    field.remove = () => { field.removed = true; };
    const document = {
      createElement: () => field,
      body: { append: () => {} },
      execCommand: () => false,
    };
    expect(copyText("x", { secure: false, clipboard: null, document })).rejects.toThrow();
    expect(field.removed).toBeTrue();
  });
});
