export function BrandMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden>
      <rect width="32" height="32" rx="9" fill="oklch(0.16 0.05 262)" />
      <rect
        x="0.75"
        y="0.75"
        width="30.5"
        height="30.5"
        rx="8.25"
        fill="none"
        stroke="currentColor"
        strokeOpacity="0.55"
      />
      <path
        d="M7.5 20.25 16 9.75l8.5 10.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
      <path
        d="M10.6 20.25 16 13.4l5.4 6.85"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.35"
        strokeLinejoin="round"
        opacity="0.6"
      />
      <path
        d="M8 23.6h16"
        stroke="oklch(0.84 0.15 78)"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
      <circle cx="16" cy="23.6" r="1.15" fill="oklch(0.84 0.15 78)" />
    </svg>
  );
}
