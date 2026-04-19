import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  detectCoursePageTemplate,
  mergeBatchCoursePageTemplates,
  pickEffectiveCourseTemplate,
} from "./course-page-template.ts";

describe("course-page-template", () => {
  it("detects Elementor-style summary blocks", () => {
    const html = `
      <body><div class="elementor-widget-text-editor">
        <h3>CRICOS Code</h3><p>12345A</p>
        <h3>Intakes</h3><p>February, July</p>
        <h3>Course Length</h3><p>3 years full-time</p>
        <h3>Campus</h3><p>Melbourne</p>
        <h3>Delivery Mode</h3><p>On-campus</p>
      </div></body>`;
    const t = detectCoursePageTemplate(html, "https://example.edu.au/courses/foo/");
    assert.equal(t.kind, "elementor_summary_blocks");
    assert.ok(t.confidence >= 0.4);
  });

  it("merge requires agreement for two samples", () => {
    const a = mergeBatchCoursePageTemplates([
      { kind: "elementor_summary_blocks", confidence: 0.8 },
      { kind: "elementor_summary_blocks", confidence: 0.7 },
    ]);
    assert.equal(a.kind, "elementor_summary_blocks");

    const b = mergeBatchCoursePageTemplates([
      { kind: "elementor_summary_blocks", confidence: 0.8 },
      { kind: "vit_keyword_summary", confidence: 0.9 },
    ]);
    assert.equal(b.kind, "unknown");
  });

  it("merge uses majority for three samples", () => {
    const m = mergeBatchCoursePageTemplates([
      { kind: "elementor_summary_blocks", confidence: 0.8 },
      { kind: "elementor_summary_blocks", confidence: 0.7 },
      { kind: "vit_keyword_summary", confidence: 0.9 },
    ]);
    assert.equal(m.kind, "elementor_summary_blocks");
  });

  it("pickEffective uses batch only when page agrees", () => {
    const batch = { kind: "elementor_summary_blocks" as const, confidence: 0.9 };
    const page = { kind: "elementor_summary_blocks" as const, confidence: 0.85 };
    const e = pickEffectiveCourseTemplate(batch, page);
    assert.equal(e.kind, "elementor_summary_blocks");

    const page2 = { kind: "unknown" as const, confidence: 0 };
    const e2 = pickEffectiveCourseTemplate(batch, page2);
    assert.equal(e2.kind, "unknown");
  });
});
