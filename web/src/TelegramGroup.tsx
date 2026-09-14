import { formatDateTime } from "./dateTime";
import { useEffect, useState } from "react";
import { TelegramLink } from "./PersonalRequest";

type Api = (path: string, options?: RequestInit) => Promise<any>;
type GroupStatus = {
  configured: boolean;
  personal_linked: boolean;
  version: number;
  bot_username: string | null;
  last_issue: string | null;
  delivery_mode: "dry_run" | "live";
  delivery_paused: boolean;
  test_delivery: {
    status: string;
    delivered_at: string | null;
    provider_message_id: string | null;
    error: string | null;
  } | null;
  connection: { title: string; status: string; reason: string } | null;
  recent_messages: {
    received_at: string;
    text: string;
    status: string;
    case_ids: string[];
    error: string | null;
  }[];
};

export function TelegramGroup({ api }: { api: Api }) {
  const [status, setStatus] = useState<GroupStatus | null>(null);
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  useEffect(() => {
    let stopped = false;
    async function refresh() {
      try {
        const next = await api("/telegram/group");
        if (!stopped) setStatus(next);
      } catch (e) {
        if (!stopped) setError((e as Error).message);
      }
    }
    void refresh();
    const timer = setInterval(refresh, 5000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, []);
  async function change(action: "connect" | "disconnect" | "test_delivery") {
    if (!status) return;
    setBusy(true);
    setError("");
    try {
      const result = await api("/telegram/group", {
        method: "POST",
        body: JSON.stringify({ action, expected_version: status.version }),
      });
      setUrl(result.url || "");
      setConfirmDisconnect(false);
      setStatus(await api("/telegram/group"));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const connected = status?.connection?.status === "connected";
  return (
    <section className="telegram-group" aria-labelledby="telegram-group-title">
      <h3 id="telegram-group-title">Community Telegram group</h3>
      <p>
        Connect one group to Northgate. Group messages from linked members enter
        Steward’s workflow.
      </p>
      {!status ? (
        <p role="status">Loading connection…</p>
      ) : !status.configured ? (
        <p>
          The application’s Telegram bot is not configured yet. Your operator
          needs to enable it before a group can be connected. The demo can still
          use simulated messages.
        </p>
      ) : (
        <>
          <p>
            <strong>
              {connected ? status.connection!.title : "No active group"}
            </strong>{" "}
            · @{status.bot_username}
          </p>
          {!status.personal_linked ? (
            <>
              <p>
                First, link the Telegram account you use to manage the group.
              </p>
              <TelegramLink api={api} />
            </>
          ) : (
            <p>Your Telegram account is linked.</p>
          )}
          {!connected && status.personal_linked && (
            <>
              <p>
                Choose your group in Telegram, add Steward as an administrator,
                then open the link again if prompted. Your own account must also
                be a group administrator. A group invite URL alone cannot
                connect the bot.
              </p>
              <button disabled={busy} onClick={() => change("connect")}>
                Choose a Telegram group
              </button>
              {url && (
                <p>
                  <a href={url} target="_blank" rel="noreferrer">
                    Open Telegram to select your group
                  </a>
                  <br />
                  <small>This personal setup link expires in 10 minutes.</small>
                </p>
              )}
            </>
          )}
          {connected && (
            <>
              <p role="status">
                {status.delivery_paused
                  ? "Sending is paused by the emergency stop."
                  : status.delivery_mode === "live"
                    ? "Group notifications are enabled. Messages are sent to Telegram."
                    : "Sending is simulated. No outgoing messages reach Telegram."}
              </p>
              {status.delivery_mode === "live" && !status.test_delivery && (
                <>
                  <p>
                    Send one connection test to this group. It is clearly
                    labeled and does not create a case or service order.
                  </p>
                  <button
                    disabled={busy || status.delivery_paused}
                    onClick={() => change("test_delivery")}
                  >
                    {busy ? "Queuing test…" : "Send connection test"}
                  </button>
                </>
              )}
              {status.test_delivery && (
                <p role="status">
                  {status.test_delivery.status === "delivered"
                    ? `Connection test delivered to Telegram · ${formatDateTime(status.test_delivery.delivered_at!)}`
                    : ["pending", "dispatching"].includes(
                          status.test_delivery.status,
                        )
                      ? "Connection test queued. Waiting for delivery…"
                      : status.test_delivery.status === "ambiguous"
                        ? "Test delivery is uncertain. Check the group; Steward will not send it again automatically."
                        : status.test_delivery.status === "simulated"
                          ? "Connection test was simulated; no message was sent."
                          : "Connection test needs attention. Check the bot’s group permissions and connection."}
                </p>
              )}
              <p>
                Group permissions were verified at connection. Send an English
                maintenance message from your linked account; receipt and
                processing will appear below. Other residents must link their
                accounts before their messages are accepted.
              </p>
              {!confirmDisconnect ? (
                <button
                  className="secondary"
                  onClick={() => setConfirmDisconnect(true)}
                >
                  Disconnect group
                </button>
              ) : (
                <div className="button-row">
                  <span>
                    Stop receiving new messages and sending group updates?
                  </span>
                  <button disabled={busy} onClick={() => change("disconnect")}>
                    Confirm disconnect
                  </button>
                  <button
                    className="secondary"
                    onClick={() => setConfirmDisconnect(false)}
                  >
                    Keep connected
                  </button>
                </div>
              )}
            </>
          )}
          {status.last_issue && <p role="status">{status.last_issue}</p>}
          {status.connection?.status === "disconnected" && (
            <p>{status.connection.reason}</p>
          )}
          <h4>Recent group messages</h4>
          {!status.recent_messages.length ? (
            <p>
              No group messages received yet. This panel checks for updates
              every five seconds.
            </p>
          ) : (
            <ol className="telegram-receipts">
              {status.recent_messages.map((message, index) => (
                <li key={index}>
                  <p>{message.text}</p>
                  <small>
                    {formatDateTime(message.received_at)} ·{" "}
                    {message.status.replaceAll("_", " ")}
                  </small>
                  {!!message.case_ids.length && (
                    <p>Case: {message.case_ids.join(", ")}</p>
                  )}
                  {message.error && (
                    <p role="status">
                      Processing needs attention: {message.error}
                    </p>
                  )}
                </li>
              ))}
            </ol>
          )}
        </>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
