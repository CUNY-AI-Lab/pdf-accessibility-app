import type { Env as WorkerEnv } from "../src/env";

declare global {
  namespace Cloudflare {
    interface Env extends WorkerEnv {
      TEST_PRIVATE_JWK: string;
    }
  }
}
