import { BLUEPRINT_SCHEMA_MARKDOWN } from "@/lib/aerodeck-blueprint-schema";

export function GET() {
  return new Response(BLUEPRINT_SCHEMA_MARKDOWN, {
    headers: {
      "Content-Type": "text/markdown; charset=utf-8",
      "Cache-Control": "public, max-age=300",
    },
  });
}
