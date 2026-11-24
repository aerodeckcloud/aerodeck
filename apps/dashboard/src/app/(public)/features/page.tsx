import type { Metadata } from "next";

import { LinkButton } from "@/components/link-button";
import { PRODUCT_NAME } from "@/lib/brand";

export const metadata: Metadata = {
  title: "Features",
  description: `What ${PRODUCT_NAME} does today: projects, GitHub deployments, logs, promotion, rollback, pause, production approval, hostnames, and Firecracker sandboxes.`,
};

const FEATURES = [
  {
    title: "Projects and environments",
    body: "A project is the unit of work. Environments under it name a kind and a DigitalOcean region, and each flight is tied to one of them.",
  },
  {
    title: "Deploy from a GitHub repository",
    body: "A deployment leaves from a GitHub repository. The deck records the repository, the commit, and the environment the flight is bound for.",
  },
  {
    title: "History, status, and logs",
    body: "Every flight keeps its place in the history, its status, and a log you can read while it runs.",
  },
  {
    title: "Repeat a commit, or take the default branch",
    body: "Redeploy repeats the commit already recorded on that flight. Deploy latest leaves from the default branch and does not pin a commit.",
  },
  {
    title: "Promote a succeeded image",
    body: "Promote runs a succeeded image on another environment of the same project. The image is the one already built. The repository is not cloned again.",
  },
  {
    title: "Roll back saved variables",
    body: "Rollback returns the environment to a saved image and restores the variables recorded with that flight.",
  },
  {
    title: "Pause, and hold production",
    body: "An environment can pause automatic redeploys. Production can also wait for an owner before the flight is allowed to leave.",
  },
  {
    title: "Hostnames, DNS, and certificates",
    body: "A hostname is published from the project with its DNS record and its certificate.",
  },
  {
    title: "Firecracker sandboxes",
    body: "A sandbox is a Firecracker microVM for work that should not last. It expires with the job. It is not a production tenant.",
  },
] as const;

export default function FeaturesPage() {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-12 md:px-8 md:py-20">
      <p className="font-mono text-[11px] tracking-[0.22em] text-primary uppercase">
        Current deck
      </p>
      <h1 className="mt-3 max-w-2xl font-heading text-4xl font-semibold tracking-tight md:text-5xl">
        What the deck does today
      </h1>
      <p className="mt-4 max-w-2xl text-sm leading-relaxed text-muted-foreground md:text-base">
        {PRODUCT_NAME} is the console for GitOps on DigitalOcean. These are the flights an
        operator can run now: the project, the deployment, the hold, the hostname, and
        the sandbox.
      </p>

      <ol className="mt-12 grid gap-px overflow-hidden rounded-xl border border-border bg-border sm:grid-cols-2 lg:grid-cols-3">
        {FEATURES.map((feature, index) => (
          <li key={feature.title} className="bg-card p-5">
            <p className="font-mono text-[11px] tracking-[0.18em] text-primary uppercase">
              {String(index + 1).padStart(2, "0")}
            </p>
            <h2 className="mt-2 font-heading text-lg font-semibold tracking-tight">
              {feature.title}
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{feature.body}</p>
          </li>
        ))}
      </ol>

      <div className="mt-8 flex flex-wrap gap-3">
        <LinkButton href="/login">Open the deck</LinkButton>
        <LinkButton href="/about" variant="outline">
          Read about {PRODUCT_NAME}
        </LinkButton>
      </div>
    </div>
  );
}
