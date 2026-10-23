const { test } = require("node:test");
const assert = require("node:assert/strict");
const { supported } = require("./check-node-security.cjs");
test("rejects old, unsupported and malformed runtimes", () => {
  for (const v of ["20.20.2", "22.23.4", "24.13.0", "24.18.1", "24.22.0", "26.12.1", "broken"]) {
    assert.equal(supported(v), false, v);
  }
});
test("accepts security baseline and later Node 24 patches", () => {
  for (const v of ["24.22.1", "v24.22.2", "24.23.0"]) assert.equal(supported(v), true);
});
