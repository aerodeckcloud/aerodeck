"use client";

import { useEffect, useRef, useState, type JSX, type KeyboardEvent, type RefObject } from "react";

import { cn } from "@/lib/utils";

const FRAMES = [
  {
    id: "approach",
    label: "Approach",
    kicker: "GitHub repository",
    caption:
      "The flight starts at a GitHub repository. That repository is the source the deck launches from.",
  },
  {
    id: "runway",
    label: "Runway",
    kicker: "Deployment",
    caption:
      "A deployment leaves from that repository. Status, commit, and environment stay on the strip.",
  },
  {
    id: "tower",
    label: "Tower",
    kicker: "Hostname",
    caption:
      "A hostname is published from the project, with its DNS record and its certificate.",
  },
  {
    id: "pad",
    label: "Pad",
    kicker: "Sandbox",
    caption:
      "A sandbox is a Firecracker microVM for work that should not last. It is not a production tenant.",
  },
] as const;

type FrameId = (typeof FRAMES)[number]["id"];
type Frame = (typeof FRAMES)[number];

export function DeckSlider() {
  const [index, setIndex] = useState(0);
  const tabs = useRef<Array<HTMLButtonElement | null>>([]);

  function select(next: number) {
    const wrapped = (next + FRAMES.length) % FRAMES.length;
    setIndex(wrapped);
    tabs.current[wrapped]?.focus();
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[15rem_1fr] lg:items-stretch">
      <DeckTabList index={index} tabs={tabs} onSelect={select} />
      <DeckPanel frame={FRAMES[index]} />
    </div>
  );
}

function DeckTabList({
  index,
  tabs,
  onSelect,
}: {
  index: number;
  tabs: RefObject<Array<HTMLButtonElement | null>>;
  onSelect: (next: number) => void;
}) {
  const orientation = useTabOrientation();

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const forward = orientation === "vertical" ? "ArrowDown" : "ArrowRight";
    const back = orientation === "vertical" ? "ArrowUp" : "ArrowLeft";
    if (event.key === forward) {
      event.preventDefault();
      onSelect(index + 1);
    } else if (event.key === back) {
      event.preventDefault();
      onSelect(index - 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      onSelect(0);
    } else if (event.key === "End") {
      event.preventDefault();
      onSelect(FRAMES.length - 1);
    }
  }

  return (
    <div
      role="tablist"
      aria-label="Deck frames"
      aria-orientation={orientation}
      className="flex gap-2 overflow-x-auto lg:flex-col"
      onKeyDown={onKeyDown}
    >
      {FRAMES.map((item, itemIndex) => {
        const selected = itemIndex === index;
        return (
          <button
            key={item.id}
            ref={(node) => {
              tabs.current[itemIndex] = node;
            }}
            id={`deck-tab-${item.id}`}
            type="button"
            role="tab"
            aria-selected={selected}
            aria-controls="deck-panel"
            tabIndex={selected ? 0 : -1}
            className={cn(
              "min-w-40 shrink-0 rounded-xl border px-3 py-3 text-left transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none lg:min-w-0",
              selected
                ? "border-primary/50 bg-primary/10 text-foreground"
                : "border-border bg-card/40 text-muted-foreground hover:text-foreground",
            )}
            onClick={() => onSelect(itemIndex)}
          >
            <span className="block font-mono text-[10px] tracking-[0.18em] uppercase">
              {String(itemIndex + 1).padStart(2, "0")}
            </span>
            <span className="mt-1 block font-heading text-sm font-semibold">{item.label}</span>
            <span className="mt-0.5 block text-xs">{item.kicker}</span>
          </button>
        );
      })}
    </div>
  );
}

function DeckPanel({ frame }: { frame: Frame }) {
  return (
    <div
      id="deck-panel"
      role="tabpanel"
      aria-labelledby={`deck-tab-${frame.id}`}
      className="relative overflow-hidden rounded-2xl border border-border bg-card"
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <p className="font-mono text-[10px] tracking-[0.2em] text-muted-foreground uppercase">
          Deck
        </p>
        <p className="font-mono text-[10px] tracking-[0.16em] text-primary uppercase">
          {frame.label}
        </p>
      </div>
      <div className="grid gap-6 p-4 md:grid-cols-[1.15fr_0.85fr] md:items-center md:p-6">
        <FramePicture id={frame.id} />
        <p className="text-sm leading-relaxed text-muted-foreground">{frame.caption}</p>
      </div>
      <span className="deck-horizon" aria-hidden />
    </div>
  );
}

