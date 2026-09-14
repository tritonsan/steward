import { formatDateTime } from "./dateTime";
import { useRef, useState } from "react";

export type PersonalQuestion = {
  request_id: string;
  case_id: string;
  kind: string;
  title: string;
  version: number;
  due_at: string;
};
type Api = (path: string, options?: RequestInit) => Promise<any>;
export function TelegramLink({ api }: { api: Api }) {
  const [url, setUrl] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  async function connect() {
    setBusy(true);
    try {
      setUrl((await api("/telegram/link", { method: "POST" })).url);
      setError("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="telegram-link">
      <button className="secondary" onClick={connect} disabled={busy}>
        {busy ? "Preparing your link…" : "Connect my Telegram account"}
      </button>
      {url && (
        <a href={url} target="_blank" rel="noreferrer">
          Open your private bot chat (link expires in 10 minutes)
        </a>
      )}
      {error && <p role="status">{error}</p>}
    </div>
  );
}
export function PersonalRequest({
  question,
  api,
  reload,
}: {
  question: PersonalQuestion;
  api: Api;
  reload: () => Promise<void>;
}) {
  const [notes, setNotes] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const key = useRef<string | null>(null);
  async function answer(accepted: boolean) {
    setBusy(true);
    setError("");
    key.current ??= crypto.randomUUID();
    try {
      await api(`/cases/${question.case_id}/commands`, {
        method: "POST",
        headers: { "Idempotency-Key": key.current },
        body: JSON.stringify({
          action:
            question.kind === "clarification"
              ? "answer_clarification"
              : question.kind === "appointment_access"
                ? "appointment_access"
                : "verify",
          expected_version: question.version,
          notes,
          accepted,
          data: {
            request_id: question.request_id,
            task_id: question.request_id,
          },
        }),
      });
      key.current = null;
      await reload();
    } catch (e) {
      setError((e as Error).message);
      key.current = null;
      await reload();
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="personal-request" aria-label="Your response is needed">
      <p className="eyebrow">A QUESTION FOR YOU</p>
      <h3>{question.title}</h3>
      <p>Response requested by {formatDateTime(question.due_at)}.</p>
      <label htmlFor={question.request_id}>
        Your answer and what you observed
      </label>
      <textarea
        id={question.request_id}
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        disabled={busy}
        maxLength={5000}
      />
      <div className="request-actions">
        <button disabled={busy || !notes.trim()} onClick={() => answer(true)}>
          {busy
            ? "Saving…"
            : question.kind === "clarification"
              ? "Send answer"
              : "Confirm"}
        </button>
        {question.kind !== "clarification" && (
          <button
            className="secondary"
            disabled={busy || !notes.trim()}
            onClick={() => answer(false)}
          >
            Decline with reason
          </button>
        )}
      </div>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
