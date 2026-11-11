/** Gateway eligibility is evaluated only in the Worker; no token goes to the runner. */
export const CREDENTIAL_LIMITS = {
  queueMinimumMs: 60 * 60_000,
  claimMinimumMs: 15 * 60_000,
  gatewayMarginMs: 10 * 60_000,
};
export type CredentialEligibility = "not_required" | "missing" | "queue_eligible" | "claim_eligible" | "relay_only" | "expired";

export function credentialEligibility(kind: string, token: string | null, expires: number | null, now: number): CredentialEligibility {
  if (kind === "sync") return "not_required";
  if (token === null || token === "" || expires === null) return "missing";
  const remaining = expires - now;
  if (remaining >= CREDENTIAL_LIMITS.queueMinimumMs) return "queue_eligible";
  if (remaining >= CREDENTIAL_LIMITS.claimMinimumMs) return "claim_eligible";
  if (remaining > CREDENTIAL_LIMITS.gatewayMarginMs) return "relay_only";
  return "expired";
}
