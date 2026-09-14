import { formatDate, formatDateTime } from "./dateTime";
import { useEffect, useRef, useState } from "react";
import { meetingActionState } from "./continuityState";
import { DateTimeField } from "./DateTimeField";
import { MeetingPreparation } from "./MeetingPreparation";
import {
  DISPLAY_TIME_LABEL,
  DISPLAY_TIME_ZONE,
  retryMeetingSlots,
  toUtcDateTime,
  validDateTime,
} from "./dateTime";
type Props = {
  caseId: string;
  version: number;
  assignments?: { payload: any }[];
  api: (path: string, options?: RequestInit) => Promise<any>;
  command: (action: string, accepted?: boolean, data?: any) => Promise<void>;
  notes: string;
  setNotes: (value: string) => void;
  busy: boolean;
  resident?: boolean;
  readOnly?: boolean;
  tasks?: { kind: string; task_id?: string }[];
};
export function MeetingPanel({
  caseId,
  version,
  assignments = [],
  api,
  command,
  notes,
  setNotes,
  busy,
  resident = false,
  readOnly = false,
  tasks,
}: Props) {
  const [meeting, setMeeting] = useState<any>(null),
    [people, setPeople] = useState<string[]>([]),
    [participants, setParticipants] = useState<string[]>([]),
    [time, setTime] = useState(""),
    [quorum, setQuorum] = useState(2),
    [error, setError] = useState(""),
    [decisions, setDecisions] = useState<string[]>([]),
    [actions, setActions] = useState<string[]>([]),
    [availability, setAvailability] = useState<string[]>([]);
  const [loading, setLoading] = useState(true),
    [savingAvailability, setSavingAvailability] = useState(false),
    [availabilityNotice, setAvailabilityNotice] = useState("");
  const availabilityDirty = useRef(false),
    currentSchedule = useRef<string | null>(null);
  const [retryStart, setRetryStart] = useState(""),
    [retryEnd, setRetryEnd] = useState("");
  function retrySlots() {
    return retryMeetingSlots(retryStart, retryEnd);
  }
  const [assets, setAssets] = useState<{ asset_id: string; label: string }[]>(
    [],
  );
  const [procurementAsset, setProcurementAsset] = useState("");
  useEffect(() => {
    let cancelled = false;
    api("/cases/" + caseId + "/meeting")
      .then((next) => {
        if (cancelled) return;
        const changedRound = currentSchedule.current !== next.schedule_id;
        if (changedRound || !availabilityDirty.current) {
          setAvailability(next.available_slot_ids || []);
          availabilityDirty.current = false;
        }
        if (changedRound) setAvailabilityNotice("");
        currentSchedule.current = next.schedule_id;
        setMeeting(next);
        setError("");
      })
      .catch((error) => {
        if (cancelled) return;
        if (error.status === 404) setMeeting(null);
        else setError(error.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [caseId, version]);
  useEffect(() => {
    let cancelled = false;
    if (!resident) {
      api("/directory")
        .then((d) => {
          if (cancelled) return;
          setPeople(d.residents);
          setParticipants(d.managers);
          setAssets(d.assets || []);
        })
        .catch((error) => {
          if (!cancelled) setError(error.message);
        });
    }
    return () => {
      cancelled = true;
    };
  }, [caseId, resident]);
  const records = meeting?.records || {},
    preparation = meeting?.preparation?.payload,
    candidates = records["meeting_decision_candidates.v1"]?.at(-1)?.payload,
    confirmed = records["meeting_decision.v1"]?.at(-1)?.payload,
    packet = records["meeting_packet.v1"]?.at(-1)?.payload,
    completions = records["meeting_action_completion.v1"] || [];
  const agreedActions =
    confirmed?.action_items.map((action: any) => ({
      ...action,
      ...assignments
        .filter((record) => record.payload.action_id === action.action_id)
        .at(-1)?.payload,
    })) || [];
  const canManage = !resident && !readOnly;
  const hasTask = (kind: string) =>
    tasks === undefined || tasks.some((task) => task.kind === kind);
  const roundNeedsReplacement = tasks?.some(
    (task) => task.kind === "meeting_reschedule",
  );
  const canRespond = !readOnly && !roundNeedsReplacement;
  const canEditActions = canManage && hasTask("meeting_action");
  const canVerify =
    canManage &&
    hasTask("meeting_verify") &&
    agreedActions.every((action: any) =>
      completions.some(
        (item: any) => item.payload.action_id === action.action_id,
      ),
    );
  const hasCompletionAction = agreedActions.some(
    (action: any) =>
      meetingActionState(
        action.action_id,
        completions,
        meeting?.maintenance || [],
        canEditActions,
        meeting?.procurement_requests || [],
      ).canComplete,
  );
  const hasMaintenanceAction = agreedActions.some(
    (action: any) =>
      meetingActionState(
        action.action_id,
        completions,
        meeting?.maintenance || [],
        canEditActions,
        meeting?.procurement_requests || [],
      ).canRequestMaintenance,
  );
  async function submitAvailability() {
    setSavingAvailability(true);
    setAvailabilityNotice("");
    setError("");
    try {
      await api("/cases/" + caseId + "/availability", {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          expected_version: meeting.version,
          schedule_id: meeting.schedule_id,
          available_slot_ids: availability,
        }),
      });
      availabilityDirty.current = false;
      setMeeting(await api("/cases/" + caseId + "/meeting"));
      setAvailabilityNotice(
        "Availability saved. Steward will check the quorum.",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSavingAvailability(false);
    }
  }
  return (
    <section className="meeting-panel">
      <h3>Community decision</h3>
      <p className="time-zone-note">All meeting times: {DISPLAY_TIME_LABEL}</p>
      {error && (
        <p role="alert" className="banner error">
          {error}
        </p>
      )}
      {loading && <p role="status">Loading meeting details…</p>}
      {availabilityNotice && <p role="status">{availabilityNotice}</p>}
      {readOnly && (
        <p>
          This meeting record is preserved for reference. Its agreed decisions
          and recorded evidence remain available below.
        </p>
      )}
      {!resident && (preparation || packet) && (
        <MeetingPreparation
          key={preparation?.plan_id || packet?.packet_id}
          preparation={preparation}
          agenda={preparation?.agenda || packet?.agenda}
          caseId={caseId}
          api={api}
        />
      )}
      {!loading &&
        !error &&
        !meeting?.schedule &&
        canManage &&
        hasTask("meeting_schedule") && (
          <>
            <p>
              Offer a meeting time. Participants answer from their own account.
            </p>
            <DateTimeField
              label="Proposed time"
              value={time}
              onChange={setTime}
              disabled={busy}
            />
            <label>
              Required quorum
              <input
                type="number"
                min="1"
                max={participants.length}
                value={quorum}
                onChange={(e) => setQuorum(Number(e.target.value))}
              />
            </label>
            <fieldset className="participant-picker">
              <legend>Eligible participants</legend>
              {people.map((p) => (
                <label className="check-line" key={p}>
                  <input
                    type="checkbox"
                    checked={participants.includes(p)}
                    onChange={(e) =>
                      setParticipants(
                        e.target.checked
                          ? [...participants, p]
                          : participants.filter((x) => x !== p),
                      )
                    }
                  />
                  {p}
                </label>
              ))}
            </fieldset>
            <fieldset>
              <legend>Optional retry window</legend>
              <p>
                Steward may offer this time on later days in the window, for at
                most two new rounds.
              </p>
              <DateTimeField
                label="Window starts"
                value={retryStart}
                onChange={setRetryStart}
                disabled={busy}
              />
              <DateTimeField
                label="Window ends"
                value={retryEnd}
                onChange={setRetryEnd}
                disabled={busy}
              />
            </fieldset>
            <label>
              Scheduling note
              <textarea
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
              />
            </label>
            <button
              disabled={
                busy ||
                !validDateTime(time) ||
                !notes.trim() ||
                quorum < 1 ||
                participants.length < quorum ||
                !!retryStart !== !!retryEnd ||
                (!!retryStart &&
                  (!validDateTime(retryStart) || !validDateTime(retryEnd))) ||
                (!!retryStart && retryStart > retryEnd)
              }
              onClick={() =>
                command("meeting_schedule", true, {
                  retry_slots: retrySlots(),
                  policy: {
                    timezone: DISPLAY_TIME_ZONE,
                    quorum,
                    eligible_participant_ids: participants,
                  },
                  slots: [
                    {
                      slot_id: "proposed-1",
                      starts_at: toUtcDateTime(time),
                    },
                  ],
                })
              }
            >
              Offer meeting time
            </button>
          </>
        )}
      {meeting?.schedule && !packet && (
        <>
          <p>
            {roundNeedsReplacement
              ? "This response round has ended. Steward will offer approved replacement times or ask management to choose new times."
              : `Waiting for the configured quorum of ${meeting.schedule.policy.quorum} participants.`}
          </p>
          {meeting.schedule.slots.map((s: any) => (
            <label key={s.slot_id} className="check-line">
              <input
                type="checkbox"
                checked={availability.includes(s.slot_id)}
                disabled={savingAvailability || !canRespond}
                onChange={(e) => {
                  availabilityDirty.current = true;
                  setAvailability(
                    e.target.checked
                      ? [...availability, s.slot_id]
                      : availability.filter((x) => x !== s.slot_id),
                  );
                }}
              />
              {formatDateTime(s.starts_at, {
                day: "numeric",
                month: "short",
                year: "numeric",
                hour: "2-digit",
                minute: "2-digit",
              })}
            </label>
          ))}
          {canRespond && (
            <button
              onClick={submitAvailability}
              disabled={busy || savingAvailability}
            >
              {savingAvailability ? "Saving…" : "Save my availability"}
            </button>
          )}
        </>
      )}
      {resident && packet && (
        <div className="reason">
          <h3>Meeting preparation</h3>
          <p>{packet.agenda?.summary || "Your meeting packet is ready."}</p>
          {packet.agenda?.agenda_items?.map((a: any, i: number) => (
            <div key={i}>
              <strong>{a.title}</strong>
              <p>{a.detail}</p>
            </div>
          ))}
        </div>
      )}
      {canManage && hasTask("meeting_minutes") && packet && !candidates && (
        <>
          <label>
            Written minutes
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Record the agreed decisions, named owners and due dates."
            />
          </label>
          <DateTimeField
            label="Meeting held at"
            value={time}
            onChange={setTime}
            disabled={busy}
          />
          <label>
            Participants present
            <input
              type="number"
              value={quorum}
              min="1"
              onChange={(e) => setQuorum(Number(e.target.value))}
            />
          </label>
          <button
            disabled={busy || !notes.trim() || !validDateTime(time)}
            onClick={() =>
              command("meeting_minutes", true, {
                held_at: toUtcDateTime(time),
                participant_count: quorum,
              })
            }
          >
            Prepare decisions for review
          </button>
        </>
      )}
      {candidates && !confirmed && canManage && hasTask("meeting_confirm") && (
        <>
          <p>Only the selected decisions and tasks become confirmed records.</p>
          {candidates.decisions.map((d: any) => (
            <label className="check-line" key={d.decision_id}>
              <input
                type="checkbox"
                checked={decisions.includes(d.decision_id)}
                onChange={(e) =>
                  setDecisions(
                    e.target.checked
                      ? [...decisions, d.decision_id]
                      : decisions.filter((x) => x !== d.decision_id),
                  )
                }
              />
              <span>
                {d.statement}
                <small>Source: “{d.evidence_excerpt}”</small>
              </span>
            </label>
          ))}
          {candidates.action_items.map((a: any) => (
            <label className="check-line" key={a.action_id}>
              <input
                type="checkbox"
                checked={actions.includes(a.action_id)}
                onChange={(e) =>
                  setActions(
                    e.target.checked
                      ? [...actions, a.action_id]
                      : actions.filter((x) => x !== a.action_id),
                  )
                }
              />
              <span>
                {a.description}
                <small>
                  {a.owner_participant_id} · {formatDate(a.due_at)}
                </small>
              </span>
            </label>
          ))}
          <label>
            Confirmation note
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
            />
          </label>
          <button
            disabled={busy || !decisions.length || !notes.trim()}
            onClick={() =>
              command("meeting_confirm", true, {
                decision_ids: decisions,
                action_ids: actions,
              })
            }
          >
            Confirm selected decisions
          </button>
        </>
      )}
      {confirmed && (
        <>
          <h3>Confirmed decisions</h3>
          {confirmed.decisions.map((decision: any) => (
            <div className="reason" key={decision.decision_id}>
              <p>{decision.statement}</p>
              {decision.evidence_excerpt && (
                <details>
                  <summary>Decision evidence</summary>
                  <p>{decision.evidence_excerpt}</p>
                </details>
              )}
            </div>
          ))}
          <h3>Agreed actions</h3>
          {agreedActions.length === 0 && (
            <p>No implementation tasks were recorded for this decision.</p>
          )}
          {agreedActions.map((a: any) => {
            const state = meetingActionState(
              a.action_id,
              completions,
              meeting.maintenance || [],
              canEditActions,
              meeting.procurement_requests || [],
            );
            return (
              <div className="reason" key={a.action_id}>
                <strong>{a.description}</strong>
                <p>
                  {a.owner_participant_id} · Due {formatDate(a.due_at)}
                </p>
                {a.reason && <p>Assignment updated: {a.reason}</p>}
                {meeting.maintenance
                  ?.filter((link: any) => link.action_id === a.action_id)
                  .map((link: any) => (
                    <p key={link.child_case_id}>
                      Linked maintenance:{" "}
                      {link.verified
                        ? "Verified outcome"
                        : "Work is in progress"}
                    </p>
                  ))}
                {state.completion ? (
                  <>
                    <span className="badge">Completion recorded</span>
                    <p>{state.completion.payload.notes}</p>
                    <small>
                      {state.completion.payload.actor_label} ·{" "}
                      {formatDateTime(state.completion.payload.completed_at)}
                    </small>
                  </>
                ) : (
                  canEditActions && (
                    <>
                      {state.waitingForMaintenance && (
                        <p>
                          {state.pendingRequest
                            ? "Maintenance has been requested. Steward is opening the linked case; completion will wait for its verified outcome."
                            : "Completion can be recorded after the linked maintenance outcome is verified."}
                        </p>
                      )}
                      {state.canComplete && (
                        <button
                          disabled={busy || !notes.trim()}
                          onClick={() =>
                            command("meeting_action", true, {
                              action_id: a.action_id,
                            })
                          }
                        >
                          Record completion
                        </button>
                      )}
                      {state.canRequestMaintenance && (
                        <button
                          className="secondary"
                          disabled={busy || !notes.trim() || !procurementAsset}
                          onClick={() =>
                            command("meeting_procurement", true, {
                              action_id: a.action_id,
                              asset_id: procurementAsset,
                            })
                          }
                        >
                          Request linked maintenance
                        </button>
                      )}
                    </>
                  )
                )}
              </div>
            );
          })}
          {canManage &&
            (hasCompletionAction || hasMaintenanceAction || canVerify) && (
              <>
                {hasMaintenanceAction && (
                  <label>
                    Asset, if this action requires maintenance
                    <select
                      value={procurementAsset}
                      onChange={(e) => setProcurementAsset(e.target.value)}
                    >
                      <option value="">Choose an asset</option>
                      {assets.map((a) => (
                        <option value={a.asset_id} key={a.asset_id}>
                          {a.label}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <label>
                  Completion or outcome evidence
                  <textarea
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                  />
                </label>
                {canVerify && (
                  <div className="button-row">
                    <button
                      disabled={busy || !notes.trim()}
                      onClick={() =>
                        command("meeting_verify", true, {
                          outcome: "confirmed",
                        })
                      }
                    >
                      Verify outcome
                    </button>
                    <button
                      className="secondary"
                      disabled={busy || !notes.trim()}
                      onClick={() =>
                        command("meeting_verify", false, {
                          outcome: "rejected",
                        })
                      }
                    >
                      Needs revision
                    </button>
                  </div>
                )}
              </>
            )}
        </>
      )}
    </section>
  );
}
