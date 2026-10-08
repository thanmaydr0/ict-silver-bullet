import { afterEach, describe, expect, it, vi } from "vitest";
import { age, ApiError, countdown, dashboard, number, request } from "./api";
afterEach(() => vi.unstubAllGlobals());
describe("dashboard API", () => {
  it("uses same-origin cookies and rejects expired sessions", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 401 });
    vi.stubGlobal("fetch", fetch);
    await expect(request("/dashboard")).rejects.toBeInstanceOf(ApiError);
    expect(fetch.mock.calls[0][1].credentials).toBe("same-origin");
  });
  it("rejects an incompatible contract", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({ schema_version: 2 }),
      }),
    );
    await expect(dashboard()).rejects.toThrow("version mismatch");
  });
  it("handles logout without parsing JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 204 }),
    );
    expect(await request("/auth/logout", { method: "POST" })).toBeUndefined();
  });
  it("keeps zero values, unknowns, and advancing time distinct", () => {
    expect(number(0)).toBe("0.00");
    expect(number(null)).toBe("Unavailable");
    const now = Date.parse("2026-10-07T14:00:00Z");
    expect(age("2026-10-07T13:59:00Z", now)).toBe(60);
    expect(age(null, now)).toBeNull();
    expect(countdown("2026-10-07T15:01:02Z", now)).toBe("01:01:02");
    expect(countdown("2026-10-07T13:00:00Z", now)).toBe("00:00:00");
  });
});
