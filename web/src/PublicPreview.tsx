import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  Building2,
  CalendarDays,
  Check,
  CheckCircle2,
  ChevronRight,
  Clock3,
  FileText,
  History,
  MessageCircle,
  RotateCcw,
  ShieldCheck,
  Users,
  Wrench,
  X,
} from "lucide-react";
import { BrandLogo } from "./BrandLogo";
import "./PublicPreview.css";

type ScenarioId = "parking" | "elevator" | "warranty";
type Source = { id: string; title: string; context: string; text: string };
type Scenario = {
  id: ScenarioId;
  title: string;
  category: string;
  description: string;
  steps: string[];
  sources: Source[];
};
type SampleState = {
  invitation: boolean;
  availability: string[];
  responded: boolean;
  decision: boolean;
  task: boolean;
  approved: boolean;
  delivered: boolean;
  appointment: boolean;
  verified: boolean;
  rework: boolean;
  warranty: boolean;
};
const initialState = (): SampleState => ({
  invitation: false,
  availability: [],
  responded: false,
  decision: false,
  task: false,
  approved: false,
  delivered: false,
  appointment: false,
  verified: false,
  rework: false,
  warranty: false,
});
const scenarios: Scenario[] = [
  {
    id: "parking",
    title: "A fairer place to park",
    category: "Community decision",
    description:
      "A conversation becomes a meeting, a shared rule and a promise someone owns.",
    steps: [
      "Conversation",
      "Meeting preparation",
      "Resident availability",
      "Verified decision",
      "Follow-through",
    ],
    sources: [
      {
        id: "NG-P-01",
        title: "Resident conversation",
        context: "Illustrative Telegram message · Maya R. · 11 September",
        text: "Hi everyone, the visitor parking spaces at Northgate are often occupied overnight, leaving no room for guests. Some residents prefer a 24-hour parking limit, while others want 72 hours. Could management arrange a residents’ meeting to discuss these options, agree on a shared rule, and decide how it should be communicated?",
      },
      {
        id: "NG-P-02",
        title: "Previous parking discussion",
        context: "Sample community memory · 12 June",
        text: "Residents asked for clearer visitor parking signs. No maximum stay was approved. Two residents raised concerns about overnight carers. A popular opinion in the conversation was not recorded as an adopted rule.",
      },
      {
        id: "NG-P-03",
        title: "Draft written minutes",
        context: "Sample meeting record · 17 September",
        text: "Attendees agreed to trial a 24-hour visitor limit for 30 days, with a registered exception for carers. Simon will publish the rule and install a notice by 20 September. The trial will be reviewed on 20 October. These draft minutes require manager verification before becoming the current decision.",
      },
    ],
  },
  {
    id: "elevator",
    title: "Keep Northgate moving",
    category: "Maintenance",
    description:
      "Compare complete quotes, authorise within policy, then verify the repair.",
    steps: [
      "Report & memory",
      "Quote comparison",
      "Service order",
      "Appointment",
      "Verified result",
    ],
    sources: [
      {
        id: "NG-E-01",
        title: "Lift fault report",
        context: "Illustrative Telegram message · Daniel K.",
        text: "The west lift doors keep opening again on the ground floor. It happened three times this morning. The other lift is working, but it’s difficult for people with pushchairs to get around.",
      },
      {
        id: "NG-E-02",
        title: "Northgate Lift Care quote",
        context:
          "Sample supplier email · Quote NLC-204 · Valid through 22 September",
        text: "Total: USD 420, inclusive of tax. Scope: inspect the west lift door sensor, clean and recalibrate it, then complete 20 test cycles. Labour and standard consumables included. Replacement hardware excluded and requires a separate quote. Available 18 September, 10:00–12:00 Europe/Istanbul. Workmanship warranty: 30 days.",
      },
      {
        id: "NG-E-03",
        title: "Metro Lift Services quote",
        context:
          "Sample supplier email · Quote MLS-118 · Valid through 22 September",
        text: "Total: USD 380, inclusive of tax. Same inspection, cleaning, calibration and 20 test cycles. Labour and standard consumables included; hardware replacement excluded. Available 22 September, 14:00–16:00 Europe/Istanbul. Workmanship warranty: 30 days.",
      },
      {
        id: "NG-E-04",
        title: "Repair history and spending policy",
        context: "Sample operational evidence · Policy revision 3",
        text: "Northgate Lift Care: 4 verified comparable jobs, 0 repeat faults in this sample. Metro Lift Services: 2 verified comparable jobs, 1 repeat fault. These small samples are descriptive, not a guarantee. Category approval threshold: USD 300. Available maintenance budget: USD 900. Work must be approved above the threshold; existing reservations count against available funds.",
      },
    ],
  },
  {
    id: "warranty",
    title: "Remember the last repair",
    category: "Repeat fault & warranty",
    description:
      "A familiar fault should start with the previous promise, before another paid order.",
    steps: [
      "Repeated fault",
      "Linked history",
      "Warranty check",
      "Rework follow-up",
      "Resident verification",
    ],
    sources: [
      {
        id: "NG-W-01",
        title: "A new report, a familiar symptom",
        context: "Illustrative Telegram message · Priya S. · 26 September",
        text: "The west lift doors are reopening on the ground floor again. It looks like the same issue that was fixed last week. Could someone check whether that repair is still covered?",
      },
      {
        id: "NG-W-02",
        title: "Previous verified repair",
        context: "Sample linked case NG-E-204 · 18 September",
        text: "Northgate Lift Care cleaned and recalibrated the west lift door sensor for USD 420. A resident confirmed that the doors worked normally after the visit. The agreed workmanship warranty runs through 18 October. A previous successful verification remains on the record even when a fault later returns.",
      },
      {
        id: "NG-W-03",
        title: "Warranty response",
        context: "Sample supplier reply · 27 September",
        text: "We can revisit the west lift on 28 September, 10:00–12:00 Europe/Istanbul, to inspect the repeated door-sensor fault under the existing workmanship warranty. No call-out fee. If we identify an unrelated part failure, we will submit a separate quote before doing chargeable work.",
      },
    ],
  },
];

