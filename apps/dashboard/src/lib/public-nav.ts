import { LOGIN_PATH, PUBLIC_HOME_PATH } from "@/lib/public-paths";

/** Register and Login must send legal copy to this route. */
export const POLICY_HREF = "/policy" as const;

export const PUBLIC_HOME = PUBLIC_HOME_PATH;

export const PUBLIC_NAV = [
  { href: PUBLIC_HOME, label: "Home" },
  { href: "/docs", label: "Docs" },
  { href: "/about", label: "About" },
  { href: "/features", label: "Features" },
  { href: POLICY_HREF, label: "Policy" },
] as const;

export const PUBLIC_AUTH_NAV = [
  { href: "/register", label: "Register" },
  { href: LOGIN_PATH, label: "Login" },
] as const;
