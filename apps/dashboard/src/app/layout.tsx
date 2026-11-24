import type { Metadata } from "next";
import { IBM_Plex_Mono, Space_Grotesk } from "next/font/google";

import { PRODUCT_NAME } from "@/lib/brand";

import "./globals.css";

const deckSans = Space_Grotesk({
  variable: "--font-deck-sans",
  subsets: ["latin"],
});

const deckMono = IBM_Plex_Mono({
  variable: "--font-deck-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: `${PRODUCT_NAME} · Console`,
  description: `Operator dashboard for ${PRODUCT_NAME} GitOps deployments.`,
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`dark ${deckSans.variable} ${deckMono.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <head>
        {/* Blocking: Cursor injects data-cursor-ref before React hydrates. */}
        <script src="/strip-cursor-ref.js" />
      </head>
      <body className="min-h-full" suppressHydrationWarning>
        {children}
      </body>
    </html>
  );
}