function ScenarioIcon({ id, size = 20 }: { id: ScenarioId; size?: number }) {
  return id === "parking" ? (
    <Users size={size} />
  ) : id === "elevator" ? (
    <Wrench size={size} />
  ) : (
    <History size={size} />
  );
}

function EvidenceDrawer({
  source,
  close,
}: {
  source: Source | null;
  close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (source) dialog.current?.showModal();
    else dialog.current?.close();
  }, [source]);
  return (
    <dialog
      ref={dialog}
      className="pp-evidence"
      aria-labelledby="pp-source-title"
      onCancel={close}
      onClose={close}
      onClick={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div className="pp-evidence-head">
        <span className="pp-kicker">Source evidence</span>
        <button
          type="button"
          className="pp-icon-button"
          aria-label="Close source evidence"
          onClick={close}
        >
          <X size={21} />
        </button>
      </div>
      {source && (
        <>
          <span className="pp-badge">Synthetic sample · {source.id}</span>
          <h2 id="pp-source-title">{source.title}</h2>
          <p className="pp-muted">{source.context}</p>
          <blockquote>{source.text}</blockquote>
          <p className="pp-evidence-note">
            This record is part of the public interactive sample. It is not a
            live message, supplier offer or operational instruction.
          </p>
        </>
      )}
    </dialog>
  );
}

