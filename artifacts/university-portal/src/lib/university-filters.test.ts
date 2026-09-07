import { describe, expect, it } from "vitest";

import { filterUniversities } from "./university-filters";

const universities = [
  { id: 1, country: "Australia", certificationStatus: "certified", featured: true },
  { id: 2, country: "Australia", certificationStatus: "draft", featured: false },
  { id: 3, country: "New Zealand", certificationStatus: "needs_review", featured: false },
  { id: 4, country: "United Kingdom", certificationStatus: null, featured: null },
];

describe("filterUniversities", () => {
  it("combines country, status, and featured filters", () => {
    expect(filterUniversities(universities, {
      country: "Australia",
      status: "certified",
      featured: "featured",
    })).toEqual([universities[0]]);
  });

  it("treats a missing certification status as draft", () => {
    expect(filterUniversities(universities, {
      country: "all",
      status: "draft",
      featured: "all",
    })).toEqual([universities[1], universities[3]]);
  });

  it("supports filtering for universities that are not featured", () => {
    expect(filterUniversities(universities, {
      country: "all",
      status: "all",
      featured: "not_featured",
    })).toEqual([universities[1], universities[2], universities[3]]);
  });
});