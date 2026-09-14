import { useEffect, useState } from "react";
export function VendorSimulation({
  api,
  cases,
  onChanged,
}: {
  api: (path: string, options?: RequestInit) => Promise<any>;
  cases: { case_id: string; title: string }[];
  onChanged: () => void;
}) {
  const [vendors, setVendors] = useState<any[]>([]),
    [caseId, setCaseId] = useState(""),
    [vendorId, setVendorId] = useState(""),
    [text, setText] = useState(""),
    [busy, setBusy] = useState(false),
    [status, setStatus] = useState("");
  useEffect(() => {
    api("/directory")
      .then((d) => setVendors(d.vendors))
      .catch((e) => setStatus(e.message));
  }, []);
  async function submit() {
    setBusy(true);
    try {
      await api("/simulation/vendor-replies", {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ case_id: caseId, vendor_id: vendorId, text }),
      });
      setStatus("Vendor reply entered through the mail intake service.");
      setText("");
      onChanged();
    } catch (e) {
      setStatus((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section>
      <h3>Vendor response</h3>
      <p>This actor supplies a reply; it does not set the case outcome.</p>
      <label>
        Case
        <select value={caseId} onChange={(e) => setCaseId(e.target.value)}>
          <option value="">Choose a case</option>
          {cases.map((c) => (
            <option key={c.case_id} value={c.case_id}>
              {c.title}
            </option>
          ))}
        </select>
      </label>
      <label>
        Vendor
        <select value={vendorId} onChange={(e) => setVendorId(e.target.value)}>
          <option value="">Choose a vendor</option>
          {vendors.map((v) => (
            <option key={v.vendor_id} value={v.vendor_id}>
              {v.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Reply text
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Quote in SGD, scope, exclusions, onsite time and validity."
        />
      </label>
      <button
        disabled={busy || !caseId || !vendorId || !text.trim()}
        onClick={submit}
      >
        Add vendor reply
      </button>
      {status && <p role="status">{status}</p>}
    </section>
  );
}
