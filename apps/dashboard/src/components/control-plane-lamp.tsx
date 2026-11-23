import { cn } from "@/lib/utils";

export function ControlPlaneLamp({
  controlPlane,
  glow = "8px",
  className,
}: {
  controlPlane: boolean;
  glow?: string;
  className?: string;
}) {
  return (
    <span
      className={cn("size-1.5 rounded-full", controlPlane ? "bg-primary" : "bg-runway", className)}
      style={controlPlane ? { boxShadow: `0 0 ${glow} var(--primary)` } : undefined}
      aria-hidden
    />
  );
}
