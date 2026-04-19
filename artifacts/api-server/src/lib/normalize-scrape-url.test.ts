import test from "node:test";
import assert from "node:assert/strict";
import { normalizeScrapeUrl, tryParseLooseUrl } from "./normalize-scrape-url.ts";

test("bare www host becomes https", () => {
  assert.equal(normalizeScrapeUrl("www.asahe.edu.au/"), "https://www.asahe.edu.au/");
});

test("protocol-relative becomes https", () => {
  assert.equal(normalizeScrapeUrl("//www.asahe.edu.au/courses"), "https://www.asahe.edu.au/courses");
});

test("tryParseLooseUrl accepts query on bare host", () => {
  const u = tryParseLooseUrl("www.example.edu.au?x=1");
  assert.ok(u);
  assert.equal(u?.protocol, "https:");
});
