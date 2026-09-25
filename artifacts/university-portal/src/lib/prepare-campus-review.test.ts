import { afterEach, describe, expect, it, vi } from "vitest";
import { needsCampusPreparation, prepareCampusReview } from "./prepare-campus-review";

afterEach(() => vi.unstubAllGlobals());
const row = (id: number) => ({ id, status: "pending", feeVariants: { status: "range" } });
describe("automatic campus preparation", () => {
  it("prepares only unscoped and grouped offerings", () => {
    expect(needsCampusPreparation(row(1))).toBe(true);
    expect(needsCampusPreparation({ ...row(1), extractionMethod: { campus_fee_scope: { locations: ["London"] } } })).toBe(false);
    expect(needsCampusPreparation({ ...row(1), extractionMethod: { campus_fee_scope: { locations: ["London", "Leeds"] } } })).toBe(true);
    expect(needsCampusPreparation({ ...row(1), status: "approved" })).toBe(false);
  });
  it("bounds concurrency, reloads once, preserves source-child mapping and never publishes", async () => {
    let active = 0, max = 0;
    const fetchMock = vi.fn(async (url, init) => {
      expect(url).toBe("/api/scrape/staged/prepare-campus-courses");
      const { ids } = JSON.parse(init.body);
      expect(ids).toHaveLength(1);
      max = Math.max(max, ++active);
      await new Promise(resolve => setTimeout(resolve, 5));
      active--;
      return { ok: true, json: async () => ({ results: [{ id: ids[0], status: "split", courseIds: [ids[0], ids[0] + 10] }] }) };
    });
    vi.stubGlobal("fetch", fetchMock);
    const reload = vi.fn(async () => [row(1), row(11)]);
    const result = await prepareCampusReview([row(1), row(2), row(3)], reload, vi.fn());
    expect(max).toBe(2);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(result.results.find(r => r.id === 1)?.courseIds).toEqual([1, 11]);
    expect(result.rows).toHaveLength(2);
  });
  it("surfaces uncertainty and permission failures without an automatic retry loop", async () => {
    const fetchMock = vi.fn(async () => ({ ok: false, status: 403 }));
    vi.stubGlobal("fetch", fetchMock);
    const reload = vi.fn(async () => [row(1), row(2), row(3)]);
    const result = await prepareCampusReview([row(1), row(2), row(3)], reload, vi.fn());
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(2);
    expect(result.issues).toHaveLength(3);
    expect(result.issues[0].reason).toContain("permission");
    expect(reload).toHaveBeenCalledTimes(1);
  });
  it("does not fetch/reload after cancellation or for already separate campuses", async () => {
    const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController(); controller.abort();
    await expect(prepareCampusReview([row(1)], vi.fn(), vi.fn(), controller.signal)).rejects.toMatchObject({ name: "AbortError" });
    const reload = vi.fn();
    await prepareCampusReview([{ ...row(2), extractionMethod: { campus_fee_scope: { locations: ["Leeds"] } } }], reload, vi.fn());
    expect(fetchMock).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  });
});