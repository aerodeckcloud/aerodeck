import type { Metadata } from "next";

import { PublicShell } from "@/components/public-shell";
import { PRODUCT_NAME } from "@/lib/brand";

export const metadata: Metadata = {
  title: {
    default: PRODUCT_NAME,
    template: `%s · ${PRODUCT_NAME}`,
  },
};

export default function PublicLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return <PublicShell>{children}</PublicShell>;
}
