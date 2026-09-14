import { beforeEach, describe, expect, it, vi } from "vitest";
import { finishSignIn, startSignIn } from "./auth";

const config = { domain: "community.auth.example", client_id: "public-client" };
let entries: Map<string, string>;
let assign: ReturnType<typeof vi.fn>;
beforeEach(() => {
  entries = new Map();
  assign = vi.fn();
  vi.stubGlobal("sessionStorage", {
    getItem: (k: string) => entries.get(k) ?? null,
    setItem: (k: string, v: string) => entries.set(k, v),
    removeItem: (k: string) => entries.delete(k),
  });
  vi.stubGlobal("location", {
    origin: "https://steward.example",
    pathname: "/",
    search: "",
    assign,
  });
  vi.stubGlobal("history", { replaceState: vi.fn() });
  vi.stubGlobal("fetch", vi.fn());
});

describe("Cognito browser sign-in boundary", () => {
  it("shows an actionable error after a cancelled provider sign-in", async () => {
    entries.set(
      "steward-oauth",
      JSON.stringify({ state: "mine", verifier: "private" }),
    );
    location.search = "?error=access_denied&state=mine";
    await expect(finishSignIn(config)).rejects.toThrow("not completed");
    expect(entries.has("steward-oauth")).toBe(false);
    expect(history.replaceState).toHaveBeenCalledWith({}, "", "/");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("does not establish an empty session from a malformed token response", async () => {
    entries.set(
      "steward-oauth",
      JSON.stringify({ state: "mine", verifier: "private" }),
    );
    location.search = "?code=one-use&state=mine";
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => ({}),
    } as Response);
    await expect(finishSignIn(config)).rejects.toThrow("no session");
  });

  it("uses a fresh PKCE verifier and S256 challenge without a client secret", async () => {
    await startSignIn(config);
    const first = JSON.parse(entries.get("steward-oauth")!);
    const url = new URL(assign.mock.calls[0][0]);
    expect(url.protocol).toBe("https:");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("state")).toBe(first.state);
    expect(url.searchParams.has("client_secret")).toBe(false);
    expect(url.searchParams.get("code_challenge")).not.toBe(first.verifier);
    await startSignIn(config);
    expect(JSON.parse(entries.get("steward-oauth")!).verifier).not.toBe(
      first.verifier,
    );
  });

  it("rejects a callback from a different sign-in attempt before token exchange", async () => {
    entries.set(
      "steward-oauth",
      JSON.stringify({ state: "mine", verifier: "private" }),
    );
    location.search = "?code=untrusted&state=someone-else";
    await expect(finishSignIn(config)).rejects.toThrow("could not be verified");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("consumes matching state once and removes the authorization code from the address", async () => {
    entries.set(
      "steward-oauth",
      JSON.stringify({ state: "mine", verifier: "private" }),
    );
    location.search = "?code=one-use&state=mine";
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: async () => ({ id_token: "session-token" }),
    } as Response);
    expect(await finishSignIn(config)).toBe("session-token");
    expect(entries.has("steward-oauth")).toBe(false);
    expect(history.replaceState).toHaveBeenCalledWith({}, "", "/");
    const request = vi.mocked(fetch).mock.calls[0][1]!;
    expect((request.body as URLSearchParams).get("code_verifier")).toBe(
      "private",
    );
    await expect(finishSignIn(config)).rejects.toThrow("could not be verified");
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
