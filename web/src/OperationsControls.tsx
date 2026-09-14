import { formatDateTime } from "./dateTime";
import { useEffect, useState } from "react";
import { PersonalRequest } from "./PersonalRequest";
import { TelegramGroup } from "./TelegramGroup";
import { DateTimeField } from "./DateTimeField";
import { toUtcDateTime, toWallDateTime, validDateTime } from "./dateTime";
import {
  appointmentControls,
  isFinishedCase,
  quoteControls,
} from "./continuityState";
type Api = (path: string, options?: RequestInit) => Promise<any>;

export function CaseContinuity({
  record,
  api,
  reload,
  showSource,
}: {
  record: any;
  api: Api;
  reload: () => Promise<void>;
  showSource?: (id: string) => void;
}) {
  const [notes, setNotes] = useState(""),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const [owner, setOwner] = useState(""),
    [due, setDue] = useState(""),
    [actionId, setActionId] = useState("");
  const [providerReceipt, setProviderReceipt] = useState("");
  const [receiptAppointment, setReceiptAppointment] = useState("");
  const [rulesConfigured, setRulesConfigured] = useState(false);
  const [completedActions, setCompletedActions] = useState<string[]>([]);
  const [directory, setDirectory] = useState<string[]>([]);
  useEffect(() => {
    let cancelled = false;
    if (record.meetings?.length) {
      Promise.all([api("/directory"), api(`/cases/${record.case_id}/meeting`)])
        .then(([directory, meeting]) => {
          if (cancelled) return;
          setDirectory(directory.residents);
          setCompletedActions(
            (meeting.records?.["meeting_action_completion.v1"] || []).map(
              (item: any) => item.payload.action_id,
            ),
          );
        })
        .catch((e) => {
          if (!cancelled) setError(e.message);
        });
    }
    if (record.appointments?.length)
      api("/appointment-rules")
        .then((data) => {
          if (!cancelled) setRulesConfigured(!!data.rules);
        })
        .catch((e) => {
          if (!cancelled) setError(e.message);
        });
    return () => {
      cancelled = true;
    };
  }, [record.case_id, record.version]);
  async function command(action: string, data = {}) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api(`/cases/${record.case_id}/commands`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          action,
          data,
          notes,
          expected_version: record.version,
        }),
      });
      setNotes("");
      setProviderReceipt("");
      setNotice(
        {
          reject_quote:
            "This quote version was rejected. Steward will review the remaining options.",
          clarify_quote:
            "The clarification request is queued. The case will update when the vendor replies.",
          appointment_accept:
            "Your acceptance was recorded. The visit is confirmed after acceptance delivery.",
          reconcile_appointment_delivery:
            "The delivery evidence was recorded. Steward is updating the appointment.",
          meeting_reschedule:
            "A linked case is preparing replacement meeting times. The previous invitation is no longer valid.",
          meeting_revise_decision:
            "A linked meeting will review the proposed change. The previous decision remains in effect until a replacement is confirmed.",
          meeting_reassign:
            "The action assignment was updated and the new owner will be notified.",
        }[action] || "Your change was recorded.",
      );
      await reload();
    } catch (e) {
      setError((e as Error).message);
      await reload();
    } finally {
      setBusy(false);
    }
  }
  const actions = (
    record.meetings?.flatMap((d: any) => d.payload.action_items || []) || []
  ).filter((item: any) => !completedActions.includes(item.action_id));
  const quoteState = quoteControls(record);
  const appointmentState = appointmentControls(record, rulesConfigured);
  const finished = isFinishedCase(record.status);
  const hasPendingRevision = record.revisions?.some(
    (revision: any) =>
      !record.confirmed_revisions?.some(
        (confirmed: any) =>
          confirmed.payload.child_case_id === revision.payload.child_case_id,
      ),
  );
  const canReschedule =
    !finished &&
    !hasPendingRevision &&
    record.tasks?.some((task: any) =>
      [
        "meeting_reschedule",
        "meeting_availability",
        "meeting_minutes",
      ].includes(task.kind),
    );
  const canRevise = !!record.meetings?.length && !hasPendingRevision;
  const canReassign =
    !finished &&
    actions.length > 0 &&
    record.tasks?.some((task: any) => task.kind === "meeting_action");
  const hasQuoteActions = quoteState.editable && quoteState.activeIds.size > 0;
  const hasChange =
    hasQuoteActions ||
    appointmentState.canAccept ||
    appointmentState.needsDeliveryReview ||
    canReschedule ||
    canRevise ||
    canReassign;
  function selectAssignment(id: string) {
    setActionId(id);
    const original = actions.find((item: any) => item.action_id === id);
    if (!original) return;
    const current = {
      ...original,
      ...record.assignments
        ?.filter((item: any) => item.payload.action_id === id)
        .at(-1)?.payload,
    };
    setOwner(current.owner_participant_id);
    setDue(toWallDateTime(current.due_at));
  }
  const currentAppointmentId =
    record.confirmed_appointments?.at(-1)?.payload.appointment_id;
  return (
    <section className="continuity-panel">
      {notice && (
        <p className="banner success" role="status">
          {notice}
        </p>
      )}
      {record.revisions?.map((r: any) => (
        <p key={r.artifact_id}>
          {record.confirmed_revisions?.some(
            (c: any) => c.payload.child_case_id === r.payload.child_case_id,
          )
            ? "A replacement decision has been confirmed."
            : "A linked meeting is preparing a revision. The previous decision remains in effect."}{" "}
          {r.payload.reason}
        </p>
      ))}
      {record.tasks
        ?.filter((t: any) =>
          ["clarification", "appointment_access"].includes(t.kind),
        )
        .map((t: any) => (
          <PersonalRequest
            key={t.task_id}
            question={{
              request_id: t.task_id,
              case_id: record.case_id,
              title: t.title,
              kind: t.kind,
              version: record.version,
              due_at: t.due_at,
            }}
            api={api}
            reload={reload}
          />
        ))}
      {!!record.appointments?.length && (
        <>
          <h3>Vendor appointments</h3>
          {record.appointments.map((a: any) => (
            <article className="quote" key={a.artifact_id}>
              <strong>
                Proposal {a.payload.revision} ·{" "}
                {record.confirmed_appointments?.some(
                  (c: any) => c.payload.appointment_id === a.artifact_id,
                )
                  ? currentAppointmentId === a.artifact_id
                    ? "Current confirmed appointment"
                    : "Previously confirmed"
                  : a.payload.status.replaceAll("_", " ")}
              </strong>
              <p>
                {a.payload.facts.starts_at
                  ? formatDateTime(a.payload.facts.starts_at, {
                      day: "numeric",
                      month: "short",
                      year: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    })
                  : "Time needs clarification"}
              </p>
              <p>
                {finished
                  ? "This visit is retained in the completed case history."
                  : record.status === "awaiting_verification"
                    ? "Work from this visit has been reported complete. The outcome is awaiting verification."
                    : record.status === "warranty_review"
                      ? "The previous repair is now under warranty review. Its visit details remain available here."
                      : currentAppointmentId === a.artifact_id
                        ? "The visit is confirmed. Steward will follow up after the appointment."
                        : a.artifact_id ===
                              appointmentState.proposal?.artifact_id &&
                            appointmentState.reason
                          ? appointmentState.reason
                          : a.payload.reason ||
                            "Waiting for access approval or acceptance delivery."}
              </p>
              <details>
                <summary>Vendor evidence</summary>
                <p>{a.payload.facts.evidence}</p>
              </details>
            </article>
          ))}
        </>
      )}
      {hasChange && (
        <>
          <label htmlFor="continuity-reason">
            {hasQuoteActions
              ? "Reason for a quote rejection or clarification"
              : appointmentState.needsDeliveryReview
                ? "Evidence supporting delivery reconciliation"
                : appointmentState.canAccept
                  ? "Reason for accepting this appointment exception"
                  : "Reason for revisiting the meeting or changing an assignment"}
          </label>
          <textarea
            id="continuity-reason"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            maxLength={5000}
            disabled={busy}
          />
        </>
      )}
      {appointmentState.needsDeliveryReview && (
        <div className="action-form">
          <p>
            Match the provider receipt to the appointment proposal. This records
            delivery evidence and does not resend an acceptance.
          </p>
          <label>
            Appointment covered by the receipt
            <select
              value={receiptAppointment}
              onChange={(e) => setReceiptAppointment(e.target.value)}
            >
              <option value="">Select the matching proposal</option>
              {record.appointments.map((a: any) => (
                <option key={a.artifact_id} value={a.artifact_id}>
                  Proposal {a.payload.revision} ·{" "}
                  {a.payload.facts.starts_at
                    ? formatDateTime(a.payload.facts.starts_at)
                    : "Time needs clarification"}
                </option>
              ))}
            </select>
          </label>
          <label>
            Provider delivery receipt
            <input
              value={providerReceipt}
              onChange={(e) => setProviderReceipt(e.target.value)}
            />
          </label>
          <button
            disabled={
              busy ||
              !notes.trim() ||
              !providerReceipt.trim() ||
              !receiptAppointment
            }
            onClick={() =>
              command("reconcile_appointment_delivery", {
                appointment_id: receiptAppointment,
                provider_message_id: providerReceipt,
              })
            }
          >
            Record confirmed acceptance delivery
          </button>
        </div>
      )}
      {appointmentState.canAccept && (
        <button
          className="secondary"
          disabled={busy || !notes.trim()}
          onClick={() =>
            command("appointment_accept", {
              appointment_id: appointmentState.proposal.artifact_id,
            })
          }
        >
          Accept this appointment exception
        </button>
      )}
      {!!record.quotes?.length && <h3>Vendor quotes</h3>}
      {!!record.quotes?.length && !quoteState.editable && (
        <p>{quoteState.lockedReason}</p>
      )}
      {record.quotes?.map((q: any) => (
        <div className="quote" key={q.artifact_id}>
          <strong>
            {q.payload.quote.vendor_id.replaceAll("-", " ")} ·{" "}
            {q.payload.quote.amount} {q.payload.quote.currency}
          </strong>
          <p>{q.payload.quote.scope}</p>
          {quoteState.rejectedIds.has(q.artifact_id) ? (
            <p className="badge">Rejected version · retained for reference</p>
          ) : (
            quoteState.supersededIds.has(q.artifact_id) && (
              <p className="badge">Previous version · retained for reference</p>
            )
          )}
          {quoteState.editable &&
            quoteState.activeIds.has(q.artifact_id) &&
            quoteState.conflictingVendorIds.has(q.payload.quote.vendor_id) && (
              <p>
                More than one version from this vendor is active. Review the
                sources and reject the version that should no longer be
                considered before approval.
              </p>
            )}
          <p>
            <small>
              Valid until:{" "}
              {q.payload.quote.valid_until
                ? formatDateTime(q.payload.quote.valid_until)
                : "Not stated — clarification required"}
            </small>
          </p>
          <p>
            {q.payload.extraction?.inclusions_evidence ||
              "Inclusions need confirmation"}
          </p>
          <p>
            {q.payload.extraction?.exclusions_evidence ||
              "Exclusions need confirmation"}
          </p>
          {showSource && (
            <button
              className="text-button"
              onClick={() => showSource(q.artifact_id)}
            >
              Read source
            </button>
          )}
          {quoteState.editable && quoteState.activeIds.has(q.artifact_id) && (
            <div className="request-actions">
              <button
                className="secondary"
                disabled={busy || !notes.trim()}
                onClick={() =>
                  command("clarify_quote", { quote_id: q.artifact_id })
                }
              >
                Request missing information
              </button>
              <button
                className="secondary"
                disabled={busy || !notes.trim()}
                onClick={() =>
                  command("reject_quote", { quote_id: q.artifact_id })
                }
              >
                Reject this version
              </button>
            </div>
          )}
        </div>
      ))}
      {canReschedule && (
        <button
          className="secondary"
          disabled={busy || !notes.trim()}
          onClick={() => command("meeting_reschedule")}
        >
          Arrange a replacement meeting
        </button>
      )}
      {canRevise && (
        <button
          className="secondary"
          disabled={busy || !notes.trim()}
          onClick={() => command("meeting_revise_decision")}
        >
          Revisit this decision in a new meeting
        </button>
      )}
      {canReassign && (
        <details>
          <summary>Change an action owner or due date</summary>
          <label>
            Action
            <select
              value={actionId}
              onChange={(e) => selectAssignment(e.target.value)}
            >
              <option value="">Select an action</option>
              {actions.map((a: any) => (
                <option key={a.action_id} value={a.action_id}>
                  {a.description}
                </option>
              ))}
            </select>
          </label>
          <label>
            Owner
            <select value={owner} onChange={(e) => setOwner(e.target.value)}>
              <option value="">Select a member</option>
              {directory.map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
          </label>
          <DateTimeField
            label="Due date"
            value={due}
            onChange={setDue}
            disabled={busy}
          />
          <button
            disabled={
              busy ||
              !notes.trim() ||
              !actionId ||
              !owner ||
              !validDateTime(due)
            }
            onClick={() =>
              command("meeting_reassign", {
                action_id: actionId,
                owner_participant_id: owner,
                due_at: toUtcDateTime(due),
              })
            }
          >
            Save assignment
          </button>
        </details>
      )}
      {error && (
        <p role="alert" className="banner error">
          {error}
        </p>
      )}
    </section>
  );
}

