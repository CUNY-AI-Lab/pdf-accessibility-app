import { generateKeyPairSync } from "node:crypto";
import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

// The production Worker (wrangler.jsonc) runs in workerd. test/worker.ts adds
// a Gateway double the GATEWAY binding points to; identity tokens are signed
// per run with a key whose public half is the Worker's CAIL_IDENTITY_JWKS.
const { publicKey, privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const kid = "pdf-accessibility-test";
const publicJwk = { ...publicKey.export({ format: "jwk" }), kid, alg: "RS256", use: "sig" };
const privateJwk = { ...privateKey.export({ format: "jwk" }), kid, alg: "RS256" };

export default defineConfig({
  plugins: [
    cloudflareTest({
      main: "./test/worker.ts",
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        bindings: {
          CAIL_IDENTITY_JWKS: JSON.stringify({ keys: [publicJwk] }),
          RUNNER_TOKEN: "runner-secret",
          TEST_PRIVATE_JWK: JSON.stringify(privateJwk),
        },
        serviceBindings: { GATEWAY: { name: "cail-pdf-accessibility", entrypoint: "GatewayDouble" } },
      },
    }),
  ],
  test: { include: ["test/**/*.test.ts"] },
});
