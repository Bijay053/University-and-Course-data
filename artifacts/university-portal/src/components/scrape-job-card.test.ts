import { describe, expect, it } from "vitest";

import {
  isCategoryPageWarningStale,
  runtimeProgressFromStatus,
} from "./scrape-job-card";

describe("runtimeProgressFromStatus", () => {
  it("uses the persisted processed/total values from every status poll", () => {
    expect(runtimeProgressFromStatus({
      current: 17,
      total: 40,
      totalFound: 40,
    })).toEqual({ current: 17, total: 40 });
  });

  it("supports resumed jobs and clamps stale counters to the catalogue total", () => {
    expect(runtimeProgressFromStatus({
      current: 257,
      totalFound: 409,
    })).toEqual({ current: 257, total: 409 });
    expect(runtimeProgressFromStatus({
      current: 411,
      totalFound: 409,
    })).toEqual({ current: 409, total: 409 });
  });
});

describe("isCategoryPageWarningStale", () => {
  it("drops a warning from an obsolete 14-link candidate set when the job has 182 URLs", () => {
    expect(isCategoryPageWarningStale(14, 182)).toBe(true);
  });

  it("keeps a warning that describes the current job total", () => {
    expect(isCategoryPageWarningStale(14, 14)).toBe(false);
  });
});