function Panel({
  title,
  children,
  className = "",
}: {
  title: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`pp-panel ${className}`}>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

export default function PublicPreview({
  onJudgeAccess,
  onMemberSignIn,
}: {
  onJudgeAccess: () => void;
  onMemberSignIn: () => void;
}) {
  const [selected, setSelected] = useState<ScenarioId>("parking");
  const [role, setRole] = useState<"manager" | "resident">("manager");
  const [steps, setSteps] = useState<Record<ScenarioId, number>>({
    parking: 1,
    elevator: 1,
    warranty: 1,
  });
  const [states, setStates] = useState<Record<ScenarioId, SampleState>>({
    parking: initialState(),
    elevator: initialState(),
    warranty: initialState(),
  });
  const [source, setSource] = useState<Source | null>(null);
  const [notice, setNotice] = useState("");
  const scenario = scenarios.find((item) => item.id === selected)!;
  const step = steps[selected];
  const state = states[selected];
  function update(
    change: Partial<SampleState>,
    message: string,
    nextStep?: number,
  ) {
    setStates((current) => ({
      ...current,
      [selected]: { ...current[selected], ...change },
    }));
    setNotice(`${message} Saved for this preview only.`);
    if (nextStep !== undefined)
      setSteps((current) => ({ ...current, [selected]: nextStep }));
  }
  function choose(id: ScenarioId) {
    setSelected(id);
    setNotice("");
  }
  function reset() {
    setStates({
      parking: initialState(),
      elevator: initialState(),
      warranty: initialState(),
    });
    setSteps({ parking: 1, elevator: 1, warranty: 1 });
    setNotice(
      "All sample actions have been reset. No community records were changed.",
    );
  }
  const sourceButton = (index: number, label?: string) => (
    <button
      type="button"
      className="pp-source-link"
      onClick={() => setSource(scenario.sources[index])}
    >
      <FileText size={15} />
      {label ?? scenario.sources[index].title}
      <ChevronRight size={14} />
    </button>
  );
  const status = (done: boolean, text: string) =>
    done && (
      <p className="pp-success">
        <CheckCircle2 size={18} />
        {text}
      </p>
    );
  const next = (label: string, value: number) => (
    <button
      type="button"
      className="pp-secondary"
      onClick={() => setSteps((current) => ({ ...current, [selected]: value }))}
    >
      {label}
      <ArrowRight size={16} />
    </button>
  );
  const availability = (
    <>
      <fieldset className="pp-slots">
        <legend>
          Which time works for you? <span>Europe/Istanbul · UTC+3</span>
        </legend>
        {["17 Sep · 18:30–19:15", "18 Sep · 19:00–19:45"].map((slot) => (
          <label key={slot}>
            <input
              type="checkbox"
              checked={state.availability.includes(slot)}
              onChange={(event) =>
                update(
                  {
                    availability: event.target.checked
                      ? [...state.availability, slot]
                      : state.availability.filter((item) => item !== slot),
                    responded: false,
                  },
                  "Availability selection updated.",
                )
              }
            />
            {slot}
          </label>
        ))}
      </fieldset>
      <button
        type="button"
        className="pp-primary"
        onClick={() =>
          update(
            { responded: true },
            state.availability.length
              ? "Your sample availability has been recorded."
              : "Your sample response has been recorded: neither time works.",
          )
        }
      >
        {state.responded
          ? "Update sample response"
          : "Share sample availability"}
        <ArrowRight size={16} />
      </button>
      {status(
        state.responded,
        "Response saved. A meeting is confirmed only when the configured quorum is met.",
      )}
    </>
  );
  const verification = (
    <>
      <p>
        The supplier reports the work is complete. The case stays open until an
        authorised person checks the result.
      </p>
      <div className="pp-actions">
        <button
          type="button"
          className="pp-primary"
          onClick={() =>
            update(
              { verified: true, rework: false },
              "The sample result is verified and the case is closed.",
            )
          }
        >
          <CheckCircle2 size={17} />
          Confirm the issue is resolved
        </button>
        <button
          type="button"
          className="pp-secondary"
          onClick={() =>
            update(
              { verified: false, rework: true },
              "The sample result was rejected. A rework follow-up is now required.",
            )
          }
        >
          The issue is still happening
        </button>
      </div>
      {status(
        state.verified,
        "Verified by the sample resident. The outcome is now part of community memory.",
      )}
      {state.rework && (
        <p className="pp-warning">
          Rework required · The case remains open. The previous work and your
          response stay on the timeline.
        </p>
      )}
    </>
  );

  function managerContent() {
    if (selected === "parking") {
      if (step === 0)
        return (
          <>
            <Panel title="It starts where residents already talk">
              <div className="pp-message">
                <span>
                  <MessageCircle size={17} />
                  Maya R. · Community group
                </span>
                <p>
                  “Visitor spaces are occupied overnight. Some of us prefer a
                  24-hour limit, others want 72 hours. Could we discuss a shared
                  rule?”
                </p>
              </div>
              {sourceButton(0)}
            </Panel>
            <Panel title="What Steward picks up">
              <p>
                This needs a community decision. There is no agreed parking
                limit to enforce yet.
              </p>
              <div className="pp-fact-row">
                <span>Next step</span>
                <strong>Prepare a meeting brief</strong>
              </div>
              <div className="pp-fact-row">
                <span>Who is needed</span>
                <strong>Community manager</strong>
              </div>
              {next("See the prepared brief", 1)}
            </Panel>
          </>
        );
      if (step === 1)
        return (
          <>
            <Panel title="A useful meeting, before the invitation">
              <p>
                Choose a fair visitor-parking rule, address exceptions and agree
                how residents will hear about it.
              </p>
              <ol className="pp-agenda">
                <li>Where are spaces being blocked, and when?</li>
                <li>Compare 24-hour and 72-hour limits.</li>
                <li>Agree exceptions for carers and longer visits.</li>
                <li>Assign communication, signage and a review date.</li>
              </ol>
              <div className="pp-options">
                <div>
                  <strong>24-hour limit</strong>
                  <p>
                    More turnover for guests. Needs an exception process for
                    longer visits.
                  </p>
                </div>
                <div>
                  <strong>72-hour limit</strong>
                  <p>
                    More flexibility for visitors. Fewer spaces become available
                    each day.
                  </p>
                </div>
              </div>
              {sourceButton(1, "Why an earlier discussion is not a decision")}
            </Panel>
            <Panel title="Needs your decision" className="pp-decision">
              <span className="pp-badge">Manager approval</span>
              <h4>Invite residents to decide together</h4>
              <p>
                Proposed quorum: 4 of 6 invited households. An invitation does
                not approve a parking policy or authorise spending.
              </p>
              <div className="pp-fact-row">
                <span>Budget impact</span>
                <strong>No spending authorised</strong>
              </div>
              <button
                type="button"
                className="pp-primary"
                onClick={() =>
                  update(
                    { invitation: true },
                    "The sample invitation is approved.",
                    2,
                  )
                }
              >
                Approve sample invitation
                <ArrowRight size={17} />
              </button>
              {status(state.invitation, "Sample invitation approved.")}
            </Panel>
          </>
        );
      if (step === 2)
        return (
          <>
            <Panel title="A small request for each resident">
              <p>
                Residents receive their own invitation and availability choices.
                Earlier scheduling rounds do not count toward new dates.
              </p>
              {availability}
            </Panel>
            <Panel title="A meeting needs a quorum">
              <div className="pp-quorum">
                <strong>
                  {state.responded && state.availability.length ? "4" : "3"}
                  <span> / 6</span>
                </strong>
                <p>
                  Illustrative available households
                  <br />4 required to confirm a time
                </p>
              </div>
              <p>
                When a round expires, Steward can offer new times within the
                manager’s permitted window, for up to two additional rounds.
              </p>
              {next("Explore the sample meeting outcome", 3)}
            </Panel>
          </>
        );
      if (step === 3)
        return (
          <>
            <Panel title="Written minutes become a verified decision">
              <span className="pp-badge">
                {state.decision
                  ? "Verified in this preview"
                  : "Draft · Needs verification"}
              </span>
              <h4>Trial a 24-hour visitor limit for 30 days</h4>
              <p>
                Include a registered exception for carers. Publish a clear
                notice, then review whether access improved.
              </p>
              {sourceButton(2)}
              <div className="pp-actions">
                <button
                  type="button"
                  className="pp-primary"
                  disabled={state.decision}
                  onClick={() =>
                    update(
                      { decision: true },
                      "The sample meeting decision is verified.",
                    )
                  }
                >
                  <Check size={17} />
                  {state.decision
                    ? "Sample decision verified"
                    : "Verify sample minutes"}
                </button>
                {state.decision && next("Follow the agreed task", 4)}
              </div>
            </Panel>
            <Panel title="What changes after verification">
              <ul className="pp-list">
                <li>The adopted rule is recorded with its source minutes.</li>
                <li>A task gets an owner, deadline and completion evidence.</li>
                <li>
                  A later policy change needs a linked, newly verified decision.
                </li>
              </ul>
              <p className="pp-callout">
                A popular opinion in chat never becomes policy on its own.
              </p>
            </Panel>
          </>
        );
      return (
        <>
          <Panel title="A promise with an owner">
            <span className="pp-badge">
              {state.task ? "Completed in this preview" : "Implementation task"}
            </span>
            <h4>Publish the parking notice</h4>
            <div className="pp-fact-row">
              <span>Owner</span>
              <strong>Simon · Community manager</strong>
            </div>
            <div className="pp-fact-row">
              <span>Due</span>
              <strong>20 September</strong>
            </div>
            <div className="pp-fact-row">
              <span>Required evidence</span>
              <strong>Published notice and resident update</strong>
            </div>
            <p>
              The sample includes a completed notice ready to attach. Confirming
              it records that evidence with the task.
            </p>
            <button
              type="button"
              className="pp-primary"
              disabled={state.task || !state.decision}
              onClick={() =>
                update(
                  { task: true },
                  "The notice evidence is recorded and the sample task is complete.",
                )
              }
            >
              <CheckCircle2 size={17} />
              {state.task
                ? "Sample task complete"
                : "Record completion evidence"}
            </button>
            {!state.decision && (
              <p className="pp-warning">
                Verify the sample minutes before completing this decision’s
                task.
              </p>
            )}
            {status(
              state.task,
              "The promise is complete. The trial review remains due on 20 October.",
            )}
          </Panel>
          <Panel title="The community remembers">
            <p>
              The conversation, verified decision and implementation evidence
              remain linked. A future parking dispute can begin with what was
              actually agreed.
            </p>
            {sourceButton(2)}
            {!state.decision && next("Review the draft minutes", 3)}
          </Panel>
        </>
      );
    }
    if (selected === "elevator") {
      if (step === 0)
        return (
          <>
            <Panel title="One issue, with useful context">
              <div className="pp-message">
                <span>
                  <MessageCircle size={17} />
                  Daniel K. · Community group
                </span>
                <p>
                  “The west lift doors keep opening again on the ground floor.
                  It happened three times this morning.”
                </p>
              </div>
              {sourceButton(0)}
              <p>
                Location and symptom are clear. Steward links the report to the
                west lift and checks comparable work before collecting offers.
              </p>
            </Panel>
            <Panel title="History informs the next action">
              <p>
                Previous verified outcomes provide context for supplier
                selection. Small sample sizes and differences in scope remain
                visible.
              </p>
              {sourceButton(3)}
              {next("Compare the sample offers", 1)}
            </Panel>
          </>
        );
      if (step === 1)
        return (
          <>
            <Panel title="Two complete offers, one clear trade-off">
              <div className="pp-table-wrap">
                <table>
                  <caption>
                    Illustrative quotes · USD · Same inspection and calibration
                    scope
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Supplier</th>
                      <th scope="col">Total</th>
                      <th scope="col">Available</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <th scope="row">
                        Northgate Lift Care<span>Recommended</span>
                      </th>
                      <td>$420</td>
                      <td>18 Sep</td>
                    </tr>
                    <tr>
                      <th scope="row">Metro Lift Services</th>
                      <td>$380</td>
                      <td>22 Sep</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p>
                <strong>Why Northgate?</strong> An earlier visit and a stronger
                record in this small, comparable sample justify the additional
                $40.
              </p>
              <p>
                <strong>What could change the choice?</strong> An earlier Metro
                appointment, a changed scope or new evidence about either
                supplier.
              </p>
              <div className="pp-source-group">
                {sourceButton(1, "Northgate quote")}
                {sourceButton(2, "Metro quote")}
                {sourceButton(3, "History & policy")}
              </div>
            </Panel>
            <Panel title="Needs your authorisation" className="pp-decision">
              <span className="pp-badge">Above the $300 policy threshold</span>
              <h4>Approve Northgate Lift Care · $420</h4>
              <div className="pp-fact-row">
                <span>Available budget</span>
                <strong>$900</strong>
              </div>
              <div className="pp-fact-row">
                <span>After reservation</span>
                <strong>$480</strong>
              </div>
              <p>
                Approval reserves funds. Quote validity, scope, policy and
                available budget must be checked again before sending an order.
              </p>
              <button
                type="button"
                className="pp-primary"
                onClick={() =>
                  update(
                    { approved: true },
                    "The sample offer is approved and $420 is reserved.",
                    2,
                  )
                }
              >
                Approve sample quote
                <ArrowRight size={17} />
              </button>
            </Panel>
          </>
        );
      if (step === 2)
        return (
          <>
            <Panel title="An order is not an appointment">
              <span className="pp-badge">
                {state.delivered
                  ? "Sample delivery recorded"
                  : state.approved
                    ? "Order ready in this preview"
                    : "Awaiting sample approval"}
              </span>
              <h4>West lift door-sensor service · $420</h4>
              <p>
                Scope, supplier, quote revision and authority stay attached to
                the order. Sending it still requires a supplier response and
                appointment agreement.
              </p>
              <button
                type="button"
                className="pp-primary"
                disabled={!state.approved || state.delivered}
                onClick={() =>
                  update(
                    { delivered: true },
                    "The sample order delivery is recorded.",
                    3,
                  )
                }
              >
                Record sample order delivery
                <ArrowRight size={17} />
              </button>
              {!state.approved && (
                <>
                  {next("Return to quote approval", 1)}
                  <p className="pp-warning">
                    Approve the quote before recording a sample order.
                  </p>
                </>
              )}
            </Panel>
            <Panel title="Every open case has a next step">
              <div className="pp-fact-row">
                <span>Waiting for</span>
                <strong>Supplier acceptance and time</strong>
              </div>
              <div className="pp-fact-row">
                <span>Owner</span>
                <strong>Northgate Lift Care</strong>
              </div>
              <div className="pp-fact-row">
                <span>Follow-up</span>
                <strong>Next working day</strong>
              </div>
              <p>
                If a delivery is uncertain, the order needs reconciliation
                before it can be sent again.
              </p>
            </Panel>
          </>
        );
      if (step === 3)
        return (
          <>
            <Panel title="A specific time, with access agreed">
              <span className="pp-badge">
                {state.appointment
                  ? "Confirmed in this preview"
                  : "Sample supplier proposal"}
              </span>
              <h4>18 September · 10:00–12:00</h4>
              <p>Europe/Istanbul · UTC+3</p>
              <ul className="pp-list">
                <li>Within the community’s working hours.</li>
                <li>Same approved fee and scope.</li>
                <li>Sample access contact: Simon, available on site.</li>
              </ul>
              <button
                type="button"
                className="pp-primary"
                disabled={!state.delivered || state.appointment}
                onClick={() =>
                  update(
                    { appointment: true },
                    "The sample appointment and access agreement are confirmed.",
                  )
                }
              >
                <CalendarDays size={17} />
                {state.appointment
                  ? "Sample appointment confirmed"
                  : "Confirm sample appointment"}
              </button>
              {!state.delivered && (
                <p className="pp-warning">
                  Record the sample order delivery first.
                </p>
              )}
              {state.appointment && next("Explore result verification", 4)}
            </Panel>
            <Panel title="Follow-up continues after scheduling">
              <p>
                A status check is due one hour after the appointment ends.
                Silence alone does not mean the supplier failed to attend.
              </p>
              <p>
                A changed fee or scope needs review. A new proposed date does
                not silently replace the confirmed appointment.
              </p>
              {!state.delivered && next("Review the service order", 2)}
            </Panel>
          </>
        );
      return (
        <>
          <Panel title="The person affected confirms the result">
            {state.appointment ? (
              verification
            ) : (
              <>
                <p>
                  Complete the sample order and appointment before verifying
                  this repair.
                </p>
                {next("Review the appointment", 3)}
              </>
            )}
          </Panel>
          <Panel title="A verified outcome becomes useful history">
            <p>
              Record what was done, whether it worked and the warranty period.
              If the fault returns, the next case can check that promise before
              preparing another paid order.
            </p>
            <button
              type="button"
              className="pp-source-link"
              onClick={() => choose("warranty")}
            >
              Explore a returning fault
              <ArrowRight size={16} />
            </button>
          </Panel>
        </>
      );
    }
    if (step === 0)
      return (
        <>
          <Panel title="The same symptom returns">
            <div className="pp-message">
              <span>
                <MessageCircle size={17} />
                Priya S. · Community group
              </span>
              <p>
                “The west lift doors are reopening again. It looks like the same
                issue that was fixed last week.”
              </p>
            </div>
            {sourceButton(0)}
          </Panel>
          <Panel title="Look back before spending again">
            <p>
              Steward connects the same asset and symptom to the recently
              verified repair. A returning fault is new evidence, even when the
              last visit appeared successful.
            </p>
            {next("Review the linked repair", 1)}
          </Panel>
        </>
      );
    if (step === 1)
      return (
        <>
          <Panel title="The earlier repair has a promise attached">
            <div className="pp-memory-link">
              <History size={24} />
              <div>
                <strong>West lift · Sensor recalibration</strong>
                <span>Verified 18 September · Northgate Lift Care</span>
              </div>
            </div>
            <div className="pp-fact-row">
              <span>Previous cost</span>
              <strong>$420</strong>
            </div>
            <div className="pp-fact-row">
              <span>Workmanship warranty</span>
              <strong>Until 18 October</strong>
            </div>
            <div className="pp-fact-row">
              <span>Repeat report</span>
              <strong>26 September</strong>
            </div>
            {sourceButton(1)}
          </Panel>
          <Panel title="A different next step">
            <h4>Check warranty coverage first</h4>
            <p>
              The timing and symptom support a warranty enquiry. They do not
              prove that all new work will be free.
            </p>
            <p className="pp-callout">
              No second paid order is prepared while this coverage question is
              open.
            </p>
            {next("Explore the warranty enquiry", 2)}
          </Panel>
        </>
      );
    if (step === 2)
      return (
        <>
          <Panel title="Ask about the original scope">
            <p>
              Link the new report, original repair and warranty terms in a
              focused request to the same supplier.
            </p>
            <button
              type="button"
              className="pp-primary"
              onClick={() =>
                update(
                  { warranty: true },
                  "The sample warranty enquiry is recorded.",
                  3,
                )
              }
            >
              Record sample warranty enquiry
              <ArrowRight size={17} />
            </button>
            {sourceButton(1, "Review the warranty evidence")}
          </Panel>
          <Panel title="Keep the boundary clear">
            <p>
              An unrelated part failure or additional charge needs a new scope
              and authorisation. Warranty handling does not change spending
              limits.
            </p>
          </Panel>
        </>
      );
    if (step === 3)
      return (
        <>
          <Panel title="A return visit, linked to the first repair">
            <span className="pp-badge">Illustrative supplier response</span>
            <h4>28 September · 10:00–12:00</h4>
            <p>
              Europe/Istanbul · UTC+3 · No call-out fee for the covered
              inspection.
            </p>
            {sourceButton(2)}
            <p>
              Any unrelated chargeable work requires a separate quote. The
              original paid work remains on record.
            </p>
            {state.warranty
              ? next("Explore the resident’s confirmation", 4)
              : next("Record the warranty enquiry first", 2)}
          </Panel>
          <Panel title="Keep following the promise">
            <div className="pp-fact-row">
              <span>Responsible party</span>
              <strong>Northgate Lift Care</strong>
            </div>
            <div className="pp-fact-row">
              <span>Next check</span>
              <strong>After the return visit</strong>
            </div>
            <div className="pp-fact-row">
              <span>Required outcome</span>
              <strong>Authorised resident verification</strong>
            </div>
          </Panel>
        </>
      );
    return (
      <>
        <Panel title="A returning fault needs a new verification">
          {state.warranty ? (
            verification
          ) : (
            <>
              <p>
                Record the sample warranty enquiry before verifying the linked
                return visit.
              </p>
              {next("Review the warranty step", 2)}
            </>
          )}
        </Panel>
        <Panel title="History keeps both outcomes">
          <p>
            The original repair, first verification, repeat fault and
            return-visit result stay connected. The next recommendation can use
            that fuller history.
          </p>
          {sourceButton(1)}
        </Panel>
      </>
    );
  }

  return (
    <div className="pp-app">
      <a className="pp-skip" href="#pp-workspace">
        Skip to interactive sample
      </a>
      <header className="pp-header">
        <a className="pp-brand" href="#" aria-label="Steward public preview">
          <BrandLogo size={72} />
          <span>A community that remembers.</span>
        </a>
        <nav aria-label="Access options">
          <button
            type="button"
            className="pp-text-button"
            onClick={onMemberSignIn}
          >
            Member sign-in
          </button>
          <button
            type="button"
            className="pp-secondary"
            onClick={onJudgeAccess}
          >
            Judge access
            <ArrowRight size={15} />
          </button>
        </nav>
      </header>
      <main>
        <section className="pp-intro">
          <div>
            <span className="pp-kicker">
              <Building2 size={15} />
              Meet Steward · Northgate sample community
            </span>
            <h1>
              Everyday conversations.
              <br />
              <span>Things that get done.</span>
            </h1>
            <p>
              Follow a repair, shape a community decision, and see how the next
              action learns from the last one.
            </p>
            <a className="pp-explore-link" href="#pp-workspace">
              Explore the scenarios
              <ArrowDown size={16} />
            </a>
          </div>
          <aside className="pp-intro-note">
            <span className="pp-badge">
              <ShieldCheck size={15} />
              Interactive sample
            </span>
            <p>
              Try the manager and resident views with fictional Northgate
              records.
            </p>
            <small>
              All actions stay in this browser session. No live AI, emails or
              Telegram messages are triggered. Use Judge access to test the
              working application.
            </small>
          </aside>
        </section>
        <section
          id="pp-workspace"
          className="pp-workspace"
          aria-label="Interactive Northgate sample"
          tabIndex={-1}
        >
          <div className="pp-toolbar">
            <div>
              <span className="pp-kicker">Your community, in focus</span>
              <h2>
                {role === "manager"
                  ? "A calmer decision desk"
                  : "Just what you need to know"}
              </h2>
            </div>
            <div className="pp-view-switch" aria-label="Sample role">
              <button
                type="button"
                aria-pressed={role === "manager"}
                onClick={() => {
                  setRole("manager");
                  setNotice("");
                }}
              >
                <Building2 size={16} />
                Manager
              </button>
              <button
                type="button"
                aria-pressed={role === "resident"}
                onClick={() => {
                  setRole("resident");
                  setNotice("");
                }}
              >
                <Users size={16} />
                Resident
              </button>
            </div>
          </div>
          <div className="pp-layout">
            <aside className="pp-scenarios" aria-label="Choose a sample case">
              <p className="pp-section-label">Three stories to explore</p>
              {scenarios.map((item, index) => (
                <button
                  type="button"
                  key={item.id}
                  className={`pp-scenario ${selected === item.id ? "is-active" : ""}`}
                  aria-pressed={selected === item.id}
                  onClick={() => choose(item.id)}
                >
                  <span className="pp-scenario-top">
                    <span className="pp-case-icon">
                      <ScenarioIcon id={item.id} />
                    </span>
                    <span>0{index + 1}</span>
                  </span>
                  <span className="pp-category">{item.category}</span>
                  <strong>{item.title}</strong>
                  <span className="pp-scenario-description">
                    {item.description}
                  </span>
                  <span className="pp-scenario-open">
                    Explore case
                    <ArrowRight size={15} />
                  </span>
                </button>
              ))}
              <button type="button" className="pp-reset" onClick={reset}>
                <RotateCcw size={15} />
                Reset sample actions
              </button>
            </aside>
            <div className="pp-case-area">
              <header className="pp-case-heading">
                <span className="pp-kicker">
                  <ScenarioIcon id={selected} size={15} />
                  {scenario.category} · Sample case
                </span>
                <h2>{scenario.title}</h2>
                <p>
                  {role === "manager"
                    ? scenario.description
                    : "A clear update, your invitation and any response needed from you."}
                </p>
              </header>
              {role === "manager" ? (
                <>
                  <nav
                    className="pp-steps"
                    aria-label="Explore this sample timeline"
                  >
                    {scenario.steps.map((label, index) => (
                      <button
                        type="button"
                        key={label}
                        aria-current={step === index ? "step" : undefined}
                        onClick={() => {
                          setSteps((current) => ({
                            ...current,
                            [selected]: index,
                          }));
                          setNotice("");
                        }}
                      >
                        <span>{index + 1}</span>
                        {label}
                      </button>
                    ))}
                  </nav>
                  <div className="pp-stage-label">
                    <span>Explore this sample timeline</span>
                    <strong>
                      Step {step + 1} of 5 · {scenario.steps[step]}
                    </strong>
                  </div>
                  <div className="pp-panels" key={`${selected}-${step}`}>
                    {managerContent()}
                  </div>
                  <div className="pp-step-footer">
                    <button
                      type="button"
                      className="pp-text-button"
                      disabled={step === 0}
                      onClick={() =>
                        setSteps((current) => ({
                          ...current,
                          [selected]: step - 1,
                        }))
                      }
                    >
                      <ArrowLeft size={16} />
                      Previous step
                    </button>
                    <span>{step + 1} / 5</span>
                    <button
                      type="button"
                      className="pp-text-button"
                      disabled={step === 4}
                      onClick={() =>
                        setSteps((current) => ({
                          ...current,
                          [selected]: step + 1,
                        }))
                      }
                    >
                      Next step
                      <ArrowRight size={16} />
                    </button>
                  </div>
                </>
              ) : (
                <div className="pp-panels pp-resident-panels">
                  <Panel title="Community update">
                    <span className="pp-badge">
                      <Clock3 size={14} />
                      {selected === "parking"
                        ? state.decision
                          ? "Community decision verified"
                          : "Meeting being arranged"
                        : state.verified
                          ? "Resolution verified"
                          : state.rework
                            ? "Rework follow-up"
                            : selected === "elevator"
                              ? "Repair awaiting your check"
                              : "Warranty follow-up"}
                    </span>
                    <p>
                      {selected === "parking"
                        ? state.decision
                          ? "The sample community has agreed to trial a 24-hour visitor limit, with a registered exception for carers. The trial will be reviewed after 30 days."
                          : "We’re bringing residents together to discuss visitor parking. No new rule takes effect until the meeting decision is verified."
                        : state.verified
                          ? "Your sample verification is recorded. The issue is resolved, and the result remains in the community’s history."
                          : state.rework
                            ? "You reported that the issue is still happening. The sample case stays open for follow-up with the service team."
                            : selected === "elevator"
                              ? "The west lift repair has been reported complete in this sample. Please tell us whether the doors now work as expected."
                              : "The recurring west-lift issue is linked to the previous repair. A warranty return visit is being followed up."}
                    </p>
                    <div className="pp-fact-row">
                      <span>Who is following up</span>
                      <strong>
                        {selected === "parking"
                          ? "Simon · Community manager"
                          : "Steward with the service team"}
                      </strong>
                    </div>
                    <p className="pp-resident-note">
                      You can stay involved through community chat. The resident
                      view keeps invitations and small requests easy to find.
                    </p>
                  </Panel>
                  <Panel
                    title={
                      selected === "parking"
                        ? state.decision
                          ? "What happens next"
                          : "Your meeting invitation"
                        : "A quick check from you"
                    }
                  >
                    {selected === "parking" ? (
                      state.decision ? (
                        <>
                          <h4>A clear notice, then a review</h4>
                          <p>
                            {state.task
                              ? "The sample parking notice has been published and its evidence recorded."
                              : "Simon is preparing the parking notice and resident update, due on 20 September."}
                          </p>
                          <div className="pp-fact-row">
                            <span>Review date</span>
                            <strong>20 October</strong>
                          </div>
                          <p>No response is needed from you right now.</p>
                        </>
                      ) : (
                        <>
                          <h4>Visitor parking: agree a shared rule</h4>
                          <p>
                            Compare the 24-hour and 72-hour options, discuss
                            exceptions and decide how to communicate the
                            outcome.
                          </p>
                          {availability}
                        </>
                      )
                    ) : (
                      verification
                    )}
                  </Panel>
                </div>
              )}
              <div
                className={`pp-notice ${notice ? "has-notice" : ""}`}
                role="status"
                aria-live="polite"
              >
                {notice && (
                  <>
                    <CheckCircle2 size={18} />
                    <span>{notice}</span>
                  </>
                )}
              </div>
            </div>
          </div>
        </section>
        <section className="pp-review-invite">
          <div>
            <span className="pp-kicker">Ready to look closer?</span>
            <h2>Review the working system.</h2>
            <p>
              Judge access opens the evaluation environment. The public sample
              above is a browser-only introduction.
            </p>
          </div>
          <button type="button" className="pp-primary" onClick={onJudgeAccess}>
            Open judge access
            <ArrowRight size={17} />
          </button>
        </section>
      </main>
      <footer className="pp-footer">
        <span>Steward · Agents for Humans Hackathon</span>
        <a
          href="https://github.com/tritonsan/steward"
          target="_blank"
          rel="noreferrer"
        >
          Source code & setup
          <ArrowRight size={14} />
        </a>
        <span>Synthetic community · Real-world purpose</span>
      </footer>
      <EvidenceDrawer source={source} close={() => setSource(null)} />
    </div>
  );
}
