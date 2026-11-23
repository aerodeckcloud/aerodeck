import type { AddonType } from "@/lib/types";

/** Addon types the control plane already accepts. */
export const DEPLOY_ADDON_TYPES = ["postgres", "redis", "mongodb"] as const satisfies readonly AddonType[];

const KNOWN = new Set<string>(DEPLOY_ADDON_TYPES);

/** Known addon types from the scan, in scan order, with duplicates dropped. */
export function knownDetectedAddons(types: readonly string[]): AddonType[] {
  const seen = new Set<string>();
  const detected: AddonType[] = [];
  for (const type of types) {
    if (!KNOWN.has(type) || seen.has(type)) {
      continue;
    }
    seen.add(type);
    detected.push(type as AddonType);
  }
  return detected;
}

/**
 * Databases to show. A scan that found some offers only those.
 * A scan that found none offers the three accepted types so the operator can add one.
 */
export function offeredAddonTypes(detected: readonly AddonType[]): readonly AddonType[] {
  return detected.length > 0 ? detected : DEPLOY_ADDON_TYPES;
}
