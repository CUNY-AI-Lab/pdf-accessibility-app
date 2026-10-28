/**
 * Who is asking. Doorway signs people in and forwards two legs: the app leg
 * (audience cail:pdf-accessibility) and the cail:gateway leg the Worker uses
 * for that person's model calls. Both are verified here; the Gateway leg's
 * own expiry decides whether work can still start.
 */
import {
  CAIL_GATEWAY_AUDIENCE,
  createCailAuthError,
  loadIdentityVerifierConfig,
  readIdentityKeyring,
  serializeCailAuthError,
  verifyIdentityJwt,
  verifyKeyringGatewayJwt,
  type IdentityVerifierConfig,
} from "@cuny-ai-lab/cail-identity";
import { z } from "zod";
import type { Env } from "./env";

export const APP_AUDIENCE = "cail:pdf-accessibility";
export const LAUNCH_PATH = "/launch/pdf-accessibility";

export interface Person {
  subject: string;
  gatewayJwt: string;
  /** When the Gateway leg expires, in milliseconds since the epoch. */
  gatewayExpiresAt: number;
}

export type AuthResult = { ok: true; person: Person } | { ok: false; response: Response };

const JSON_HEADERS = { "content-type": "application/json", "cache-control": "no-store" };

function signInRequired(): Response {
  const body = createCailAuthError("authentication_required", "Sign in to continue.", LAUNCH_PATH);
  return new Response(serializeCailAuthError(body), {
    status: 401,
    headers: { ...JSON_HEADERS, "www-authenticate": 'Bearer realm="CAIL"' },
  });
}

function misconfigured(): Response {
  const body = createCailAuthError(
    "identity_verification_misconfigured",
    "PDF Accessibility is not set up correctly right now. Email ailab@gc.cuny.edu.",
  );
  return new Response(serializeCailAuthError(body), { status: 503, headers: JSON_HEADERS });
}

const configCache = new Map<string, IdentityVerifierConfig>();

async function verifierConfig(env: Env, audience: string): Promise<IdentityVerifierConfig | null> {
  const key = `${audience}:${env.CAIL_IDENTITY_JWKS}:${env.CAIL_IDENTITY_ISSUER}`;
  const cached = configCache.get(key);
  if (cached) return cached;
  const loaded = await loadIdentityVerifierConfig({
    jwks: env.CAIL_IDENTITY_JWKS,
    issuer: env.CAIL_IDENTITY_ISSUER,
    expectedAudience: audience,
  });
  if (!loaded.ok) return null;
  configCache.set(key, loaded.config);
  return loaded.config;
}

const expiryClaims = z.object({ exp: z.number().int() });

/** The `exp` of a token the verifier has already accepted. */
function expiresAt(token: string): number | null {
  const payload = token.split(".")[1];
  if (payload === undefined) return null;
  try {
    const decoded = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    const parsed = expiryClaims.safeParse(JSON.parse(decoded));
    return parsed.success ? parsed.data.exp * 1000 : null;
  } catch {
    return null;
  }
}

export async function authenticate(request: Request, env: Env): Promise<AuthResult> {
  const [appConfig, gatewayConfig] = await Promise.all([
    verifierConfig(env, APP_AUDIENCE),
    verifierConfig(env, CAIL_GATEWAY_AUDIENCE),
  ]);
  if (appConfig === null || gatewayConfig === null) return { ok: false, response: misconfigured() };

  const keyring = readIdentityKeyring(request.headers);
  if (keyring === null || keyring.gatewayJwt === undefined) return { ok: false, response: signInRequired() };
  const identity = await verifyIdentityJwt(keyring.appJwt, appConfig);
  if (identity === null) return { ok: false, response: signInRequired() };
  const gateway = await verifyKeyringGatewayJwt(keyring, gatewayConfig, identity.subject);
  const gatewayExpiresAt = expiresAt(keyring.gatewayJwt);
  if (gateway === null || gatewayExpiresAt === null) return { ok: false, response: signInRequired() };

  return {
    ok: true,
    person: { subject: identity.subject, gatewayJwt: keyring.gatewayJwt, gatewayExpiresAt },
  };
}
