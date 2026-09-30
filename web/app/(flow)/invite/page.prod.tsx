/**
 * An accountant's invitation (§29): "Your accountant has enabled Back Office for you." Built only into
 * server builds (the `.prod.tsx` page extension, see next.config.ts) and shown only in production mode.
 */
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { AcceptInvitation } from "@/components/auth/AcceptInvitation";
import { production } from "@/lib/mode";

export const metadata: Metadata = { title: "Your accountant invited you" };

export default function InvitePage() {
  if (!production) notFound();
  return (
    <div className="container">
      <AcceptInvitation />
    </div>
  );
}
