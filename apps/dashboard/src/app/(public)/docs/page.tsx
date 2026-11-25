import type { Metadata } from "next";
import { headers } from "next/headers";

import { DocsCopyButton } from "@/components/docs-copy-button";
import { LinkButton } from "@/components/link-button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  BLUEPRINT_EXAMPLE_YAML,
  BLUEPRINT_SCHEMA_PATH,
  agentBlueprintPrompt,
} from "@/lib/aerodeck-blueprint-schema";
import { PRODUCT_NAME } from "@/lib/brand";
import { publicOrigin } from "@/lib/public-origin";

export const metadata: Metadata = {
  title: "Docs",
  description: `${PRODUCT_NAME} docs for aerodeck.yaml, including the blueprint schema agents use to generate a deployment file.`,
};

const ROOT_FIELDS = [
  ["version", "1", "Yes", "Schema version (1)."],
  ["name", "string", "No", "Human-readable application name. Defaults to the GitHub repository name."],
  ["build", "BlueprintBuild", "No", "Build strategy, Dockerfile/Compose path, and listening TCP port."],
  ["addons", "BlueprintAddon[]", "No", "Database addons (postgres, redis, mongodb) to provision and attach."],
  ["env", "BlueprintEnvVar[]", "No", "Runtime environment variables and secrets prompted on /deploy."],
] as const;

const BUILD_FIELDS = [
  ["strategy", '"dockerfile" | "compose" | "auto"', '"dockerfile"', "Root Dockerfile, Compose service, or auto Dockerfile from a project file."],
  ["dockerfile", "string", '"Dockerfile"', "Relative path to the Dockerfile."],
  ["compose_file", "string | null", "null", "Relative path to docker-compose.yaml when strategy is compose."],
  ["service", "string | null", "null", "Primary web service name in docker-compose.yaml."],
  ["port", "integer (1–65535)", "8000", "Internal container TCP port Traefik sends HTTP to."],
  ["runtime", '"node" | "python" | "go" | "java" | "rust" | "static" | null', "null", "Runtime from a root project file when strategy is auto."],
  ["marker", "string | null", "null", "Root file that chose runtime, such as package.json."],
] as const;

const ADDON_FIELDS = [
  ["type", '"postgres" | "redis" | "mongodb"', "Required", "Database engine on the tenant Docker network."],
  ["env_var", "string", "DATABASE_URL / REDIS_URL / MONGODB_URI", "Environment variable injected into the app container."],
  ["version", "string", "16 / 7 / 7", "Image major version tag."],
] as const;

const ENV_FIELDS = [
  ["key", "string", "Required", "Environment variable name."],
  ["description", "string", '""', "Help text under the /deploy input."],
  ["required", "boolean", "true", "When true, /deploy needs a value before launch."],
  ["secret", "boolean", "false", "Masks the input as a password field."],
  ["default", "string | null", "null", "Pre-filled default on /deploy."],
  ["generate", '"hex32" | null', "null", "Adds a Generate button for signing keys and tokens."],
] as const;

