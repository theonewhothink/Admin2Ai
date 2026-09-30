import type { Metadata, Viewport } from "next";
import { AdminShell } from "@/components/internal/AdminShell";
import { SessionProvider } from "@/components/session/SessionProvider";
import { production } from "@/lib/mode";

/** The team's internal dashboard ("Admin OS"). Never indexed; admin-only once sign-in exists. */
export const metadata: Metadata = {
  title: { default: "Command Center", template: "%s · Admin OS" },
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: "#020617",
  colorScheme: "dark",
};

export default function InternalLayout({ children }: { children: React.ReactNode }) {
  const shell = <AdminShell>{children}</AdminShell>;
  // Production: the signed-in session decides (admins only); without one, sign in first.
  return production ? <SessionProvider>{shell}</SessionProvider> : shell;
}
