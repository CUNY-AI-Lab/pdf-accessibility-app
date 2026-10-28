import { WorkerEntrypoint } from "cloudflare:workers";
import { z } from "zod";
import app from "../src/index";

export { JobBoard } from "../src/board";
export default app;

const modelRequest = z.object({ model: z.string() });

/**
 * The Gateway as the relay sees it. The request's model picks the answer:
 * "budget-spent" gets the Gateway's quota refusal; anything else succeeds and
 * echoes the credential the relay sent.
 */
export class GatewayDouble extends WorkerEntrypoint {
  override async fetch(request: Request): Promise<Response> {
    const body = modelRequest.parse(await request.json());
    const credential = request.headers.get("x-cail-identity-jwt");
    if (body.model === "budget-spent") {
      return Response.json(
        { error: { code: "quota_exceeded", message: "Budget used up" } },
        { status: 429, headers: { "x-should-retry": "false" } },
      );
    }
    return Response.json({
      choices: [{ message: { content: "{}" } }],
      seen: { url: request.url, credential, authorization: request.headers.get("authorization") },
    });
  }
}
