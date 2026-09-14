export type AuthConfig = { domain: string | null; client_id: string | null };
const base64url = (bytes: Uint8Array) =>
  btoa(String.fromCharCode(...bytes))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
export async function startSignIn(config: AuthConfig) {
  if (!config.domain || !config.client_id)
    throw new Error("Community sign-in is not configured.");
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(32))),
    state = crypto.randomUUID();
  sessionStorage.setItem("steward-oauth", JSON.stringify({ verifier, state }));
  const challenge = base64url(
    new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)),
    ),
  );
  const url = new URL("https://" + config.domain + "/oauth2/authorize");
  url.search = new URLSearchParams({
    client_id: config.client_id,
    response_type: "code",
    scope: "openid email profile",
    redirect_uri: location.origin + "/",
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  }).toString();
  location.assign(url);
}
export async function finishSignIn(config: AuthConfig): Promise<string | null> {
  const query = new URLSearchParams(location.search),
    code = query.get("code");
  if (query.has("error")) {
    sessionStorage.removeItem("steward-oauth");
    history.replaceState({}, "", location.pathname);
    throw new Error("Sign-in was not completed. Please try again.");
  }
  if (!code) return null;
  let saved;
  try {
    saved = JSON.parse(sessionStorage.getItem("steward-oauth") || "null");
  } catch {
    sessionStorage.removeItem("steward-oauth");
    throw new Error("Sign-in could not be verified. Please try again.");
  }
  if (!saved || query.get("state") !== saved.state)
    throw new Error("Sign-in could not be verified. Please try again.");
  sessionStorage.removeItem("steward-oauth");
  history.replaceState({}, "", location.pathname);
  const r = await fetch("https://" + config.domain + "/oauth2/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: config.client_id!,
      code,
      redirect_uri: location.origin + "/",
      code_verifier: saved.verifier,
    }),
  });
  if (!r.ok) throw new Error("Sign-in expired. Please try again.");
  const result = await r.json();
  if (typeof result.id_token !== "string" || !result.id_token)
    throw new Error("Sign-in returned no session. Please try again.");
  return result.id_token;
}
