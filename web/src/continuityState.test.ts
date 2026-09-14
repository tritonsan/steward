import { describe, expect, it } from "vitest";
import {
  appointmentControls,
  meetingActionState,
  quoteControls,
} from "./continuityState";

const quote = (id: string, vendor = "test-vendor") => ({
  artifact_id: id,
  payload: { quote: { vendor_id: vendor } },
});
const appointment = () => ({
  artifact_id: "proposal-1",
  created_at: "2026-09-11T07:00:00Z",
  payload: {
    status: "review",
    facts: {
      starts_at: "2026-09-12T10:00:00Z",
      ends_at: "2026-09-12T11:00:00Z",
      unchanged_terms_evidence: "The quoted price and scope are unchanged.",
      access_evidence: "Meet the access contact at reception.",
    },
  },
});
const appointmentCase = () => ({
  status: "awaiting_appointment",
  appointments: [appointment()],
  tasks: [{ kind: "appointment_review" }],
});

describe("case controls at delivery and revision boundaries", () => {
  it("keeps the new unsent order editable while excluding its withdrawn predecessor", () => {
    const state = quoteControls({
      status: "committed",
      accepted_quote_id: "corrected",
      quotes: [
        quote("old"),
        quote("corrected"),
        quote("other", "other-vendor"),
      ],
      quote_supersessions: [
        {
          payload: {
            previous_quote_id: "old",
            replacement_quote_id: "corrected",
          },
        },
      ],
      outbox: [
        {
          payload: { purpose: "commitment" },
          last_error_code: "superseded_before_send",
          status: "dead_letter",
        },
        { payload: { purpose: "commitment" }, status: "pending" },
      ],
    });
    expect(state.editable).toBe(true);
    expect([...state.activeIds]).toEqual(["corrected", "other"]);
    expect(state.supersededIds.has("old")).toBe(true);
  });

  it("keeps rejection tied to its version and leaves a corrected vendor reply eligible", () => {
    const state = quoteControls({
      status: "quotes_received",
      quotes: [quote("rejected"), quote("replacement")],
      quote_rejections: [
        {
          payload: {
            quote_id: "rejected",
            reason: "Scope did not include the entrance.",
          },
        },
      ],
    });
    expect([...state.activeIds]).toEqual(["replacement"]);
    expect(state.rejectedIds.has("rejected")).toBe(true);
    expect(state.editable).toBe(true);
    expect(state.conflictingVendorIds.size).toBe(0);
  });

  it("does not infer supersession merely because a newer quote arrived", () => {
    const state = quoteControls({
      status: "quotes_received",
      quotes: [quote("first"), quote("second")],
    });
    expect([...state.activeIds]).toEqual(["first", "second"]);
    expect(state.supersededIds.size).toBe(0);
    expect(state.conflictingVendorIds.has("test-vendor")).toBe(true);
  });

  it.each([
    { status: "pending", delivery_started_at: "2026-09-11T07:00:00Z" },
    { status: "ambiguous" },
    { status: "delivered" },
  ])(
    "locks quote changes once a service order may have left Steward: %j",
    (delivery) => {
      expect(
        quoteControls({
          status: "committed",
          accepted_quote_id: "quote-1",
          outbox: [{ payload: { purpose: "commitment" }, ...delivery }],
        }).editable,
      ).toBe(false);
    },
  );

  it("preserves completed quote history without reopening procurement controls", () => {
    expect(
      quoteControls({ status: "closed", quotes: [quote("quote-1")] }).editable,
    ).toBe(false);
  });

  it("offers an appointment exception only with explicit terms, access, rules and ordered times", () => {
    expect(appointmentControls(appointmentCase(), true).canAccept).toBe(true);
    expect(appointmentControls(appointmentCase(), false).canAccept).toBe(false);
    const missingTerms = appointmentCase();
    missingTerms.appointments[0].payload.facts.unchanged_terms_evidence = "";
    expect(appointmentControls(missingTerms, true).canAccept).toBe(false);
    const invalidTimes = appointmentCase();
    invalidTimes.appointments[0].payload.facts.ends_at = "2026-09-12T09:00:00Z";
    expect(appointmentControls(invalidTimes, true).canAccept).toBe(false);
    const simulated = appointmentCase();
    simulated.appointments[0].payload.facts.starts_at = "2020-01-01T10:00:00Z";
    simulated.appointments[0].payload.facts.ends_at = "2020-01-01T11:00:00Z";
    // The server clock owns expiry, including replayed simulation scenarios.
    expect(appointmentControls(simulated, true).canAccept).toBe(true);
  });

  it("does not replace an access response or uncertain delivery with another acceptance", () => {
    for (const kind of ["appointment_access", "appointment_delivery"]) {
      const record = appointmentCase();
      record.tasks.push({ kind });
      const result = appointmentControls(record, true);
      expect(result.canAccept).toBe(false);
      expect(result.needsDeliveryReview).toBe(kind === "appointment_delivery");
    }
  });

  it("shows acceptance waiting for delivery without offering the same command again", () => {
    const record = {
      ...appointmentCase(),
      outbox: [
        {
          created_at: "2026-09-11T07:01:00Z",
          status: "pending",
          payload: {
            purpose: "follow_up",
            message: { subject: "Appointment: Lift repair" },
          },
        },
      ],
    };
    expect(appointmentControls(record, true).canAccept).toBe(false);
    expect(appointmentControls(record, true).reason).toContain(
      "checking delivery",
    );
  });

  it("allows a new proposal at the frozen timestamp of an older confirmed acceptance, while retaining pending and ambiguous delivery gates", () => {
    const proposal = appointment();
    const oldAcceptance = {
      outbox_id: "appointment-accept-old",
      created_at: proposal.created_at,
      status: "delivered",
      payload: {
        purpose: "follow_up",
        message: { subject: "Appointment: Lift repair" },
      },
    };
    const record = {
      ...appointmentCase(),
      status: "scheduled",
      appointments: [{ ...proposal, artifact_id: "proposal-old" }, proposal],
      confirmed_appointments: [
        {
          artifact_id: oldAcceptance.outbox_id,
          payload: { appointment_id: "proposal-old", revision: 1 },
        },
      ],
      outbox: [oldAcceptance],
    };
    expect(appointmentControls(record, true).canAccept).toBe(true);
    for (const status of ["pending", "ambiguous"]) {
      const withCurrentAcceptance = {
        ...record,
        outbox: [
          ...record.outbox,
          { ...oldAcceptance, outbox_id: "appointment-accept-current", status },
        ],
      };
      expect(appointmentControls(withCurrentAcceptance, true).canAccept).toBe(
        false,
      );
      expect(appointmentControls(withCurrentAcceptance, true).reason).toContain(
        "checking delivery",
      );
    }
  });

  it("waits for linked maintenance verification before offering action completion", () => {
    const linked = [{ action_id: "action-1", verified: false }];
    expect(meetingActionState("action-1", [], linked, true)).toMatchObject({
      canComplete: false,
      canRequestMaintenance: false,
      waitingForMaintenance: true,
    });
    expect(
      meetingActionState(
        "action-1",
        [],
        [{ ...linked[0], verified: true }],
        true,
      ).canComplete,
    ).toBe(true);
  });

  it("shows pending procurement before the worker has created its linked case", () => {
    const request = { payload: { action_id: "action-1" } };
    expect(
      meetingActionState("action-1", [], [], true, [request]),
    ).toMatchObject({
      pendingRequest: true,
      canComplete: false,
      canRequestMaintenance: false,
      waitingForMaintenance: true,
    });
    expect(
      meetingActionState(
        "action-1",
        [],
        [{ action_id: "action-1", verified: true }],
        true,
        [request],
      ),
    ).toMatchObject({
      pendingRequest: false,
      canComplete: true,
      canRequestMaintenance: false,
    });
  });

  it("shows completion evidence without repeat completion or procurement actions", () => {
    const completion = {
      payload: { action_id: "action-1", notes: "Access markings checked." },
    };
    expect(
      meetingActionState("action-1", [completion], [], true),
    ).toMatchObject({
      completion,
      canComplete: false,
      canRequestMaintenance: false,
    });
    expect(meetingActionState("action-1", [], [], false)).toMatchObject({
      canComplete: false,
      canRequestMaintenance: false,
    });
  });
});
