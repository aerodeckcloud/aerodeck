/** Load the canonical Application Blueprint Schema from docs/ (or the image copy). */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const BLUEPRINT_SCHEMA_PATH = "/docs/aerodeck-blueprint-schema.md" as const;
export const BLUEPRINT_SCHEMA_FILENAME = "aerodeck-blueprint-schema.md" as const;

export type SchemaTable = {
  heading: string;
  headers: string[];
  rows: string[][];
};

function candidateSchemaPaths(): string[] {
  const cwd = process.cwd();
  const here = dirname(fileURLToPath(import.meta.url));
  return [
    join(cwd, "content", BLUEPRINT_SCHEMA_FILENAME),
    join(cwd, "docs", BLUEPRINT_SCHEMA_FILENAME),
    join(cwd, "..", "..", "docs", BLUEPRINT_SCHEMA_FILENAME),
    join(here, "..", "..", "..", "..", "docs", BLUEPRINT_SCHEMA_FILENAME),
  ];
}

/** Absolute path to the canonical markdown on disk. */
export function resolveBlueprintSchemaPath(): string {
  for (const path of candidateSchemaPaths()) {
    if (existsSync(path)) {
      return path;
    }
  }
  throw new Error(
    `Missing ${BLUEPRINT_SCHEMA_FILENAME}; expected under docs/ or content/`,
  );
}

export function readBlueprintSchemaMarkdown(): string {
  return readFileSync(resolveBlueprintSchemaPath(), "utf8");
}

export function extractYamlExample(markdown: string): string {
  const match = markdown.match(/```yaml\r?\n([\s\S]*?)\r?\n```/);
  if (!match) {
    throw new Error("blueprint schema markdown is missing a yaml example fence");
  }
  return `${match[1].trimEnd()}\n`;
}

function splitTableRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((cell) => cell.trim());
}

function isDividerRow(cells: string[]): boolean {
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

/** Parse every markdown table that follows a `###` heading in the schema doc. */
export function extractSchemaTables(markdown: string): SchemaTable[] {
  const lines = markdown.split(/\r?\n/);
  const tables: SchemaTable[] = [];
  let heading = "";
  let index = 0;
  while (index < lines.length) {
    const line = lines[index] ?? "";
    const headingMatch = line.match(/^###\s+(.+)\s*$/);
    if (headingMatch) {
      heading = headingMatch[1].trim();
      index += 1;
      continue;
    }
    if (line.startsWith("|") && heading) {
      const header = splitTableRow(line);
      const divider = lines[index + 1] ?? "";
      if (!divider.startsWith("|") || !isDividerRow(splitTableRow(divider))) {
        index += 1;
        continue;
      }
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && (lines[index] ?? "").startsWith("|")) {
        rows.push(splitTableRow(lines[index] ?? ""));
        index += 1;
      }
      tables.push({ heading, headers: header, rows });
      heading = "";
      continue;
    }
    index += 1;
  }
  return tables;
}

export function agentBlueprintPrompt(schemaUrl: string): string {
  return [
    "You are writing an Aerodeck Cloud application blueprint.",
    "",
    `Read the schema at: ${schemaUrl}`,
    "",
    "Inspect the application repository and write aerodeck.yaml at the repository root.",
    "Follow schema version 1 and the agent instructions in that document.",
    "Return only the YAML file contents unless the operator asks for an explanation.",
  ].join("\n");
}
