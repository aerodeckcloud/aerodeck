"use client";

import { Check, Copy } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export function DocsCopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current !== null) {
        window.clearTimeout(timer.current);
      }
    };
  }, []);

  return (
    <button
      type="button"
      className={cn(buttonVariants({ variant: "outline", size: "sm" }), "gap-1.5")}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setStatus("copied");
        } catch {
          setStatus("failed");
        }
        if (timer.current !== null) {
          window.clearTimeout(timer.current);
        }
        timer.current = window.setTimeout(() => setStatus("idle"), 1500);
      }}
    >
      {status === "copied" ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      {status === "copied" ? "Copied" : status === "failed" ? "Copy failed" : label}
    </button>
  );
}
