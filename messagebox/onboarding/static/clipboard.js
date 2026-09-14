"use strict";

(function exposeClipboard(root) {
  async function copyText(text, { secure, clipboard, document }) {
    if (secure && clipboard?.writeText) {
      await clipboard.writeText(text);
      return;
    }
    const field = document.createElement("textarea");
    field.value = text;
    field.readOnly = true;
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.append(field);
    try {
      field.select();
      if (!document.execCommand("copy")) throw new Error("copy unavailable");
    } finally {
      field.remove();
    }
  }

  root.ButtonBoxClipboard = { copyText };
  if (typeof module !== "undefined") module.exports = { copyText };
})(typeof window === "undefined" ? globalThis : window);
