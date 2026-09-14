import { DISPLAY_TIME_LABEL, formatDateTime } from "./dateTime";
import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ArrowRight,
  Check,
  ChevronRight,
  Clock3,
  Home,
  ListChecks,
  Menu,
  MessageSquare,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  X,
  FlaskConical,
  LogOut,
} from "lucide-react";
import "./style.css";
import { BrandLogo } from "./BrandLogo";
import { CaseContinuity, ChannelSettings } from "./OperationsControls";
import { Suggestions } from "./Suggestions";
import { VendorSimulation } from "./VendorSimulation";
import { ResidentDesk } from "./ResidentDesk";
import { MeetingPanel } from "./MeetingPanel";
import { startSignIn, finishSignIn, type AuthConfig } from "./auth";
import {
  needsManagerAction,
  taskActionLabel,
  taskHeading,
} from "./decisionDesk";

type Case = {
  case_id: string;
  title: string;
  status: string;
  category: string;
  version: number;
  next_step: string;
  waiting_for: string;
  due_at: string | null;
  asset_id: string | null;
  updated_at: string;
  timeline?: Entry[];
  assignments?: { payload: any }[];
  meetings?: { artifact_id: string }[];
  tasks?: Task[];
  outbox?: {
    outbox_id: string;
    status: string;
    payload: { purpose?: string };
  }[];
  quotes?: {
    artifact_id: string;
    payload: {
      quote: {
        vendor_id: string;
        amount: string;
        currency: string;
        scope: string;
        valid_until: string | null;
      };
    };
  }[];
  decisions?: {
    artifact_id: string;
    payload: {
      recommendation: {
        rationale: string;
        source_ids: string[];
        alternatives?: { quote_id: string; reason_not_selected: string }[];
        decision_changes_when?: string[];
      };
      selected_quote: { amount: string; currency: string };
    };
  }[];
};
type Task = {
  task_id: string;
  case_id: string;
  kind: string;
  title: string;
  reason: string;
  expected_version: number;
  due_at: string;
  allowed_roles?: string[];
  assigned_actor_ids?: string[];
};
type Entry = {
  event_id: string;
  at: string;
  summary: string;
  refs: string[];
  actor: string;
};
type Memory = {
  has_case_record?: boolean;
  case_id: string;
  title: string;
  category: string;
  cost: string;
  currency: string;
  work_performed: string;
  resolution_notes: string;
  selected_vendor_id: string | null;
  closed_at: string;
  outcome_verified: boolean;
  is_simulated: boolean;
};
const words = (v: string) => v.replaceAll("_", " ");
const when = (v: string | null | undefined) =>
  v
    ? formatDateTime(v, {
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "No deadline";
const pages = [
  ["Today", Home],
  ["Cases", ListChecks],
  ["Decisions", Check],
  ["Community memory", BrandLogo],
  ["Settings", Settings],
] as const;

function App() {
  const [role, setRole] = useState("manager");
  const [actorId, setActorId] = useState("");
  const [token, setToken] = useState(""),
    [draftToken, setDraftToken] = useState(""),
    [signedIn, setSignedIn] = useState(false);
  const [page, setPage] = useState("Today"),
    [cases, setCases] = useState<Case[]>([]),
    [tasks, setTasks] = useState<Task[]>([]),
    [memory, setMemory] = useState<Memory[]>([]);
  const [selected, setSelected] = useState<Case | null>(null),
    [evidence, setEvidence] = useState<Memory | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(false);
  const [query, setQuery] = useState(""),
    [notice, setNotice] = useState(""),
    [notes, setNotes] = useState(""),
    [sim, setSim] = useState(false),
    [connected, setConnected] = useState(false),
    [mobileNav, setMobileNav] = useState(false);
  const [source, setSource] = useState<any>(null);
  const [settings, setSettings] = useState<any>(null),
    [message, setMessage] = useState(""),
    [sender, setSender] = useState("Daniel K.");
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null),
    oauthStarted = useRef(false);
  const [authLoading, setAuthLoading] = useState(true),
    [settingsDirty, setSettingsDirty] = useState(false);
  const selectedRef = useRef<Case | null>(null),
    settingsDirtyRef = useRef(false);
  const requestKey = useRef<string | null>(null),
    panel = useRef<HTMLDivElement>(null);
  async function api(path: string, options: RequestInit = {}, auth = token) {
    const r = await fetch("/api" + path, {
      ...options,
      headers: {
        Authorization: "Bearer " + auth,
        "Content-Type": "application/json",
        ...options.headers,
      },
    });
    if (!r.ok) {
      const body = await r
        .json()
        .catch(() => ({ detail: "Connection failed" }));
      if (r.status === 401 && signedIn) {
        setSignedIn(false);
        setToken("");
        setSelected(null);
        setEvidence(null);
      }
      const failure = new Error(
        r.status === 401 && signedIn
          ? "Your session expired. Sign in again to continue."
          : typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail),
      );
      Object.assign(failure, { status: r.status });
      throw failure;
    }
    return r.json();
  }
  async function load(auth = token) {
    setLoading(true);
    try {
      const [c, t, m, s] = await Promise.all([
        api("/cases", {}, auth),
        api("/tasks", {}, auth),
        api("/memory", {}, auth),
        api("/settings", {}, auth),
      ]);
      setCases(c);
      setTasks(t);
      setMemory(m);
      if (!settingsDirtyRef.current) setSettings(s);
      const current = selectedRef.current;
      if (current) {
        const detail = await api(
          "/cases/" + encodeURIComponent(current.case_id),
          {},
          auth,
        );
        setSelected((previous) =>
          previous &&
          previous.case_id === detail.case_id &&
          detail.version > previous.version
            ? detail
            : previous,
        );
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }
  async function establish(auth: string) {
    const me = await api("/me", {}, auth);
    setRole(me.role);
    setActorId(me.actor_id);
    setToken(auth);
    setSim(me.simulation);
    setSignedIn(true);
    setPage("Today");
    setError("");
    setNotice("");
    settingsDirtyRef.current = false;
    setSettingsDirty(false);
    if (me.role === "manager") await load(auth);
  }
  async function login(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (authConfig?.domain) await startSignIn(authConfig);
      else await establish(draftToken.trim());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function configureSignIn() {
    setAuthLoading(true);
    setError("");
    try {
      const response = await fetch("/api/auth/config");
      if (!response.ok)
        throw new Error("Sign-in is temporarily unavailable. Please retry.");
      const config = await response.json();
      setAuthConfig(config);
      if (config.domain) {
        const auth = await finishSignIn(config);
        if (auth) await establish(auth);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setAuthLoading(false);
    }
  }
  useEffect(() => {
    if (oauthStarted.current) return;
    oauthStarted.current = true;
    void configureSignIn();
  }, []);
  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);

  useEffect(() => {
    if (!signedIn || role !== "manager") return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function subscribe() {
      try {
        const r = await fetch("/api/events", {
          headers: { Authorization: "Bearer " + token },
          signal: controller.signal,
        });
        if (!r.ok || !r.body) throw new Error();
        setConnected(true);
        const reader = r.body.getReader();
        let buffer = "";
        while (true) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += new TextDecoder().decode(chunk.value);
          let split;
          while ((split = buffer.indexOf("\n\n")) >= 0) {
            const event = buffer.slice(0, split);
            buffer = buffer.slice(split + 2);
            if (event.includes("event: refresh")) await load();
          }
        }
      } catch {
      } finally {
        setConnected(false);
        if (!controller.signal.aborted) timer = setTimeout(subscribe, 4000);
      }
    }
    subscribe();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [token, signedIn, role]);
  useEffect(() => {
    if (selected || evidence) {
      const previous = document.activeElement as HTMLElement | null;
      panel.current?.focus();
      return () => previous?.focus();
    }
  }, [selected?.case_id, evidence?.case_id]);
  useEffect(() => {
    if (!source || !panel.current) return;
    const previous = document.activeElement as HTMLElement | null;
    const scroll = panel.current.scrollTop;
    panel.current.scrollTop = 0;
    panel.current
      .querySelector<HTMLButtonElement>(".evidence-pane button")
      ?.focus();
    return () => {
      if (panel.current) panel.current.scrollTop = scroll;
      previous?.focus();
    };
  }, [source]);
  async function showSource(id: string) {
    try {
      setSource(
        await api(
          "/cases/" + selected!.case_id + "/sources/" + encodeURIComponent(id),
        ),
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function open(id: string) {
    try {
      setError("");
      setNotice("");
      setNotes("");
      setSource(null);
      requestKey.current = null;
      setSelected(await api("/cases/" + encodeURIComponent(id)));
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function command(
    action: string,
    accepted = true,
    data: Record<string, unknown> = {},
  ) {
    if (!selected || !notes.trim()) return;
    setBusy(true);
    setError("");
    requestKey.current ??= crypto.randomUUID();
    try {
      await api("/cases/" + selected.case_id + "/commands", {
        method: "POST",
        headers: { "Idempotency-Key": requestKey.current },
        body: JSON.stringify({
          action,
          accepted,
          notes,
          data,
          expected_version: selected.version,
        }),
      });
      requestKey.current = null;
      setNotice(
        action === "verify"
          ? accepted
            ? "Outcome verified and remembered."
            : "Repair returned for warranty review."
          : action === "record_completion"
            ? "Completion recorded. Verification is now required."
            : action === "review_resolution"
              ? "Resolution preparation queued. Steward will review the evidence."
              : action === "approve_quote"
                ? "Approval recorded. The current delivery status and next step are shown below."
                : "Your decision was recorded. The case is up to date.",
      );
      setNotes("");
      await load();
      panel.current?.scrollTo({ top: 0, behavior: "instant" });
      panel.current?.focus();
    } catch (e) {
      setError(
        (e as Error & { status?: number }).status === 409
          ? "This case changed while you were reviewing it. Review the refreshed details before trying again. Your note is preserved."
          : (e as Error).message,
      );
      requestKey.current = null;
      await load();
    } finally {
      setBusy(false);
    }
  }
  async function saveSettings() {
    setBusy(true);
    setError("");
    try {
      await api("/settings", {
        method: "PUT",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({
          ...settings,
          expected_version: settings.version,
          version: undefined,
        }),
      });
      settingsDirtyRef.current = false;
      setSettingsDirty(false);
      setNotice("Your permissions have been saved.");
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function editSettings(next: any) {
    settingsDirtyRef.current = true;
    setSettingsDirty(true);
    setSettings(next);
  }
  async function simulation(action: string) {
    setBusy(true);
    setError("");
    try {
      if (action === "message") {
        await api("/simulation/messages", {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
          body: JSON.stringify({ sender, text: message }),
        });
        setMessage("");
      } else await api("/simulation/" + action, { method: "POST" });
      await load();
      setNotice("Simulation event processed.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const filtered = cases.filter((c) =>
    (c.title + " " + c.category + " " + c.status)
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  const decisionTasks = tasks.filter((task) =>
    needsManagerAction(task, actorId),
  );
  const waitingTasks = tasks.filter(
    (task) => !needsManagerAction(task, actorId),
  );
  const waitingCases = cases.filter(
    (c) =>
      waitingTasks.some((task) => task.case_id === c.case_id) ||
      (c.waiting_for !== "Steward" &&
        c.waiting_for !== "Management" &&
        !decisionTasks.some((task) => task.case_id === c.case_id)),
  );
  const filteredMemory = memory.filter((m) =>
    JSON.stringify(m).toLowerCase().includes(query.toLowerCase()),
  );
  const changePage = (p: string) => {
    setPage(p);
    setQuery("");
    setMobileNav(false);
    setNotice("");
  };
  if (!signedIn)
    return (
      <main className="login">
        <div className="login-art">
          <BrandLogo size={156} />
          <h1>
            A little less to manage.
            <br />A community that remembers.
          </h1>
          <p>
            Steward follows the work from the first message to a verified
            resolution.
          </p>
          <small>NORTHGATE RESIDENCE</small>
        </div>
        <form onSubmit={login} className="login-form">
          <div className="brand">
            <BrandLogo size={96} />
          </div>
          <h2>Welcome back.</h2>
          <p>Sign in to your Northgate workspace.</p>
          {authConfig && !authConfig.domain && (
            <label>
              Local access token
              <input
                type="password"
                autoComplete="off"
                required
                value={draftToken}
                onChange={(e) => setDraftToken(e.target.value)}
              />
            </label>
          )}
          <button disabled={busy || authLoading || !authConfig}>
            {authLoading
              ? "Preparing sign-in…"
              : busy
                ? "Signing in…"
                : authConfig?.domain
                  ? "Sign in to Northgate"
                  : "Open your desk"}{" "}
            <ArrowRight size={16} />
          </button>
          {!authConfig && !authLoading && (
            <button
              type="button"
              className="secondary"
              onClick={configureSignIn}
            >
              Retry sign-in setup
            </button>
          )}
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          <small>
            Your access is checked before any community information is loaded.
          </small>
        </form>
      </main>
    );
  if (role === "resident")
    return (
      <ResidentDesk
        api={api}
        onSignOut={() => {
          setToken("");
          setSignedIn(false);
        }}
      />
    );
  return (
    <div className="app">
      <a className="skip" href="#content" inert={!!(selected || evidence)}>
        Skip to content
      </a>
      <aside
        className={"sidebar " + (mobileNav ? "expanded" : "")}
        inert={!!(selected || evidence)}
      >
        <div className="brand">
          <BrandLogo size={96} />
        </div>
        <div className="property">
          <span className="property-icon">N</span>
          <div>
            Northgate Residence<small>Management workspace</small>
          </div>
        </div>
        <nav aria-label="Main navigation">
          {pages.map(([p, Icon]) => (
            <button
              key={p}
              className={p === page ? "active" : ""}
              aria-current={p === page ? "page" : undefined}
              onClick={() => changePage(p)}
            >
              {p === "Community memory" ? (
                <BrandLogo size={28} decorative />
              ) : (
                <Icon size={18} />
              )}
              {p}
              {p === "Decisions" && decisionTasks.length > 0 && (
                <span className="nav-count">{decisionTasks.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div>
            <span className={"dot " + (connected ? "" : "off")} />
            {connected ? "Your desk is up to date" : "Reconnecting…"}
          </div>
          {sim && (
            <button
              onClick={() => changePage("Simulation")}
              className={page === "Simulation" ? "active" : ""}
            >
              <FlaskConical size={16} /> Simulation studio
            </button>
          )}
          <button
            onClick={() => {
              setToken("");
              setDraftToken("");
              setSignedIn(false);
            }}
          >
            <LogOut size={16} /> Sign out
          </button>
        </div>
      </aside>
      <div className="workspace" inert={!!(selected || evidence)}>
        <header>
          <button
            className="mobile-menu"
            aria-label="Toggle navigation"
            aria-expanded={mobileNav}
            onClick={() => setMobileNav(!mobileNav)}
          >
            <Menu />
          </button>
          <div>
            Northgate <ChevronRight size={14} /> <strong>{page}</strong>
          </div>
          <div>
            <span className="live-label">
              {sim ? "Synthetic community" : "Community workspace"}
            </span>
            <span className="avatar" aria-label={actorId}>
              {actorId
                .split(" ")
                .map((part) => part[0])
                .slice(0, 2)
                .join("")}
            </span>
          </div>
        </header>
        <main id="content">
          <div className="page-heading">
            <div>
              <p className="eyebrow">MANAGEMENT WORKSPACE</p>
              <h1>{page === "Today" ? "Community operations." : page}</h1>
              <p>
                {page === "Today"
                  ? "See what needs a decision, who is responsible and what happens next."
                  : page === "Cases"
                    ? "Every open problem has a place, a next step and someone looking after it."
                    : page === "Community memory"
                      ? "The experience your community can carry forward."
                      : page === "Decisions"
                        ? "A clear reason, the evidence, and a decision that stays yours."
                        : page === "Settings"
                          ? "Set the boundaries. Steward works within them."
                          : "Create external events and observe the same operational runtime."}
              </p>
            </div>
            <button
              className="secondary compact"
              disabled={loading}
              onClick={() => load()}
            >
              <RefreshCw size={15} />
              {loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>
          {error && (
            <div className="banner error" role="alert">
              {error}
              <button aria-label="Dismiss error" onClick={() => setError("")}>
                <X size={16} />
              </button>
            </div>
          )}
          {notice && (
            <div className="banner success" role="status">
              {notice}
              <button
                aria-label="Dismiss notification"
                onClick={() => setNotice("")}
              >
                <X size={16} />
              </button>
            </div>
          )}
          {(page === "Today" || page === "Decisions") && (
            <>
              <section className="stats" aria-label="Community activity">
                <div>
                  <span>Needs your decision</span>
                  <strong>
                    {decisionTasks.length.toString().padStart(2, "0")}
                  </strong>
                  <small>Prepared for your review</small>
                </div>
                <div>
                  <span>In Steward’s care</span>
                  <strong>{cases.length.toString().padStart(2, "0")}</strong>
                  <small>Open community cases</small>
                </div>
                <div>
                  <span>Waiting for a response</span>
                  <strong>
                    {waitingCases.length.toString().padStart(2, "0")}
                  </strong>
                  <small>Residents, participants or vendors</small>
                </div>
              </section>
              <section>
                <div className="section-heading">
                  <h2>
                    Needs your decision <span>{decisionTasks.length}</span>
                  </h2>
                  <span>Prepared, with context</span>
                </div>
                {decisionTasks.length === 0 ? (
                  <div className="empty">
                    <ShieldCheck />
                    <h3>Nothing needs your decision.</h3>
                    <p>Steward will bring you the next meaningful decision.</p>
                  </div>
                ) : (
                  <div className="decision-grid">
                    {decisionTasks.map((t) => (
                      <article className="decision-card" key={t.task_id}>
                        <div className="card-label">
                          <span className="badge amber">
                            {taskHeading(t.kind)}
                          </span>
                          <Clock3 size={15} />
                        </div>
                        <h3>
                          {cases.find((c) => c.case_id === t.case_id)?.title ||
                            t.title}
                        </h3>
                        <p>{t.reason}</p>
                        <div className="card-footer">
                          <span>
                            <Clock3 size={13} /> {when(t.due_at)}
                          </span>
                          <button
                            className="text-button"
                            onClick={() => open(t.case_id)}
                          >
                            {taskActionLabel(t.kind)} <ArrowRight size={16} />
                          </button>
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </section>
              {waitingTasks.length > 0 && (
                <section className="response-tracking">
                  <div className="section-heading">
                    <h2>
                      Waiting for replies <span>{waitingTasks.length}</span>
                    </h2>
                    <span>Steward is following up</span>
                  </div>
                  <div className="case-list">
                    {waitingTasks.map((t) => (
                      <button
                        key={t.task_id}
                        className="response-row"
                        onClick={() => open(t.case_id)}
                      >
                        <MessageSquare size={18} aria-hidden="true" />
                        <span>
                          <strong>
                            {cases.find((c) => c.case_id === t.case_id)
                              ?.title || t.title}
                          </strong>
                          <small>
                            {taskHeading(t.kind)} ·{" "}
                            {t.kind === "meeting_availability"
                              ? "Meeting participants"
                              : t.assigned_actor_ids?.join(", ") ||
                                "Assigned participant"}
                          </small>
                          <small>Follow-up: {when(t.due_at)}</small>
                        </span>
                        <span className="response-action">
                          {taskActionLabel(t.kind)} <ArrowRight size={15} />
                        </span>
                      </button>
                    ))}
                  </div>
                </section>
              )}
            </>
          )}
          {(page === "Today" || page === "Cases") && (
            <section className="case-section">
              <div className="section-heading">
                <h2>
                  {page === "Today" ? "Operations in progress" : "Open cases"}{" "}
                  <span>{filtered.length}</span>
                </h2>
                <label className="search">
                  <Search size={16} />
                  <input
                    aria-label="Search cases"
                    placeholder="Find a case…"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                </label>
              </div>
              <div className="case-list">
                {filtered.map((c) => (
                  <button
                    className="case-row"
                    key={c.case_id}
                    onClick={() => open(c.case_id)}
                  >
                    <span className="case-icon">
                      <Home size={18} />
                    </span>
                    <span className="case-name">
                      <strong>{c.title}</strong>
                      <small>
                        {words(c.category)}{" "}
                        {c.asset_id ? " · " + words(c.asset_id) : ""}
                      </small>
                    </span>
                    <span className="case-next">
                      <span>{c.next_step}</span>
                      <small>
                        {c.waiting_for} · {when(c.due_at)}
                      </small>
                    </span>
                    <span className="badge">{words(c.status)}</span>
                    <ChevronRight size={16} />
                  </button>
                ))}
                {!filtered.length && (
                  <div className="empty">
                    <BrandLogo size={72} />
                    <h3>{query ? "No matching cases." : "A clear desk."}</h3>
                    <p>
                      {query
                        ? "Try a different title or category."
                        : "New community concerns will appear here."}
                    </p>
                  </div>
                )}
              </div>
            </section>
          )}
          {page === "Community memory" && (
            <>
              <label className="search memory-search">
                <Search size={16} />
                <input
                  aria-label="Search community memory"
                  placeholder="Find an asset, problem or vendor…"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
              </label>
              <div className="memory-grid">
                {filteredMemory.map((m) => (
                  <button
                    className="memory-card"
                    key={m.case_id}
                    onClick={() => setEvidence(m)}
                  >
                    <span className="eyebrow">{words(m.category)}</span>
                    <h3>{m.title}</h3>
                    <p>
                      {m.work_performed ||
                        m.resolution_notes ||
                        "Open the recorded evidence."}
                    </p>
                    <div>
                      <span>
                        {m.cost} {m.currency}
                      </span>
                      <span>
                        {m.outcome_verified
                          ? "Verified outcome"
                          : "Verification unknown"}{" "}
                        <ChevronRight size={14} />
                      </span>
                    </div>
                  </button>
                ))}
              </div>
              {!filteredMemory.length && (
                <div className="empty">
                  <h3>
                    {query
                      ? "No matching memories."
                      : "Your community’s experience starts here."}
                  </h3>
                  <p>
                    {query
                      ? "Try another asset, problem or vendor."
                      : "Verified outcomes will be remembered after cases close."}
                  </p>
                </div>
              )}
            </>
          )}
          {page === "Settings" && <ChannelSettings api={api} />}
          {page === "Settings" && settings && (
            <>
              <section className="settings-panel">
                <div>
                  <ShieldCheck />
                  <h2>Operational authority</h2>
                  <p>
                    These rules are enforced before external actions. Changing a
                    limit does not approve a particular case.
                  </p>
                </div>
                <label className="toggle">
                  <input
                    type="checkbox"
                    checked={settings.settings.kill_switch}
                    onChange={(e) =>
                      editSettings({
                        ...settings,
                        settings: {
                          ...settings.settings,
                          kill_switch: e.target.checked,
                        },
                      })
                    }
                  />
                  <span>
                    Pause all outbound actions
                    <small>Open cases and evidence remain available.</small>
                  </span>
                </label>
                {Object.entries(settings.policies).map(
                  ([key, p]: [string, any]) => (
                    <div className="policy" key={key}>
                      <strong>{words(key)}</strong>
                      <label>
                        Authority
                        <select
                          value={p.mode}
                          onChange={(e) =>
                            editSettings({
                              ...settings,
                              policies: {
                                ...settings.policies,
                                [key]: { ...p, mode: e.target.value },
                              },
                            })
                          }
                        >
                          <option value="always_approve">Within policy</option>
                          <option value="prepare_only">
                            Prepare for approval
                          </option>
                          <option value="escalate_only">Management only</option>
                        </select>
                      </label>
                      <label>
                        Per incident ({p.currency})
                        <input
                          type="number"
                          min="0"
                          step="0.01"
                          value={p.per_incident_cap ?? ""}
                          onChange={(e) =>
                            editSettings({
                              ...settings,
                              policies: {
                                ...settings.policies,
                                [key]: {
                                  ...p,
                                  per_incident_cap: e.target.value || null,
                                },
                              },
                            })
                          }
                        />
                      </label>
                      <label>
                        Monthly ({p.currency})
                        <input
                          type="number"
                          min="0"
                          step="0.01"
                          value={p.monthly_cap ?? ""}
                          onChange={(e) =>
                            editSettings({
                              ...settings,
                              policies: {
                                ...settings.policies,
                                [key]: {
                                  ...p,
                                  monthly_cap: e.target.value || null,
                                },
                              },
                            })
                          }
                        />
                      </label>
                    </div>
                  ),
                )}
                {settingsDirty && (
                  <p role="status">You have unsaved permission changes.</p>
                )}
                <button
                  disabled={busy || !settingsDirty}
                  onClick={saveSettings}
                >
                  Save permissions <Check size={16} />
                </button>
              </section>
            </>
          )}
          {page === "Simulation" && sim && (
            <section className="simulation">
              <span className="badge amber">
                Synthetic inputs · operational runtime
              </span>
              <h2>Give the community something to handle.</h2>
              <VendorSimulation api={api} cases={cases} onChanged={load} />
              <hr />
              <p>
                Events enter the durable inbox. The worker determines what
                happens next.
              </p>
              <label>
                Resident
                <select
                  value={sender}
                  onChange={(e) => setSender(e.target.value)}
                >
                  {[
                    "Daniel K.",
                    "Simon O.",
                    "Amelia T.",
                    "Priya S.",
                    "Ingrid B.",
                    "Marcus R.",
                    "Hannah W.",
                    "James D.",
                  ].map((n) => (
                    <option key={n}>{n}</option>
                  ))}
                </select>
              </label>
              <label>
                Message
                <textarea
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                  placeholder="The A Block elevator is shuddering again near the fourth floor."
                />
              </label>
              <button
                disabled={busy || !message.trim()}
                onClick={() => simulation("message")}
              >
                <MessageSquare size={16} /> Add resident message
              </button>
              <hr />
              <div className="button-row">
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => simulation("tick")}
                >
                  <RefreshCw size={16} /> Run one tick
                </button>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => simulation("advance?hours=24")}
                >
                  <Clock3 size={16} /> Advance one day
                </button>
              </div>
            </section>
          )}
          {page === "Today" && <Suggestions api={api} onChanged={load} />}
          <footer>
            <BrandLogo size={36} decorative /> A little less to manage. A
            community that remembers.
            <span className="time-zone-note">
              Times shown in {DISPLAY_TIME_LABEL}
            </span>
          </footer>
        </main>
      </div>
      {(selected || evidence) && (
        <div
          className="overlay"
          onClick={() => {
            setSelected(null);
            setEvidence(null);
          }}
        >
          <div
            className="detail"
            role="dialog"
            aria-modal="true"
            aria-label={selected?.title || evidence?.title}
            tabIndex={-1}
            ref={panel}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                if (source) {
                  setSource(null);
                  return;
                }
                setSelected(null);
                setEvidence(null);
              }
              if (e.key === "Tab") {
                const scope = source
                  ? panel.current?.querySelector(".evidence-pane")
                  : panel.current;
                const focusable = Array.from(
                  scope?.querySelectorAll<HTMLElement>(
                    "button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),a[href],summary",
                  ) || [],
                ).filter((element) => element.getClientRects().length > 0);
                if (focusable?.length) {
                  const first = focusable[0],
                    last = focusable[focusable.length - 1];
                  if (
                    e.shiftKey &&
                    (document.activeElement === first ||
                      document.activeElement === panel.current)
                  ) {
                    e.preventDefault();
                    last.focus();
                  } else if (!e.shiftKey && document.activeElement === last) {
                    e.preventDefault();
                    first.focus();
                  }
                }
              }
            }}
          >
            {error && (
              <p role="alert" className="error">
                {error}
              </p>
            )}
            {notice && (
              <div className="banner success" role="status">
                {notice}
                <button
                  aria-label="Dismiss case notification"
                  onClick={() => setNotice("")}
                >
                  <X size={16} />
                </button>
              </div>
            )}
            <button
              className="close secondary"
              aria-label="Close details"
              onClick={() => {
                setSelected(null);
                setEvidence(null);
              }}
            >
              <X size={18} />
            </button>
            {selected ? (
              <>
                <p className="eyebrow">
                  CASE RECORD · {words(selected.category)}
                </p>
                <h2>{selected.title}</h2>
                <span className="badge">{words(selected.status)}</span>
                <div className="next-step">
                  <small>WHAT HAPPENS NEXT</small>
                  <h3>{selected.next_step}</h3>
                  <dl>
                    <div>
                      <dt>Responsible / waiting for</dt>
                      <dd>{selected.waiting_for}</dd>
                    </div>
                    <div>
                      <dt>Next follow-up</dt>
                      <dd>{when(selected.due_at)}</dd>
                    </div>
                  </dl>
                </div>
                {(!!selected.meetings?.length ||
                  selected.tasks?.some((t) =>
                    t.kind.startsWith("meeting_"),
                  )) && (
                  <MeetingPanel
                    key={`meeting:${selected.case_id}`}
                    version={selected.version}
                    assignments={selected.assignments}
                    tasks={selected.tasks}
                    readOnly={["closed", "cancelled"].includes(selected.status)}
                    caseId={selected.case_id}
                    api={api}
                    command={command}
                    notes={notes}
                    setNotes={setNotes}
                    busy={busy}
                  />
                )}
                {((selected.status === "awaiting_verification" &&
                  selected.tasks?.some(
                    (task) => task.kind === "verify_completion",
                  )) ||
                  (["scheduled", "warranty_review"].includes(selected.status) &&
                    !selected.meetings?.length &&
                    !selected.tasks?.some((task) =>
                      task.kind.startsWith("meeting_"),
                    ))) && (
                  <div className="action-form">
                    <label>
                      {selected.status !== "awaiting_verification"
                        ? "Completion evidence"
                        : "Your verification notes"}
                      <textarea
                        required
                        value={notes}
                        onChange={(e) => setNotes(e.target.value)}
                      />
                    </label>
                    <div className="button-row">
                      {selected.status !== "awaiting_verification" ? (
                        <button
                          disabled={busy || !notes.trim()}
                          onClick={() => command("record_completion")}
                        >
                          Record completion
                        </button>
                      ) : (
                        <>
                          <button
                            disabled={busy || !notes.trim()}
                            onClick={() => command("verify", true)}
                          >
                            <Check size={16} /> Confirm resolved
                          </button>
                          <button
                            className="secondary"
                            disabled={busy || !notes.trim()}
                            onClick={() => command("verify", false)}
                          >
                            Still a problem
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                )}
                {selected.decisions?.slice(-1).map((d, i) => (
                  <div className="reason" key={i}>
                    <h3>Recommendation</h3>
                    <p>{d.payload.recommendation.rationale}</p>
                    <strong>
                      {d.payload.selected_quote.amount}{" "}
                      {d.payload.selected_quote.currency}
                    </strong>
                    {d.payload.recommendation.alternatives?.map((a) => (
                      <p key={a.quote_id}>
                        Alternative: {a.reason_not_selected}
                      </p>
                    ))}
                    {d.payload.recommendation.decision_changes_when?.length ? (
                      <>
                        <h4>What could change this decision</h4>
                        <ul>
                          {d.payload.recommendation.decision_changes_when.map(
                            (reason, i) => (
                              <li key={i}>{reason}</li>
                            ),
                          )}
                        </ul>
                      </>
                    ) : null}
                    <div className="source-links">
                      {d.payload.recommendation.source_ids.map((id, i) => (
                        <button
                          className="text-button"
                          key={id}
                          onClick={() => showSource(id)}
                        >
                          Source {i + 1}
                        </button>
                      ))}
                    </div>
                    {selected.tasks?.some(
                      (t) => t.kind === "quote_approval",
                    ) && (
                      <>
                        <label>
                          Approval note
                          <textarea
                            aria-describedby="approval-note-help"
                            placeholder="Why is this option appropriate for the community?"
                            value={notes}
                            onChange={(e) => setNotes(e.target.value)}
                          />
                        </label>
                        <small
                          className="decision-note-hint"
                          id="approval-note-help"
                        >
                          Add a short reason to enable approval. The appointment
                          is confirmed only after the vendor agrees a time.
                        </small>
                        <button
                          disabled={busy || !notes.trim()}
                          onClick={() =>
                            command("approve_quote", true, {
                              decision_id: d.artifact_id,
                            })
                          }
                        >
                          {busy
                            ? "Recording approval…"
                            : "Approve this service order"}
                        </button>
                      </>
                    )}
                  </div>
                ))}
                {selected.tasks?.some((t) => t.kind === "delivery_review") && (
                  <form
                    className="action-form"
                    onSubmit={(e) => {
                      e.preventDefault();
                      const form = new FormData(e.currentTarget);
                      const item = selected.outbox?.find(
                        (o) =>
                          o.payload.purpose === "commitment" &&
                          ["ambiguous", "dead_letter"].includes(o.status),
                      );
                      command("reconcile_order_delivery", true, {
                        outbox_id: item?.outbox_id,
                        provider_message_id: form.get("receipt"),
                      });
                    }}
                  >
                    <h3>Confirm service order delivery</h3>
                    <p>
                      Check the provider receipt before continuing. This records
                      evidence; it does not resend the order.
                    </p>
                    <label>
                      Provider receipt identifier
                      <input required name="receipt" />
                    </label>
                    <label>
                      Reconciliation evidence
                      <textarea
                        required
                        value={notes}
                        onChange={(e) => setNotes(e.target.value)}
                      />
                    </label>
                    <button disabled={busy || !notes.trim()}>
                      Record confirmed delivery
                    </button>
                  </form>
                )}
                {selected.tasks?.some((t) => t.kind === "warranty_check") && (
                  <div className="action-form">
                    <h3>Check the previous repair first</h3>
                    <p>
                      {
                        selected.tasks.find((t) => t.kind === "warranty_check")
                          ?.reason
                      }
                    </p>
                    <label>
                      Warranty evidence and reason for a new paid review
                      <textarea
                        value={notes}
                        onChange={(e) => setNotes(e.target.value)}
                      />
                    </label>
                    <button
                      disabled={busy || !notes.trim()}
                      onClick={() => command("continue_after_warranty_check")}
                    >
                      Record check and continue procurement
                    </button>
                  </div>
                )}
                {selected.tasks?.some((t) => t.kind === "intake_review") && (
                  <div className="action-form">
                    <label>
                      What needs clarification?
                      <textarea
                        value={notes}
                        onChange={(e) => setNotes(e.target.value)}
                      />
                    </label>
                    <button
                      disabled={busy || !notes.trim()}
                      onClick={() => command("review_resolution")}
                    >
                      Prepare a resolution
                    </button>
                  </div>
                )}
                <CaseContinuity
                  key={`continuity:${selected.case_id}`}
                  record={selected}
                  showSource={showSource}
                  api={api}
                  reload={async () => {
                    await open(selected.case_id);
                    await load();
                  }}
                />
                <details className="activity-details">
                  <summary>Activity history and sources</summary>
                  <ol className="timeline">
                    {selected.timeline?.map((e) => (
                      <li key={e.event_id}>
                        <small>
                          {when(e.at)} · {words(e.actor)}
                        </small>
                        <p>{e.summary}</p>
                        {e.refs.length > 0 && (
                          <div className="source-links">
                            {e.refs.map((id, i) => (
                              <button
                                className="text-button"
                                key={id}
                                onClick={() => showSource(id)}
                              >
                                Source {i + 1}
                              </button>
                            ))}
                          </div>
                        )}
                      </li>
                    ))}
                  </ol>
                </details>
              </>
            ) : (
              evidence && (
                <>
                  <p className="eyebrow">COMMUNITY EXPERIENCE</p>
                  <h2>{evidence.title}</h2>
                  {evidence.has_case_record && (
                    <button
                      className="secondary"
                      onClick={() => open(evidence.case_id)}
                    >
                      Open case record
                    </button>
                  )}
                  <span className="badge">
                    {evidence.outcome_verified
                      ? "Verified outcome"
                      : "Verification unknown"}
                  </span>
                  <dl>
                    <dt>Work performed</dt>
                    <dd>{evidence.work_performed || "Not recorded"}</dd>
                    <dt>Outcome</dt>
                    <dd>{evidence.resolution_notes || "Not recorded"}</dd>
                    <dt>Cost</dt>
                    <dd>
                      {evidence.cost} {evidence.currency}
                    </dd>
                    <dt>Vendor</dt>
                    <dd>{evidence.selected_vendor_id || "No vendor"}</dd>
                    <dt>Closed</dt>
                    <dd>{when(evidence.closed_at)}</dd>
                    <dt>Source record</dt>
                    <dd>{evidence.case_id}</dd>
                  </dl>
                </>
              )
            )}
            {source && (
              <aside className="evidence-pane" aria-label="Source evidence">
                <button className="secondary" onClick={() => setSource(null)}>
                  Back to case
                </button>
                <h3>{source.title}</h3>
                <small>{when(source.at)}</small>
                <p style={{ whiteSpace: "pre-wrap" }}>{source.text}</p>
                <small>{source.source_id}</small>
              </aside>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
