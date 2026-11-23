import { NextResponse, type NextRequest } from "next/server";

import { SESSION_COOKIE } from "@/lib/session-cookie";

export function proxy(request: NextRequest) {
  if (!request.cookies.get(SESSION_COOKIE)?.value) {
    const login = request.nextUrl.clone();
    login.pathname = "/login";
    login.search = "";
    return NextResponse.redirect(login);
  }

  return NextResponse.next();
}

export const config = {
  // Literal copy of buildProxyMatcher() in public-paths.ts. public-paths.test.ts checks they match.
  matcher: [
    "/((?!_next/static|_next/image|api/|about|home|policy|login|register|favicon.ico|.*\\..*).*)",
  ],
};
