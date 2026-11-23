/** Path segments the session proxy must not gate. `home` is the public homepage. */
export const PUBLIC_PATH_SEGMENTS = [
  "about",
  "home",
  "policy",
  "login",
  "register",
] as const;

export function buildProxyMatcher(
  segments: readonly string[] = PUBLIC_PATH_SEGMENTS,
): string {
  return `/((?!_next/static|_next/image|api/|${segments.join("|")}|favicon.ico|.*\\..*).*)`;
}

export function pathnameMatchesProxy(
  pathname: string,
  matcher = buildProxyMatcher(),
): boolean {
  return new RegExp(`^${matcher}$`).test(pathname);
}
