/** Register and Login must send legal copy to this route. */
export const POLICY_HREF = "/policy" as const;

export const PUBLIC_HOME = "/home" as const;

export const PUBLIC_NAV = [
  { href: PUBLIC_HOME, label: "Home" },
  { href: "/about", label: "About" },
  { href: "/features", label: "Features" },
  { href: POLICY_HREF, label: "Policy" },
] as const;

export const PUBLIC_AUTH_NAV = [
  { href: "/register", label: "Register" },
  { href: "/login", label: "Login" },
] as const;
