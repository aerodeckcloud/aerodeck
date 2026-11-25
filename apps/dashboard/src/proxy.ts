import { NextResponse, type NextRequest } from "next/server";

import { anonymousEntryPath } from "@/lib/public-paths";
import { SESSION_COOKIE } from "@/lib/session-cookie";

export function proxy(request: NextRequest) {
  if (!request.cookies.get(SESSION_COOKIE)?.value) {
    const entry = request.nextUrl.clone();
    entry.pathname = anonymousEntryPath(request.nextUrl.pathname);
    entry.search = "";
    return NextResponse.redirect(entry);
  }

  return NextResponse.next();
}

export const config = {
  // Literal copy of buildProxyMatcher() in public-paths.ts. public-paths.test.ts checks they match.
  matcher: [
    "/((?!_next/static|_next/image|api/|about|docs|features|home|policy|login|register|favicon.ico|.*\\..*).*)",
  ],
};
