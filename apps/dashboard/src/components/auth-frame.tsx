import { BrandMark } from "@/components/brand-mark";

export function AuthFrame({
  eyebrow,
  title,
  description,
  children,
}: {
  eyebrow: string;
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mx-auto flex w-full max-w-md flex-col gap-6 px-4 py-12 md:py-20">
      <div className="flex items-center gap-3">
        <BrandMark className="size-11 shrink-0 text-primary" />
        <div>
          <p className="font-mono text-[11px] tracking-[0.2em] text-primary uppercase">{eyebrow}</p>
          <h1 className="font-heading text-3xl font-semibold tracking-tight">{title}</h1>
        </div>
      </div>
      <p className="text-sm text-muted-foreground">{description}</p>
      {children}
    </div>
  );
}
