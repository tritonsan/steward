import { DISPLAY_TIME_LABEL, formatDateTime } from "./dateTime";
import { useEffect, useState } from "react";
import { CalendarDays, CheckCircle2, RefreshCw, Building2 } from "lucide-react";
import { BrandLogo } from "./BrandLogo";
import {
  PersonalRequest,
  TelegramLink,
  type PersonalQuestion,
} from "./PersonalRequest";

type Invitation = {
  case_id: string;
  schedule_id: string;
  title: string;
  version: number;
  starts_at: string | null;
  slots: { slot_id: string; starts_at: string }[];
  agenda: string[];
  available_slot_ids: string[];
  responded: boolean;
};
type ResidentCase = {
  case_id: string;
  title: string;
  update: string;
  updated_at: string;
};
type Api = (path: string, options?: RequestInit) => Promise<any>;
const date = (value: string) =>
  formatDateTime(value, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });

function InvitationCard({
  invite,
  api,
  reload,
}: {
  invite: Invitation;
  api: Api;
  reload: () => Promise<void>;
}) {
  const [selected, setSelected] = useState(invite.available_slot_ids);
  const [busy, setBusy] = useState(false),
    [message, setMessage] = useState("");
  useEffect(() => setSelected(invite.available_slot_ids), [invite.version]);
  async function respond() {
    setBusy(true);
    setMessage("");
    try {
      await api(`/cases/${invite.case_id}/availability`, {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          expected_version: invite.version,
          schedule_id: invite.schedule_id,
          available_slot_ids: selected,
        }),
      });
      setMessage(
        selected.length
          ? "Your availability has been shared."
          : "Thank you. We have recorded that you cannot attend these times.",
      );
      await reload();
    } catch (error) {
      setMessage((error as Error).message);
      await reload();
    } finally {
      setBusy(false);
    }
  }
  return (
    <article className="invitation-card">
      <div className="resident-card-heading">
        <CalendarDays size={21} />
        <span>{invite.starts_at ? "Meeting confirmed" : "You’re invited"}</span>
      </div>
      <h3>{invite.title}</h3>
      <p className="time-zone-note">{DISPLAY_TIME_LABEL}</p>
      {invite.starts_at ? (
        <p className="meeting-date">{date(invite.starts_at)}</p>
      ) : (
        <>
          <p>Which times work for you? Leave all unchecked if none do.</p>
          <fieldset disabled={busy}>
            <legend>Choose your availability</legend>
            {invite.slots.map((slot) => (
              <label className="check-line" key={slot.slot_id}>
                <input
                  type="checkbox"
                  checked={selected.includes(slot.slot_id)}
                  onChange={(e) =>
                    setSelected(
                      e.target.checked
                        ? [...selected, slot.slot_id]
                        : selected.filter((id) => id !== slot.slot_id),
                    )
                  }
                />
                {date(slot.starts_at)}
              </label>
            ))}
          </fieldset>
          <button disabled={busy} onClick={respond}>
            {busy
              ? "Saving…"
              : invite.responded
                ? "Update availability"
                : "Share availability"}
          </button>
        </>
      )}
      {!!invite.agenda.length && (
        <details>
          <summary>What we’ll discuss</summary>
          <ul>
            {invite.agenda.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </details>
      )}
      {invite.responded && !invite.starts_at && (
        <p className="resident-receipt">
          <CheckCircle2 size={16} /> Your response is recorded.
        </p>
      )}
      {message && <p role="status">{message}</p>}
    </article>
  );
}

export function ResidentDesk({
  api,
  onSignOut,
  channelsEnabled = true,
}: {
  api: Api;
  onSignOut: () => void;
  channelsEnabled?: boolean;
}) {
  const [data, setData] = useState<{
    cases: ResidentCase[];
    invitations: Invitation[];
    requests: PersonalQuestion[];
  }>({ cases: [], invitations: [], requests: [] });
  const [loading, setLoading] = useState(true),
    [error, setError] = useState("");
  const [tab, setTab] = useState("cases");
  async function load() {
    setLoading(true);
    setError("");
    try {
      setData(await api("/resident/overview"));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load();
  }, []);
  return (
    <main className="resident-desk">
      <a className="skip" href="#resident-content">
        Skip to content
      </a>
      <header>
        <div className="resident-identity">
          <BrandLogo size={76} />
          <div>
            <strong>Northgate Residence</strong>
            <small>Resident space</small>
          </div>
        </div>
        <button className="secondary" onClick={onSignOut}>
          Sign out
        </button>
      </header>
      <section id="resident-content">
        <div className="resident-welcome">
          <div>
            <p className="eyebrow">YOUR COMMUNITY</p>
            <h1>What’s happening at Northgate.</h1>
            <p>
              Follow community cases and see the meetings you’re invited to.
            </p>
          </div>
          <button className="secondary" disabled={loading} onClick={load}>
            <RefreshCw size={16} />
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>
        <nav className="resident-tabs" aria-label="Resident sections">
          <button
            aria-current={tab === "cases" ? "page" : undefined}
            onClick={() => setTab("cases")}
          >
            <Building2 size={18} />
            Community cases <span>{data.cases.length}</span>
          </button>
          <button
            aria-current={tab === "meetings" ? "page" : undefined}
            onClick={() => setTab("meetings")}
          >
            <CalendarDays size={18} />
            My meetings <span>{data.invitations.length}</span>
          </button>
        </nav>
        {error && (
          <p className="banner error" role="alert">
            {error}
          </p>
        )}
        {tab === "cases" ? (
          <section aria-label="Community cases" className="resident-case-grid">
            {data.cases.map((item) => (
              <article className="resident-case-card" key={item.case_id}>
                <span className="resident-status">
                  <span className="dot" />
                  In progress
                </span>
                <h2>{item.title}</h2>
                <p>{item.update}</p>
                <small>Updated {date(item.updated_at)}</small>
                {data.requests
                  ?.filter((q) => q.case_id === item.case_id)
                  .map((q) => (
                    <PersonalRequest
                      key={q.request_id}
                      question={q}
                      api={api}
                      reload={load}
                    />
                  ))}
              </article>
            ))}
            {!data.cases.length && !loading && !error && (
              <div className="empty">
                <BrandLogo size={88} />
                <h2>No open cases.</h2>
                <p>Community updates will appear here.</p>
              </div>
            )}
          </section>
        ) : (
          <section
            aria-label="Your meeting invitations"
            className="resident-meetings"
          >
            {data.invitations.map((invite) => (
              <InvitationCard
                key={invite.case_id}
                invite={invite}
                api={api}
                reload={load}
              />
            ))}
            {!data.invitations.length && !loading && !error && (
              <div className="empty">
                <CalendarDays />
                <h2>No meeting invitations.</h2>
                <p>Your invitations and meeting times will appear here.</p>
              </div>
            )}
          </section>
        )}
        {channelsEnabled && <TelegramLink api={api} />}
        <p className="resident-footnote">
          Management is following the work. You can keep up here without
          managing the details.
        </p>
      </section>
    </main>
  );
}
