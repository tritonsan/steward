import { formatDate } from "./dateTime";
import { useEffect, useState } from "react";

export function Suggestions({
  api,
  onChanged,
}: {
  api: (path: string, options?: RequestInit) => Promise<any>;
  onChanged: () => void;
}) {
  const [items, setItems] = useState<any[]>([]),
    [selected, setSelected] = useState(""),
    [notes, setNotes] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const load = () =>
    api("/suggestions")
      .then(setItems)
      .catch((e) => setError(e.message));
  useEffect(() => {
    load();
  }, []);
  async function decide(accepted: boolean) {
    setBusy(true);
    setError("");
    try {
      await api("/suggestions/" + selected + "/decision", {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ accepted, notes }),
      });
      setSelected("");
      setNotes("");
      await load();
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const pending = items.filter((s) => s.status === "suggested");
  if (!pending.length && !error) return null;
  return (
    <section className="suggestions">
      <h2>Prevent the next problem</h2>
      <p>
        Suggestions from recorded experience. You decide whether to start a
        review.
      </p>
      {error && <p role="alert">{error}</p>}
      {pending.map((s) => (
        <article className="reason" key={s.suggestion_id}>
          <h3>{s.title}</h3>
          <p>
            {s.reason.startsWith("Playbook rule")
              ? "Seasonal maintenance is due. Review the prior work before scheduling."
              : s.reason}
          </p>
          <small>
            {s.source_case_ids.length} source records · Review by{" "}
            {formatDate(s.due_at)}
          </small>
          {selected === s.suggestion_id ? (
            <>
              <label>
                Decision notes
                <textarea
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                />
              </label>
              <div className="button-row">
                <button
                  disabled={busy || !notes.trim()}
                  onClick={() => decide(true)}
                >
                  Start maintenance review
                </button>
                <button
                  className="secondary"
                  disabled={busy || !notes.trim()}
                  onClick={() => decide(false)}
                >
                  Dismiss with reason
                </button>
              </div>
            </>
          ) : (
            <button
              className="text-button"
              onClick={() => setSelected(s.suggestion_id)}
            >
              Review suggestion
            </button>
          )}
        </article>
      ))}
    </section>
  );
}
