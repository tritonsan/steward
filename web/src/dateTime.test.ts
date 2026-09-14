import { describe, expect, it } from "vitest";
import {
  addCalendarDays,
  formatDate,
  formatDateTime,
  retryMeetingSlots,
  toUtcDateTime,
  toWallDateTime,
  validDateTime,
} from "./dateTime";

describe("English dates in the Northgate display zone", () => {
  it("saves 18:00 Istanbul as 15:00 UTC independently of the browser zone", () => {
    expect(toUtcDateTime("2026-09-13T18:00")).toBe("2026-09-13T15:00:00.000Z");
    expect(toWallDateTime("2026-09-13T15:00:00Z")).toBe("2026-09-13T18:00");
  });
  it("retains the correct local calendar day across midnight", () => {
    expect(toUtcDateTime("2026-09-13T00:30")).toBe("2026-09-12T21:30:00.000Z");
    expect(formatDate("2026-09-12T22:00:00Z")).toBe("13 Sept 2026");
  });
  it("always displays English month names and 24-hour Istanbul time", () => {
    expect(formatDateTime("2026-09-13T15:00:00Z")).toBe("13 Sept 2026, 18:00");
  });
  it("rejects impossible dates, partial input, and invalid clock values", () => {
    for (const value of [
      "2026-02-30T18:00",
      "2026-09-13T24:00",
      "2026-09-13T18:75",
      "T18:00",
      "2026-09-13T",
      "2026-09-13T8:30",
      "2026-13-01T10:00",
    ]) {
      expect(validDateTime(value)).toBe(false);
      expect(() => toUtcDateTime(value)).toThrow();
    }
  });
  it("handles leap days and calendar boundaries", () => {
    expect(validDateTime("2028-02-29T18:00")).toBe(true);
    expect(validDateTime("2026-02-29T18:00")).toBe(false);
    expect(addCalendarDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addCalendarDays("2028-03-01", -1)).toBe("2028-02-29");
  });
  it("keeps daily retry options at the same Istanbul time across a month boundary", () => {
    expect(
      retryMeetingSlots("2026-09-30T18:00", "2026-10-02T18:00").map(
        (s) => s.starts_at,
      ),
    ).toEqual([
      "2026-09-30T15:00:00.000Z",
      "2026-10-01T15:00:00.000Z",
      "2026-10-02T15:00:00.000Z",
    ]);
    expect(retryMeetingSlots("2026-09-13T18:00", "2026-09-12T18:00")).toEqual(
      [],
    );
    expect(
      retryMeetingSlots("2026-09-13T18:00", "2026-10-13T18:00"),
    ).toHaveLength(12);
  });
});
