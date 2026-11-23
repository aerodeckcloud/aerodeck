import type { Metadata } from "next";

import { DeckSlider } from "@/components/deck-slider";
import { LinkButton } from "@/components/link-button";

export const metadata: Metadata = {
  title: "Home",
  description:
    "Step through an AeroDeck flight: a GitHub repository, a deployment, a hostname, and a short-lived sandbox.",
};

export default function HomePage() {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-12 md:px-8 md:py-20">
      <p className="font-mono text-[11px] tracking-[0.22em] text-primary uppercase">Flight deck</p>
      <h1 className="mt-3 max-w-2xl font-heading text-4xl font-semibold tracking-tight md:text-5xl">
        See the flight before you open the deck
      </h1>
      <p className="mt-4 max-w-2xl text-sm leading-relaxed text-muted-foreground md:text-base">
        AeroDeck is the console for GitOps on DigitalOcean. Choose a frame: the repository
        the flight leaves from, the deployment on the strip, the hostname in the air, and
        the sandbox that expires when the work is done.
      </p>

      <div className="mt-10">
        <DeckSlider />
      </div>

      <div className="mt-8 flex flex-wrap gap-3">
        <LinkButton href="/login">Open the deck</LinkButton>
        <LinkButton href="/about" variant="outline">
          Read about AeroDeck
        </LinkButton>
      </div>
    </div>
  );
}
