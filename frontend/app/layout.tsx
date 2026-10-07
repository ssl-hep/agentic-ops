import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "EWKit Agentic Ops",
  description: "Browse EWKit analyses, configs and sample lists by campaign",
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
          </nav>
        </header>
        <main className="site-main">{children}</main>
      </body>
    </html>
  );
}
