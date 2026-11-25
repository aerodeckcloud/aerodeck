/** Path segments the session proxy must not gate. `home` and `features` are public pages. */
export const PUBLIC_PATH_SEGMENTS = [
  "about",
  "features",
  "home",
  "policy",
  "login",
  "register",
] as const;

export const PUBLIC_HOME_PATH = "/home" as const;
export const LOGIN_PATH = "/login" as const;

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

/** Where an anonymous visitor goes when the session proxy blocks a path. */
export function anonymousEntryPath(pathname: string): typeof PUBLIC_HOME_PATH | typeof LOGIN_PATH {
  return pathname === "/" ? PUBLIC_HOME_PATH : LOGIN_PATH;
}
