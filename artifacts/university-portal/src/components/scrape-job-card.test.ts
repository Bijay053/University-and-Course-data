import { describe, expect, it } from "vitest";

import { runtimeProgressFromStatus } from "./scrape-job-card";

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