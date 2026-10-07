import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "EWKit Agentic Ops",
  description: "Browse EWKit analyses and set up runs of them",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="site-head">
          <Link href="/" className="brand">
            EWKit Agentic Ops
          </Link>
          <nav>
            <Link href="/">Analyses</Link>
            <Link href="/setups/new">New run</Link>
          </nav>
        </header>
        <main className="site-main">{children}</main>
      </body>
    </html>
  );
}
