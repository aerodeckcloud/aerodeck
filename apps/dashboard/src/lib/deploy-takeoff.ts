/** How long the launch-route takeoff runs. The CSS animation uses the same value (#344). */
export const TAKEOFF_MS = 1400;

/** Reduced motion keeps the Deploy button's progress and skips the timeline. */
export function shouldPlayTakeoff(reducedMotion: boolean): boolean {
  return !reducedMotion;
}

/** Remaining time to hold a successful launch so navigation waits for the takeoff. */
export function takeoffHoldMs(elapsedMs: number, playTakeoff: boolean): number {
  if (!playTakeoff) {
    return 0;
  }
  const elapsed = Math.max(0, elapsedMs);
  return elapsed >= TAKEOFF_MS ? 0 : TAKEOFF_MS - elapsed;
}
