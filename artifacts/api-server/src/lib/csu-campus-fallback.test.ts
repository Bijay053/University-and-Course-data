import { test } from "node:test";
import assert from "node:assert/strict";
import * as cheerio from "cheerio";
import { applyCsuTextualCampusFallback } from "./csu-campus-fallback.ts";

const CSU_URL = "https://study.csu.edu.au/courses/postgraduate/bachelor-veterinary-biology-doctor-veterinary-medicine";

function run(html: string, url = CSU_URL) {
  const $ = cheerio.load(html);
  const data: { courseLocation?: string } = {};
  applyCsuTextualCampusFallback($, html, url, data);
  return data.courseLocation;
}

test("no-op when courseLocation already populated", () => {
  const $ = cheerio.load("<h2>Where you can study</h2><p>Bathurst</p>");
  const data: { courseLocation?: string } = { courseLocation: "Wagga Wagga" };
  applyCsuTextualCampusFallback($, $.html(), CSU_URL, data);
  assert.equal(data.courseLocation, "Wagga Wagga");
});

test("no-op for non-CSU URLs", () => {
  const out = run(
    "<h2>Where you can study</h2><p>Bathurst, Wagga Wagga</p>",
    "https://www.example.edu.au/courses/foo",
  );
  assert.equal(out, undefined);
});

test("harvests campuses from heading + paragraph block", () => {
  const out = run(`
    <html><body>
      <h2>Where you can study</h2>
      <p>Bathurst, Wagga Wagga, Port Macquarie</p>
    </body></html>
  `);
  assert.equal(out, "Bathurst, Port Macquarie, Wagga Wagga");
});

test("harvests from heading + ul block (campus chips)", () => {
  const out = run(`
    <h3>Campuses</h3>
    <ul><li>Albury-Wodonga</li><li>Bathurst</li><li>Orange</li></ul>
  `);
  assert.equal(out, "Albury-Wodonga, Bathurst, Orange");
});

test("appends Online when an online offering is mentioned in the same block", () => {
  const out = run(`
    <h2>Study locations</h2>
    <ul><li>Bathurst</li><li>Wagga Wagga</li><li>Online</li></ul>
  `);
  assert.equal(out, "Bathurst, Wagga Wagga, Online");
});

test("inline-sentence harvest catches 'available at' phrasing without a heading", () => {
  const out = run(`
    <p>This course is available at Bathurst, Wagga Wagga and Port Macquarie.</p>
    <p>Some unrelated paragraph mentioning Bathurst again as alumni news.</p>
  `);
  assert.equal(out, "Bathurst, Port Macquarie, Wagga Wagga");
});

test("returns undefined when no campus context anchor is present", () => {
  // Mentioning Bathurst in unrelated text (alumni news, etc.) must NOT leak in.
  const out = run(`
    <p>Our latest research grant supports projects in Bathurst and beyond.</p>
  `);
  assert.equal(out, undefined);
});

test("Albury-Wodonga matches whether hyphenated or space-separated", () => {
  const out = run(`
    <h2>Where you can study</h2>
    <p>Albury Wodonga</p>
  `);
  assert.equal(out, "Albury-Wodonga");
});

test("Albury–Wodonga with en-dash and Wagga/Wagga with slash both match", () => {
  const out = run(`
    <h2>Where you can study</h2>
    <p>Albury\u2013Wodonga, Wagga/Wagga</p>
  `);
  assert.equal(out, "Albury-Wodonga, Wagga Wagga");
});

test("anchor in one paragraph does NOT pull city names from a separate paragraph (P1 fix)", () => {
  // Even though the page contains an anchor phrase ("available at" without
  // any campus in its sentence) AND separately mentions Bathurst in alumni
  // copy, the per-element scoping must keep them isolated.
  const out = run(`
    <p>Scholarships are available at our institution.</p>
    <p>Read our latest alumni story from Bathurst.</p>
    <p>Our research team in Wagga Wagga published a paper.</p>
  `);
  assert.equal(out, undefined);
});

test("heading-anchored harvest ignores adjacent <div> sidebar wrappers (P1 fix)", () => {
  // The next sibling after the heading is a generic <div> sidebar, NOT a
  // value-block tag (ul/ol/dl/dd/p/td). The probe must skip it without
  // harvesting Bathurst from the sidebar.
  const out = run(`
    <h2>Where you can study</h2>
    <div class="sidebar"><a href="/news">Story from Bathurst alumni</a></div>
    <span>Recent news</span>
    <span>More news</span>
  `);
  assert.equal(out, undefined);
});

test("inline 'study online' produces an Online-only location", () => {
  const out = run(`
    <h2>Study mode and locations</h2>
    <p>You can study online.</p>
  `);
  assert.equal(out, "Online");
});
