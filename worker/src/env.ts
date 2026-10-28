import type { JobBoard } from "./board";

export interface Env {
  ASSETS: Fetcher;
  GATEWAY: Fetcher;
  FILES: R2Bucket;
  JOB_BOARD: DurableObjectNamespace<JobBoard>;
  CAIL_API_BASE: string;
  CAIL_IDENTITY_ISSUER: string;
  CAIL_IDENTITY_JWKS: string;
  CAIL_CANONICAL_ORIGIN: string;
  BASE_PATH: string;
  RUNNER_TOKEN: string;
}

/** The one JobBoard: every job, the queue, and the limits across them. */
export function jobBoard(env: Env): DurableObjectStub<JobBoard> {
  return env.JOB_BOARD.get(env.JOB_BOARD.idFromName("jobs"));
}
