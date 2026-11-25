import { readBlueprintSchemaMarkdown } from "@/lib/aerodeck-blueprint-schema";

export function GET() {
  return new Response(readBlueprintSchemaMarkdown(), {
    headers: {
      "Content-Type": "text/markdown; charset=utf-8",
      "Cache-Control": "public, max-age=300",
    },
  });
}
