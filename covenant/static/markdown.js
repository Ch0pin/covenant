"use strict";
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(require("./markdown-it.min.js"));
  else root.CovenantMarkdown = factory(root.markdownit);
})(typeof globalThis !== "undefined" ? globalThis : this, function (MarkdownIt) {
  const parser = new MarkdownIt({html: false, breaks: true, linkify: false, typographer: false});
  parser.disable("image");
  const validateLink = parser.validateLink.bind(parser);
  parser.validateLink = url => /^(https?:|mailto:)/i.test(url) && validateLink(url);
  parser.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
    tokens[index].attrSet("target", "_blank");
    tokens[index].attrSet("rel", "noopener noreferrer");
    return renderer.renderToken(tokens, index, options);
  };
  function splitRecipients(text) {
    const recipients = [], pattern = /^@([a-z][a-z0-9_]{0,31})(?=\s|$)/;
    let body = text, match;
    while ((match = body.match(pattern))) {
      if (!recipients.includes(match[1])) recipients.push(match[1]);
      body = body.slice(match[0].length);
      const next = body.replace(/^\s+/, "");
      if (pattern.test(next)) body = next;
      else {body = body.replace(/^[ \t]*(?:\r?\n)?/, ""); break;}
    }
    return {recipients, body};
  }
  return {render: text => parser.render(text), splitRecipients};
});
