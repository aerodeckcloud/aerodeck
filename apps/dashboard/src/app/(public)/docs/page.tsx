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
  BLUEPRINT_SCHEMA_PATH,
  agentBlueprintPrompt,
  extractSchemaTables,
  extractYamlExample,
  readBlueprintSchemaMarkdown,
} from "@/lib/aerodeck-blueprint-schema";
import { PRODUCT_NAME } from "@/lib/brand";
import { publicOrigin } from "@/lib/public-origin";

export const metadata: Metadata = {
  title: "Docs",
  description: `${PRODUCT_NAME} docs for aerodeck.yaml, including the blueprint schema agents use to generate a deployment file.`,
};

function SchemaTable({
  heading,
  headers,
  rows,
}: {
  heading: string;
  headers: string[];
  rows: string[][];
}) {
  return (
    <div className="space-y-3">
      <h3 className="font-heading text-lg font-semibold tracking-tight">{heading}</h3>
      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full min-w-[40rem] text-left text-sm">
          <thead className="bg-muted/40 font-mono text-[11px] tracking-[0.14em] text-muted-foreground uppercase">
            <tr>
              {headers.map((header) => (
                <th key={header} className="px-4 py-3 font-medium">
                  {header.replace(/`/g, "")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.join("|")} className="border-t border-border align-top">
                {row.map((cell, index) => (
                  <td
                    key={`${row[0]}-${index}`}
                    className={
                      index === row.length - 1
                        ? "px-4 py-3 text-muted-foreground"
                        : "px-4 py-3 font-mono text-xs text-muted-foreground first:text-primary"
                    }
                  >
                    {cell.replace(/`/g, "")}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default async function DocsPage() {
  const markdown = readBlueprintSchemaMarkdown();
  const example = extractYamlExample(markdown);
  const tables = extractSchemaTables(markdown);

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
          <DocsCopyButton text={example} label="Copy YAML" />
        </div>
        <pre className="overflow-x-auto rounded-xl border border-border bg-card p-4 font-mono text-xs leading-relaxed text-foreground">
          {example}
        </pre>
      </section>

      <section className="mt-12 space-y-8">
        <div>
          <p className="font-mono text-[11px] tracking-[0.18em] text-primary uppercase">Schema</p>
          <h2 className="mt-2 font-heading text-2xl font-semibold tracking-tight">
            Version 1 reference
          </h2>
          <p className="mt-2 max-w-2xl text-sm text-muted-foreground">
            Tables are parsed from{" "}
            <code className="font-mono text-xs">docs/aerodeck-blueprint-schema.md</code>, the
            same file agents fetch at{" "}
            <code className="font-mono text-xs">{BLUEPRINT_SCHEMA_PATH}</code>.
          </p>
        </div>
        {tables.map((table) => (
          <SchemaTable key={table.heading} {...table} />
        ))}
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
