import { JOB_STATUSES, type JobStatus } from "../types";
export type DashboardFilter = JobStatus | undefined;
export function filterTransition(_current: DashboardFilter, next: DashboardFilter): DashboardFilter {
  if (next !== undefined && !JOB_STATUSES.includes(next)) throw new Error(`Unknown job filter: ${next}`);
  return next;
}
export type DisclosureState = "collapsed" | "expanded";
export function disclosureTransition(state: DisclosureState, event: "toggle"): DisclosureState {
  if (event !== "toggle") throw new Error(`Illegal disclosure event: ${event}`);
  return state === "collapsed" ? "expanded" : "collapsed";
}
