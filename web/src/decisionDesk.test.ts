import { describe, expect, it } from "vitest";
import { needsManagerAction } from "./decisionDesk";

describe("management decision queue", () => {
  it("keeps participant availability in response tracking", () => {
    expect(
      needsManagerAction(
        { kind: "meeting_availability", allowed_roles: ["manager"] },
        "Simon",
      ),
    ).toBe(false);
  });
  it("keeps another resident's private request out of the manager decision count", () => {
    expect(
      needsManagerAction(
        {
          kind: "clarification",
          allowed_roles: ["manager", "resident"],
          assigned_actor_ids: ["James"],
        },
        "Simon",
      ),
    ).toBe(false);
  });
  it("shows a manager's own assigned action and access response", () => {
    for (const kind of ["meeting_action", "appointment_access"])
      expect(
        needsManagerAction(
          {
            kind,
            allowed_roles: ["manager", "resident"],
            assigned_actor_ids: ["Simon"],
          },
          "Simon",
        ),
      ).toBe(true);
  });
  it("preserves authorized manager verification even when the reporter is also assigned", () => {
    expect(
      needsManagerAction(
        {
          kind: "verify_completion",
          allowed_roles: ["manager", "resident"],
          assigned_actor_ids: ["James"],
        },
        "Simon",
      ),
    ).toBe(true);
  });
  it("keeps exceptions actionable while preserving resident-only tasks in tracking", () => {
    expect(
      needsManagerAction(
        { kind: "delivery_review", allowed_roles: ["manager"] },
        "Simon",
      ),
    ).toBe(true);
    expect(
      needsManagerAction(
        {
          kind: "appointment_access",
          allowed_roles: ["resident"],
          assigned_actor_ids: ["James"],
        },
        "Simon",
      ),
    ).toBe(false);
  });
});
