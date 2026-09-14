type RecordData = Record<string, any>;

export const isFinishedCase = (status: string) =>
  ["closed", "cancelled"].includes(status);

export function quoteControls(record: RecordData) {
  const quotes: RecordData[] = record.quotes || [];
  const rejectedIds = new Set<string>(
    (record.quote_rejections || []).map(
      (item: RecordData) => item.payload.quote_id,
    ),
  );
  const supersededIds = new Set<string>(
    (record.quote_supersessions || []).map(
      (item: RecordData) => item.payload.previous_quote_id,
    ),
  );
  const activeIds = new Set<string>();
  const vendorCounts = new Map<string, number>();
  for (const quote of quotes) {
    if (
      rejectedIds.has(quote.artifact_id) ||
      supersededIds.has(quote.artifact_id)
    )
      continue;
    activeIds.add(quote.artifact_id);
    const vendorId = quote.payload.quote.vendor_id;
    vendorCounts.set(vendorId, (vendorCounts.get(vendorId) || 0) + 1);
  }
  const conflictingVendorIds = new Set(
    [...vendorCounts].filter(([, count]) => count > 1).map(([id]) => id),
  );
  const orders = (record.outbox || []).filter(
    (item: RecordData) =>
      item.payload.purpose === "commitment" &&
      item.last_error_code !== "superseded_before_send",
  );
  const deliveryStarted = orders.some(
    (item: RecordData) =>
      item.delivery_started_at ||
      item.delivered_at ||
      ["dispatching", "delivered", "ambiguous"].includes(item.status),
  );
  const editable =
    !isFinishedCase(record.status) &&
    ![
      "scheduled",
      "awaiting_appointment",
      "awaiting_verification",
      "warranty_review",
      "resolved",
    ].includes(record.status) &&
    !deliveryStarted &&
    (!record.accepted_quote_id || orders.length === 1);
  return {
    editable,
    activeIds,
    rejectedIds,
    supersededIds,
    conflictingVendorIds,
    lockedReason: isFinishedCase(record.status)
      ? "These quotes are retained with the completed case."
      : "The service order is already being delivered or has been sent. Its quotation is retained for reference; a later change needs a separate review.",
  };
}

export function appointmentControls(
  record: RecordData,
  rulesConfigured: boolean,
) {
  const proposal = record.appointments?.at(-1);
  const tasks = record.tasks || [];
  const hasTask = (kind: string) =>
    tasks.some((task: RecordData) => task.kind === kind);
  const active =
    !isFinishedCase(record.status) &&
    ["committed", "awaiting_appointment", "scheduled"].includes(record.status);
  const needsDeliveryReview = active && hasTask("appointment_delivery");
  if (!proposal)
    return { proposal, canAccept: false, needsDeliveryReview, reason: "" };
  const facts = proposal.payload.facts;
  const confirmed = record.confirmed_appointments?.some(
    (item: RecordData) => item.payload.appointment_id === proposal.artifact_id,
  );
  const queuedAcceptance = (record.outbox || []).some(
    (item: RecordData) =>
      !(
        item.status === "delivered" &&
        item.outbox_id &&
        record.confirmed_appointments?.some(
          (confirmation: RecordData) =>
            confirmation.artifact_id === item.outbox_id,
        )
      ) &&
      item.payload.purpose === "follow_up" &&
      item.payload.message?.subject?.startsWith("Appointment:") &&
      (!item.created_at ||
        !proposal.created_at ||
        item.created_at >= proposal.created_at),
  );
  let reason = "";
  if (!active || confirmed)
    reason = "This appointment is retained as part of the case record.";
  else if (needsDeliveryReview)
    reason =
      "Check the delivery receipt before confirming or changing the visit.";
  else if (hasTask("appointment_access"))
    reason =
      "The access contact’s response is needed before the visit can be confirmed.";
  else if (queuedAcceptance)
    reason =
      "Acceptance is already recorded. Steward is checking delivery before confirming the visit.";
  else if (!rulesConfigured)
    reason =
      "Set working hours and access instructions in Settings before accepting this visit.";
  else if (
    !facts.starts_at ||
    !facts.ends_at ||
    !Number.isFinite(new Date(facts.starts_at).getTime()) ||
    !Number.isFinite(new Date(facts.ends_at).getTime()) ||
    new Date(facts.ends_at).getTime() <= new Date(facts.starts_at).getTime() ||
    !facts.unchanged_terms_evidence ||
    !facts.access_evidence
  )
    reason =
      "A current vendor reply must confirm future start and end times, unchanged price and scope, and access arrangements.";
  else if (
    !hasTask("appointment_review") ||
    proposal.payload.status !== "review"
  )
    reason =
      "Steward is processing this proposal. No additional approval is needed yet.";
  return { proposal, canAccept: !reason, needsDeliveryReview, reason };
}

export function meetingActionState(
  actionId: string,
  completions: RecordData[],
  maintenance: RecordData[],
  canEdit: boolean,
  requests: RecordData[] = [],
) {
  const completion = completions.find(
    (item) => item.payload.action_id === actionId,
  );
  const linked = maintenance.filter((item) => item.action_id === actionId);
  const requested = requests.some(
    (item) => item.payload.action_id === actionId,
  );
  const pendingRequest = requested && linked.length === 0;
  return {
    completion,
    canComplete:
      canEdit &&
      !completion &&
      !pendingRequest &&
      linked.every((item) => item.verified),
    canRequestMaintenance:
      canEdit && !completion && !requested && linked.length === 0,
    waitingForMaintenance:
      !completion && (pendingRequest || linked.some((item) => !item.verified)),
    pendingRequest,
  };
}
