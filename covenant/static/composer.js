"use strict";
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CovenantComposer = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function extractRecipients(text, handles) {
    const known = new Set(handles), recipients = [], pattern = /^@([a-z][a-z0-9_]{0,31})(?=\s)/i;
    let body = text, match;
    while ((match = body.match(pattern)) && known.has(match[1].toLowerCase())) {
      const handle = match[1].toLowerCase();
      if (!recipients.includes(handle)) recipients.push(handle);
      body = body.slice(match[0].length);
      const next = body.replace(/^\s+/, ""), nextMatch = next.match(pattern);
      if (nextMatch && known.has(nextMatch[1].toLowerCase())) body = next;
      else {body = body.replace(/^[ \t]*(?:\r?\n)?/, ""); break;}
    }
    return {recipients, body, consumed: text.length - body.length};
  }
  function serialize(recipients, body) {
    return [...new Set(recipients)].map(handle => "@" + handle).join(" ") + (recipients.length ? "\n" : "") + body;
  }
  return {extractRecipients, serialize};
});