/** Matches the `lg` column. The first paint stays horizontal so hydration does not disagree. */
function useTabOrientation(): "horizontal" | "vertical" {
  const [orientation, setOrientation] = useState<"horizontal" | "vertical">("horizontal");

  useEffect(() => {
    const media = window.matchMedia("(min-width: 64rem)");
    const apply = () => setOrientation(media.matches ? "vertical" : "horizontal");
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, []);

  return orientation;
}

const FRAME_PICTURES: Record<FrameId, () => JSX.Element> = {
  approach: ApproachPicture,
  runway: RunwayPicture,
  tower: TowerPicture,
  pad: PadPicture,
};

function FramePicture({ id }: { id: FrameId }) {
  return FRAME_PICTURES[id]();
}

function ApproachPicture() {
  return (
    <div className="overflow-hidden rounded-xl border border-border bg-background">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <span className="size-1.5 rounded-full bg-primary" />
        <p className="font-mono text-[11px] text-foreground">github.com/hangar/payments</p>
      </div>
      <div className="grid grid-cols-[1fr_auto] gap-3 p-3">
        <ul className="space-y-2 font-mono text-[11px] text-muted-foreground">
          <li className="flex items-center justify-between gap-3 rounded-md bg-card px-2 py-1.5 text-foreground">
            aerodeck.yaml
            <span className="text-primary">source</span>
          </li>
          <li className="px-2">Dockerfile</li>
          <li className="px-2">.env.example</li>
        </ul>
        <ApproachMark />
      </div>
    </div>
  );
}

function ApproachMark() {
  return (
    <svg viewBox="0 0 88 88" className="size-20 text-primary" aria-hidden>
      <path
        d="M18 62 44 28l26 34"
        fill="none"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinejoin="round"
      />
      <path
        d="M28 62 44 40l16 22"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="round"
        opacity="0.55"
      />
      <path d="M16 72h56" stroke="var(--runway)" strokeWidth="3" strokeLinecap="round" />
      <circle cx="44" cy="72" r="2.4" fill="var(--runway)" />
    </svg>
  );
}

function RunwayPicture() {
  return (
    <div className="space-y-3">
      <StripRow lamp="bg-primary" title="succeeded" meta="a1b2c3d · production" live />
      <StripRow lamp="bg-runway" title="queued" meta="e4f5a6b · staging" />
      <div className="relative h-2 overflow-hidden rounded-full bg-muted">
        <span className="absolute inset-y-0 left-0 w-2/3 rounded-full bg-primary" />
        <span className="absolute top-1/2 left-2/3 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-runway" />
      </div>
    </div>
  );
}

function StripRow({
  lamp,
  title,
  meta,
  live = false,
}: {
  lamp: string;
  title: string;
  meta: string;
  live?: boolean;
}) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border bg-background px-3 py-2">
      <span
        className={cn("size-1.5 shrink-0 rounded-full", lamp)}
        style={live ? { boxShadow: "0 0 8px var(--primary)" } : undefined}
      />
      <div className="min-w-0">
        <p className="font-mono text-[11px] text-foreground">{title}</p>
        <p className="truncate font-mono text-[10px] text-muted-foreground">{meta}</p>
      </div>
    </div>
  );
}

function TowerPicture() {
  return (
    <div className="flex items-center gap-4 rounded-xl border border-border bg-background p-4">
      <svg viewBox="0 0 48 72" className="h-16 w-10 shrink-0 text-primary" aria-hidden>
        <path d="M24 8v40" stroke="currentColor" strokeWidth="2" />
        <path d="M14 18h20" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        <circle cx="24" cy="14" r="3" fill="var(--runway)" />
        <path d="M8 64h32" stroke="var(--runway)" strokeWidth="2" strokeLinecap="round" />
      </svg>
      <div className="min-w-0">
        <p className="truncate font-mono text-sm text-foreground">payments.aero-deck.app</p>
        <p className="mt-2 flex items-center gap-2 font-mono text-[10px] tracking-[0.16em] text-primary uppercase">
          <span className="size-1.5 rounded-full bg-primary" style={{ boxShadow: "0 0 8px var(--primary)" }} />
          Live
        </p>
        <p className="mt-1 font-mono text-[11px] text-muted-foreground">Certificate issued</p>
      </div>
    </div>
  );
}

function PadPicture() {
  return (
    <div className="flex items-center gap-4 rounded-xl border border-border bg-background p-4">
      <div className="relative grid size-16 shrink-0 place-items-center rounded-full border border-runway/60">
        <span className="size-8 rounded-full border border-primary/50" />
        <span className="absolute size-2 rounded-full bg-primary" style={{ boxShadow: "0 0 8px var(--primary)" }} />
      </div>
      <div>
        <p className="font-heading text-sm font-semibold">lab-7</p>
        <p className="mt-1 font-mono text-[11px] text-primary">running</p>
        <p className="mt-1 text-xs text-muted-foreground">Firecracker · expires with the job</p>
      </div>
    </div>
  );
}
