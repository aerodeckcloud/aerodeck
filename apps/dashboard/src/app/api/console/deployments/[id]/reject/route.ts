import { connection, NextResponse } from "next/server";

import { postDeploymentOwnerDecision } from "@/lib/console-deployment-decision";
import { requireSessionUser } from "@/lib/session";

export async function POST(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  await connection();
  const user = await requireSessionUser();
  if (user instanceof NextResponse) {
    return user;
  }
  const { id } = await context.params;
  return postDeploymentOwnerDecision(id, "reject", user.id);
}
