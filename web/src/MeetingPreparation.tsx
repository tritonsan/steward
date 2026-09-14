import { useState } from "react";

export function MeetingPreparation({
  preparation,
  agenda,
  caseId,
  api,
}: {
  preparation?: any;
  agenda: any;
  caseId: string;
  api: (path: string) => Promise<any>;
}) {
  const [source, setSource] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  async function inspect(id: string) {
    setLoading(true);
    setError("");
    setSource(null);
    try {
      setSource(
        await api(`/cases/${caseId}/sources/${encodeURIComponent(id)}`),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }
  return (
    <section className="meeting-preparation" aria-label="Meeting preparation">
      <div className="preparation-heading">
        <h3>Meeting preparation</h3>
        <span className="badge">Discussion draft</span>
      </div>
      <p className="muted">
        Prepared from community evidence. Options below await a community
        decision.
      </p>
      <h4>Topic</h4>
      <p>{agenda.summary}</p>
      <h4>Proposed agenda</h4>
      <ol>
        {agenda.agenda_items?.map((item: any, i: number) => (
          <li key={i}>
            <strong>{item.title}</strong>
            <p>{item.detail}</p>
          </li>
        ))}
      </ol>
      {!!preparation?.history?.length && (
        <details>
          <summary>
            What happened before · {preparation.history.length} records
          </summary>
          {preparation.history.map((item: any) => (
            <div className="reason" key={item.case_id}>
              <strong>{item.title}</strong>
              <p>{item.work_performed}</p>
              <p>{item.resolution_notes}</p>
              <small>
                {item.outcome_verified
                  ? "Verified historical outcome"
                  : "Historical record · verification not established"}
              </small>
              <button
                className="secondary"
                onClick={() => inspect(item.case_id)}
              >
                Read historical source
              </button>
            </div>
          ))}
        </details>
      )}
      {!!agenda.solution_options?.length && (
        <>
          <h4>Options to discuss</h4>
          {agenda.solution_options.map((option: any, i: number) => (
            <div className="reason" key={i}>
              <strong>{option.title}</strong>
              <p>{option.detail}</p>
              <p>
                <b>Tradeoffs:</b> {option.tradeoffs}
              </p>
            </div>
          ))}
        </>
      )}
      {!!agenda.open_questions?.length && (
        <>
          <h4>Questions to resolve</h4>
          <ul>
            {agenda.open_questions.map((item: any, i: number) => (
              <li key={i}>{item.question}</li>
            ))}
          </ul>
        </>
      )}
      <h4>Potential quote requests</h4>
      <p className="muted">
        Planning only. These are not received quotes or authorization to contact
        a supplier.
      </p>
      {agenda.quote_needs?.length ? (
        agenda.quote_needs.map((need: any, i: number) => (
          <div className="reason" key={i}>
            <strong>{need.scope}</strong>
            <p>{need.reason}</p>
            <p>
              <b>Clarify first:</b> {need.question}
            </p>
            {!!need.vendor_ids?.length && (
              <p>
                Directory options:{" "}
                {need.vendor_ids
                  .map(
                    (id: string) =>
                      preparation?.vendor_options?.find(
                        (vendor: any) => vendor.vendor_id === id,
                      )?.name || id,
                  )
                  .join(", ")}
              </p>
            )}
          </div>
        ))
      ) : (
        <p>
          No external quote need identified. Reassess if the agreed scope
          requires physical work.
        </p>
      )}
      <details>
        <summary>Review supporting evidence</summary>
        <div className="button-row">
          {agenda.summary_source_ids?.map((id: string, i: number) => (
            <button
              className="secondary"
              key={id}
              disabled={loading}
              onClick={() => inspect(id)}
            >
              Read source {i + 1}
            </button>
          ))}
        </div>
      </details>
      {loading && <p role="status">Loading source…</p>}
      {error && <p role="alert">{error}</p>}
      {source && (
        <aside className="reason" aria-label="Supporting source">
          <strong>{source.title}</strong>
          <p>{source.text}</p>
          <button className="secondary" onClick={() => setSource(null)}>
            Close source
          </button>
        </aside>
      )}
    </section>
  );
}
