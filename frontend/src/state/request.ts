export type ResponseOutcome =
  | { status: "success" }
  | { status: "redirect"; launch: string }
  | { status: "failed"; detail: string };

/** Doorway authentication is a separate outcome from an application failure. */
export function responseOutcome(httpStatus: number, detail: string, launch: unknown): ResponseOutcome {
  if (httpStatus >= 200 && httpStatus < 300) return { status: "success" };
  if (httpStatus === 401 && typeof launch === "string" && launch.startsWith("/launch/")) return { status: "redirect", launch };
  return { status: "failed", detail };
}
