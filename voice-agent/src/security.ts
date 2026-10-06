const COOKIE = "shopping_voice";

export function localRequest(request: Request, origin: string): boolean {
  const expected = new URL(origin);
  const actual = new URL(request.url);
  if (expected.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(expected.hostname)) return false;
  if (actual.origin !== expected.origin) return false;
  const supplied = request.headers.get("Origin");
  if (supplied && supplied !== origin) return false;
  const site = request.headers.get("Sec-Fetch-Site");
  if (site && site !== "same-origin" && site !== "none") return false;
  return (request.method === "GET" && request.headers.get("Upgrade") !== "websocket") || supplied === origin;
}

async function key(secret: string) {
  if (secret.length < 32) throw new Error("Local session secret missing.");
  return crypto.subtle.importKey("raw", new TextEncoder().encode(secret), {name: "HMAC", hash: "SHA-256"}, false, ["sign", "verify"]);
}

export async function sessionCookie(secret: string): Promise<string> {
  const expiry = Math.floor(Date.now() / 1000) + 12 * 60 * 60;
  const payload = String(expiry);
  const signature = await crypto.subtle.sign("HMAC", await key(secret), new TextEncoder().encode(payload));
  const hex = Array.from(new Uint8Array(signature), byte => byte.toString(16).padStart(2, "0")).join("");
  return `${COOKIE}=${payload}.${hex}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200`;
}

export async function authenticated(request: Request, secret: string): Promise<boolean> {
  const value = request.headers.get("Cookie")?.split(";").map(part => part.trim()).find(part => part.startsWith(COOKIE + "="))?.slice(COOKIE.length + 1);
  const match = /^(\d{10})\.([a-f0-9]{64})$/.exec(value ?? "");
  if (!match || Number(match[1]) < Date.now() / 1000 || Number(match[1]) > Date.now() / 1000 + 43200) return false;
  const signature = Uint8Array.from(match[2].match(/../g)!, byte => parseInt(byte, 16));
  return crypto.subtle.verify("HMAC", await key(secret), signature, new TextEncoder().encode(match[1]));
}
