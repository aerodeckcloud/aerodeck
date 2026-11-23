import { ControlPlaneError } from "@/lib/control-plane";
import { jsonError } from "@/lib/mock-http";

/** Pass a 422 through so the console shows the control-plane reason (#274). */
export function rollbackRefusalResponse(cause: unknown) {
  if (!(cause instanceof ControlPlaneError) || cause.status !== 422) {
    return null;
  }
  return jsonError(422, cause.message, cause.code ?? "unprocessable");
}
