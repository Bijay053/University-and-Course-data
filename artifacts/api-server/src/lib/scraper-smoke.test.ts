/**
 * Offline smoke suite: representative page shapes and URL normalization used by bulk scrape.
 * No network — keeps CI / smoke runs deterministic.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  detectCoursePageTemplate,
  mergeBatchCoursePageTemplates,
  pickEffectiveCourseTemplate,
} from "./course-page-template.ts";
import { normalizeScrapeUrl, tryParseLooseUrl } from "./normalize-scrape-url.ts";

/** Minimal HTML bodies that mirror recurring university CMS patterns in production scrapes. */
const REPRESENTATIVE_SAMPLES = [
  {
    id: "vit_keyword_summary",
    url: "https://www.vit.edu.au/courses/bachelor-of-it/",
    html: `
      <html><body>
        <p>Locations: Melbourne Sydney 2026 intakes: March, July</p>
        <p>CRICOS 01234J</p>
        <!-- padding: detectCoursePageTemplate ignores very short documents -->
        <p>${"x".repeat(120)}</p>
      </body></html>`,
    expectedKind: "vit_keyword_summary" as const,
  },
  {
    id: "course_card_panels",
    url: "https://www.kbs.edu.au/course/mba/",
    html: `
      <html><body>
        <div class="course-card-panel__item"><span class="course-card-panel__label">Duration</span>
          <span class="course-card-panel__value">2 years</span></div>
        <div class="course-card-panel__item"><span class="course-card-panel__label">Fees</span>
          <span class="course-card-panel__value">$40,000</span></div>
        <div class="course-card-panel__item"><span class="course-card-panel__label">Location</span>
          <span class="course-card-panel__value">Sydney</span></div>
      </body></html>`,
    expectedKind: "course_card_panels" as const,
  },
  {
    id: "elementor_summary_blocks",
    url: "https://example.edu.au/study/courses/cs/",
    html: `
      <body><div class="elementor-widget-text-editor">
        <h3>CRICOS Code</h3><p>12345A</p>
        <h3>Intakes</h3><p>February, July</p>
        <h3>Course Length</h3><p>3 years full-time</p>
        <h3>Campus</h3><p>Melbourne</p>
        <h3>Delivery Mode</h3><p>On-campus</p>
      </div></body>`,
    expectedKind: "elementor_summary_blocks" as const,
  },
] as const;

describe("scraper smoke (representative university samples)", () => {
  it("classifies each offline fixture to the expected template kind", () => {
    for (const sample of REPRESENTATIVE_SAMPLES) {
      const t = detectCoursePageTemplate(sample.html, sample.url);
      assert.equal(
        t.kind,
        sample.expectedKind,
        `${sample.id}: expected ${sample.expectedKind}, got ${t.kind} (confidence ${t.confidence})`,
      );
      assert.ok(t.confidence > 0, `${sample.id}: confidence should be > 0`);
    }
  });

  it("batch merge + pickEffective agree when samples are homogeneous", () => {
    const detections = REPRESENTATIVE_SAMPLES.filter((s) => s.id === "elementor_summary_blocks").map((s) =>
      detectCoursePageTemplate(s.html, s.url),
    );
    assert.equal(detections.length, 1);
    const batch = mergeBatchCoursePageTemplates([detections[0]!, detections[0]!]);
    assert.equal(batch.kind, "elementor_summary_blocks");
    const page = detections[0]!;
    const effective = pickEffectiveCourseTemplate(batch, page);
    assert.equal(effective.kind, "elementor_summary_blocks");
  });

  it("normalizeScrapeUrl accepts common pasted forms", () => {
    assert.equal(normalizeScrapeUrl("www.uni.edu.au/path?q=1"), "https://www.uni.edu.au/path?q=1");
    assert.equal(normalizeScrapeUrl("//cdn.uni.edu.au/foo"), "https://cdn.uni.edu.au/foo");
    assert.equal(tryParseLooseUrl("study.uni.edu.au/course/x")?.hostname, "study.uni.edu.au");
  });
});
