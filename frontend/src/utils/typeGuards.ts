/** Safely narrow an unknown value to a finite number, or null. */
export function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Safely narrow an unknown value to a boolean, or null. */
export function asBool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

/** Safely narrow an unknown value to a non-empty string, or null. */
export function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}
