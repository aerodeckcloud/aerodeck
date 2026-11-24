import { Rocket } from "lucide-react";

import { TAKEOFF_MS } from "@/lib/deploy-takeoff";

/** Decorative rocket climb on the launch route. The Deploy button remains the status. */
export function LaunchTakeoff({ active }: { active: boolean }) {
  if (!active) {
    return null;
  }
  return (
    <div
      className="relative h-24 overflow-hidden rounded-lg border border-border/80 bg-background motion-reduce:hidden"
      aria-hidden="true"
    >
      <div className="absolute inset-x-6 bottom-5 h-px bg-runway" />
      <Rocket
        className="aerodeck-takeoff-rocket absolute bottom-5 left-6 size-6 text-primary"
        style={{ animationDuration: `${TAKEOFF_MS}ms` }}
      />
    </div>
  );
}
