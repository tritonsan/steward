import { useEffect, useState, type ReactNode } from "react";
import { ArrowLeft, ArrowRight, ShieldCheck } from "lucide-react";
import { BrandLogo } from "./BrandLogo";
import "./ReviewAccess.css";

export type ReviewSession = {
  token: string;
  actor_id: string;
  role: "manager" | "resident";
};
const sessionKey = "steward-review-session";
const codeKey = "steward-review-access";

export function ReviewAccess({
  children,
}: {
  children: (session: ReviewSession, signOut: () => void) => ReactNode;
}) {
  const [session, setSession] = useState<ReviewSession | null>(null);
  const [code, setCode] = useState("");
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    fetch("/api/review/config")
      .then(async (response) => {
        if (!response.ok)
          throw new Error(
            "Judge access is temporarily unavailable. Please retry.",
          );
        const config = await response.json();
        if (!active) return;
        setEnabled(config.enabled === true);
        try {
          const saved = JSON.parse(
            sessionStorage.getItem(sessionKey) || "null",
          );
          if (
            config.enabled &&
            saved?.token &&
            ["manager", "resident"].includes(saved.role)
          )
            setSession(saved);
        } catch {
          sessionStorage.removeItem(sessionKey);
        }
      })
      .catch((failure) => {
        if (active) setError(failure.message);
      });
    return () => {
      active = false;
    };
  }, []);
  function signOut() {
    sessionStorage.removeItem(sessionKey);
    sessionStorage.removeItem(codeKey);
    setSession(null);
    setCode("");
    setError("");
  }
  async function enter(role: ReviewSession["role"], accessCode = code) {
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/review/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ access_code: accessCode.trim(), role }),
      });
      const body = await response.json();
      if (!response.ok)
        throw new Error(
          typeof body.detail === "string"
            ? body.detail
            : "Unable to open the review workspace.",
        );
      sessionStorage.setItem(sessionKey, JSON.stringify(body));
      sessionStorage.setItem(codeKey, accessCode.trim());
      setSession(body);
      setCode("");
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }
  if (!session)
    return (
      <main className="review-login">
        <a className="review-back" href="?mode=preview">
          <ArrowLeft size={16} /> Public preview
        </a>
        <div className="review-login-card">
          <BrandLogo size={104} />
          <span className="review-eyebrow">HACKATHON REVIEW WORKSPACE</span>
          <h1>Try Steward for yourself.</h1>
          <p>
            Explore the working application, make decisions, and follow what
            happens next.
          </p>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void enter("manager");
            }}
          >
            <label htmlFor="review-code">Judge access code</label>
            <input
              id="review-code"
              type="password"
              autoComplete="off"
              required
              value={code}
              onChange={(event) => setCode(event.target.value)}
              aria-describedby="review-code-help"
            />
            <small id="review-code-help">
              Provided in the Devpost testing instructions. No account or email
              verification needed.
            </small>
            <button disabled={busy || enabled !== true}>
              {busy
                ? "Opening workspace…"
                : enabled === null
                  ? "Checking availability…"
                  : "Open review workspace"}
              <ArrowRight size={16} />
            </button>
          </form>
          {enabled === false && (
            <p role="status">
              The review workspace has not been enabled yet. The public preview
              is available now.
            </p>
          )}
          {error && (
            <div role="alert" className="error">
              <p>{error}</p>
              {enabled === null && (
                <button className="secondary" onClick={() => location.reload()}>
                  Retry connection
                </button>
              )}
            </div>
          )}
          <div className="review-explainer">
            <ShieldCheck size={20} />
            <p>
              Synthetic Northgate records. Working backend and AI. Email and
              Telegram delivery are simulated in this workspace.
            </p>
          </div>
          <p className="review-small">
            Reviewers share this demo community. Changes persist, so you may see
            another reviewer’s work.
          </p>
        </div>
      </main>
    );
  return (
    <div className="review-shell">
      <div className="review-banner">
        <div>
          <ShieldCheck size={18} />
          <strong>Judge workspace</strong>
          <span>Real workflows · Synthetic community · Simulated delivery</span>
        </div>
        <div>
          <label htmlFor="review-role">View as</label>
          <select
            id="review-role"
            value={session.role}
            disabled={busy}
            onChange={(event) =>
              void enter(
                event.target.value as ReviewSession["role"],
                sessionStorage.getItem(codeKey) || "",
              )
            }
          >
            <option value="manager">Manager</option>
            <option value="resident">Resident</option>
          </select>
          <a href="?mode=preview">Public preview</a>
        </div>
      </div>
      {error && (
        <p role="alert" className="review-error">
          {error}
        </p>
      )}
      <details className="review-guide">
        <summary>Quick testing guide</summary>
        <ol>
          <li>
            Open Cases. Inspect a prepared case, its source messages, next step
            and history.
          </li>
          <li>
            Make a decision with a reason. Steward’s worker follows up in the
            background.
          </li>
          <li>
            Switch to Resident to view invitations and personal response cards.
          </li>
          <li>
            Use Simulation studio to add a resident message or vendor reply and
            advance demo time. New messages use the deployed AI.
          </li>
        </ol>
        <p>
          Changes are shared and persistent. Delivery stays inside this demo.
          The two starting cases use prepared sample model outputs. New messages
          use the deployed AI and may take a minute. Memory here searches the
          synthetic history by category, asset and text.
        </p>
      </details>
      {children(session, signOut)}
    </div>
  );
}
