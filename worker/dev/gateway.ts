/**
 * Local development only: stands in for the Gateway Worker (cail-model-api)
 * behind the GATEWAY binding. It swaps the person's identity leg, which
 * only a real Doorway can sign, for the developer's own Gateway key and
 * forwards the request to the real Gateway. Never deployed.
 */
interface DevEnv {
  UPSTREAM: string;
  DEV_GATEWAY_KEY: string;
}

export default {
  async fetch(request: Request, env: DevEnv): Promise<Response> {
    const url = new URL(request.url);
    const headers = new Headers(request.headers);
    headers.delete("x-cail-identity-jwt");
    headers.set("authorization", `Bearer ${env.DEV_GATEWAY_KEY}`);
    return fetch(`${env.UPSTREAM}${url.pathname}`, { method: request.method, headers, body: request.body });
  },
} satisfies ExportedHandler<DevEnv>;