function SchemaTable({
  rows,
}: {
  rows: readonly (readonly [string, string, string, string])[];
}) {
  return (
    <div className="overflow-x-auto rounded-xl border border-border">
      <table className="w-full min-w-[40rem] text-left text-sm">
        <thead className="bg-muted/40 font-mono text-[11px] tracking-[0.14em] text-muted-foreground uppercase">
          <tr>
            <th className="px-4 py-3 font-medium">Field</th>
            <th className="px-4 py-3 font-medium">Type</th>
            <th className="px-4 py-3 font-medium">Default / required</th>
            <th className="px-4 py-3 font-medium">Description</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([field, type, def, description]) => (
            <tr key={field} className="border-t border-border align-top">
              <td className="px-4 py-3 font-mono text-xs text-primary">{field}</td>
              <td className="px-4 py-3 font-mono text-xs text-muted-foreground">{type}</td>
              <td className="px-4 py-3 font-mono text-xs text-muted-foreground">{def}</td>
              <td className="px-4 py-3 text-muted-foreground">{description}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default async function DocsPage() {
  const headerList = await headers();
  const host = headerList.get("x-forwarded-host") ?? headerList.get("host") ?? "localhost:3000";
  const proto = headerList.get("x-forwarded-proto") ?? "http";
  const origin = publicOrigin(
    new Request(`http://127.0.0.1:3000${BLUEPRINT_SCHEMA_PATH}`, {
      headers: {
        host,
        "x-forwarded-proto": proto,
        "x-forwarded-host": host,
      },
    }),
  );
  const schemaUrl = `${origin}${BLUEPRINT_SCHEMA_PATH}`;
  const prompt = agentBlueprintPrompt(schemaUrl);

  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-12 md:px-8 md:py-20">
      <p className="font-mono text-[11px] tracking-[0.22em] text-primary uppercase">Docs</p>
      <h1 className="mt-3 max-w-3xl font-heading text-4xl font-semibold tracking-tight md:text-5xl">
        Application blueprint for {PRODUCT_NAME}
      </h1>
      <p className="mt-4 max-w-2xl text-sm leading-relaxed text-muted-foreground md:text-base">
        Put <code className="font-mono text-xs text-foreground">aerodeck.yaml</code> at the
        repository root. {PRODUCT_NAME} reads it on deploy. Point an agent at the schema URL
        below to generate that file from a sample repo.
      </p>

      <div className="mt-8 flex flex-wrap gap-3">
        <LinkButton href={BLUEPRINT_SCHEMA_PATH}>Open schema markdown</LinkButton>
        <LinkButton href="/deploy" variant="outline">
          Open deploy
        </LinkButton>
      </div>

      <Card className="mt-10">
        <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
          <div>
            <CardTitle>Agent prompt</CardTitle>
            <CardDescription>
              Paste this into an agent with access to a sample repository. Schema URL:{" "}
              <code className="font-mono text-xs">{schemaUrl}</code>
            </CardDescription>
          </div>
          <DocsCopyButton text={prompt} label="Copy prompt" />
        </CardHeader>
        <CardContent>
          <pre className="overflow-x-auto rounded-lg border border-border bg-muted/30 p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap text-foreground">
            {prompt}
          </pre>
        </CardContent>
      </Card>

      <section className="mt-12 space-y-4">
        <div className="flex items-end justify-between gap-4">
          <div>
            <p className="font-mono text-[11px] tracking-[0.18em] text-primary uppercase">Example</p>
            <h2 className="mt-2 font-heading text-2xl font-semibold tracking-tight">
              Full aerodeck.yaml
            </h2>
          </div>
          <DocsCopyButton text={BLUEPRINT_EXAMPLE_YAML} label="Copy YAML" />
        </div>
        <pre className="overflow-x-auto rounded-xl border border-border bg-card p-4 font-mono text-xs leading-relaxed text-foreground">
          {BLUEPRINT_EXAMPLE_YAML}
        </pre>
      </section>

      <section className="mt-12 space-y-8">
        <div>
          <p className="font-mono text-[11px] tracking-[0.18em] text-primary uppercase">Schema</p>
          <h2 className="mt-2 font-heading text-2xl font-semibold tracking-tight">
            Version 1 reference
          </h2>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
            Inspect order: root <code className="font-mono text-xs">aerodeck.yaml</code> /{" "}
            <code className="font-mono text-xs">.yml</code> /{" "}
            <code className="font-mono text-xs">.json</code>, then compose, Dockerfile, and env
            samples when no blueprint is present.
          </p>
        </div>

        <div className="space-y-3">
          <h3 className="font-heading text-lg font-semibold tracking-tight">Root fields</h3>
          <SchemaTable rows={ROOT_FIELDS} />
        </div>
        <div className="space-y-3">
          <h3 className="font-heading text-lg font-semibold tracking-tight">build</h3>
          <SchemaTable rows={BUILD_FIELDS} />
        </div>
        <div className="space-y-3">
          <h3 className="font-heading text-lg font-semibold tracking-tight">addons[]</h3>
          <SchemaTable rows={ADDON_FIELDS} />
        </div>
        <div className="space-y-3">
          <h3 className="font-heading text-lg font-semibold tracking-tight">env[]</h3>
          <SchemaTable rows={ENV_FIELDS} />
        </div>
      </section>

      <section className="mt-12 space-y-3">
        <p className="font-mono text-[11px] tracking-[0.18em] text-primary uppercase">Platform</p>
        <h2 className="font-heading text-2xl font-semibold tracking-tight">
          Variables injected on every deploy
        </h2>
        <ul className="list-disc space-y-2 pl-5 text-sm text-muted-foreground">
          <li>
            <code className="font-mono text-xs text-foreground">PORT</code> from{" "}
            <code className="font-mono text-xs">build.port</code>
          </li>
          <li>
            <code className="font-mono text-xs text-foreground">HOST</code> ={" "}
            <code className="font-mono text-xs">0.0.0.0</code>
          </li>
          <li>
            <code className="font-mono text-xs text-foreground">AERODECK_PUBLIC_URL</code>,{" "}
            <code className="font-mono text-xs text-foreground">PUBLIC_URL</code>,{" "}
            <code className="font-mono text-xs text-foreground">PORTAL_PUBLIC_URL</code> ={" "}
            <code className="font-mono text-xs">
              https://&lt;project&gt;-&lt;env&gt;.dev.aerodeckcloud.com
            </code>
          </li>
          <li>
            Addon URLs for postgres, redis, and mongodb when those addons are enabled
          </li>
        </ul>
      </section>
    </div>
  );
}