export function ChannelSettings({ api }: { api: Api }) {
  const [data, setData] = useState<any>(null),
    [reviews, setReviews] = useState<any[]>([]),
    [people, setPeople] = useState<string[]>([]);
  const [error, setError] = useState(""),
    [notes, setNotes] = useState(""),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(true);
  async function load() {
    try {
      const [rules, mail, directory] = await Promise.all([
        api("/appointment-rules"),
        api("/inbound-reviews"),
        api("/directory"),
      ]);
      setData({
        ...rules,
        rules: rules.rules || {
          weekdays: [0, 1, 2, 3, 4],
          start_hour: 9,
          end_hour: 17,
          minimum_notice_hours: 24,
          access_instructions: "",
          access_actor_id: null,
        },
      });
      setReviews(mail);
      setPeople(directory.residents);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load();
  }, []);
  async function save() {
    if (
      !data.rules.weekdays.length ||
      !Number.isInteger(data.rules.start_hour) ||
      !Number.isInteger(data.rules.end_hour) ||
      data.rules.start_hour < 0 ||
      data.rules.end_hour > 24 ||
      data.rules.start_hour >= data.rules.end_hour ||
      !Number.isInteger(data.rules.minimum_notice_hours) ||
      data.rules.minimum_notice_hours < 1 ||
      data.rules.minimum_notice_hours > 168
    ) {
      setError(
        "Choose at least one working day, a valid start and end hour, and 1–168 hours of notice.",
      );
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api("/appointment-rules", {
        method: "PUT",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          action: "configure",
          expected_version: data.version + 1,
          data: data.rules,
          notes,
        }),
      });
      await load();
      setError("Appointment rules saved.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function review(id: string, action: string) {
    setBusy(true);
    setError("");
    try {
      await api(`/inbound-reviews/${id}/commands`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ action, expected_version: 1, notes }),
      });
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function field(key: string, value: any) {
    setData({ ...data, rules: { ...data.rules, [key]: value } });
  }
  return (
    <section className="channel-settings">
      <h2>Visits and communication</h2>
      <TelegramGroup api={api} />
      {data && (
        <details>
          <summary>Approved appointment hours and access</summary>
          <p>Property time zone: {data.timezone}</p>
          <p>
            No automatic appointment is confirmed until these rules are saved.
          </p>
          <fieldset>
            <legend>Working days</legend>
            {[
              "Monday",
              "Tuesday",
              "Wednesday",
              "Thursday",
              "Friday",
              "Saturday",
              "Sunday",
            ].map((day, i) => (
              <label className="check-line" key={day}>
                <input
                  type="checkbox"
                  checked={data.rules.weekdays.includes(i)}
                  onChange={(e) =>
                    field(
                      "weekdays",
                      e.target.checked
                        ? [...data.rules.weekdays, i]
                        : data.rules.weekdays.filter((d: number) => d !== i),
                    )
                  }
                />
                {day}
              </label>
            ))}
          </fieldset>
          <label>
            Start hour
            <input
              type="number"
              min={0}
              max={23}
              value={data.rules.start_hour}
              onChange={(e) => field("start_hour", +e.target.value)}
            />
          </label>
          <label>
            End hour
            <input
              type="number"
              min={1}
              max={24}
              value={data.rules.end_hour}
              onChange={(e) => field("end_hour", +e.target.value)}
            />
          </label>
          <label>
            Minimum notice (hours)
            <input
              type="number"
              min={1}
              max={168}
              value={data.rules.minimum_notice_hours}
              onChange={(e) => field("minimum_notice_hours", +e.target.value)}
            />
          </label>
          <label>
            Access instructions
            <textarea
              value={data.rules.access_instructions}
              onChange={(e) => field("access_instructions", e.target.value)}
            />
          </label>
          <label>
            Person who must confirm access
            <select
              value={data.rules.access_actor_id || ""}
              onChange={(e) => field("access_actor_id", e.target.value || null)}
            >
              <option value="">No personal confirmation required</option>
              {people.map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
          </label>
          <button
            disabled={busy || !data.rules.access_instructions.trim()}
            onClick={save}
          >
            Save appointment rules
          </button>
        </details>
      )}
      <h3>Mail needing review</h3>
      <label>
        Review or configuration note
        <textarea value={notes} onChange={(e) => setNotes(e.target.value)} />
      </label>
      {loading && <p role="status">Loading mail review queue…</p>}
      {!loading && !error && !reviews.length && <p>No messages need review.</p>}
      {reviews.map((r) => (
        <article className="quote" key={r.artifact_id}>
          <strong>{r.payload.reason.replaceAll("_", " ")}</strong>
          <p>{r.payload.attachments?.join(", ")}</p>
          <button
            className="secondary"
            disabled={busy || !notes.trim()}
            onClick={() => review(r.artifact_id, "dismiss")}
          >
            Close with reason
          </button>
          {r.payload.reason !== "sender_authentication_or_content_check" && (
            <>
              <button
                className="secondary"
                disabled={busy || !notes.trim()}
                onClick={() => review(r.artifact_id, "request_replacement")}
              >
                Request replacement
              </button>
              <button
                className="secondary"
                disabled={busy || !notes.trim()}
                onClick={() => review(r.artifact_id, "reprocess")}
              >
                Reprocess verified source
              </button>
            </>
          )}
        </article>
      ))}
      {error && (
        <p role="status" className="banner">
          {error}
        </p>
      )}
    </section>
  );
}
