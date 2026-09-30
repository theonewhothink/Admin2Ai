/**
 * Sign in. Built only into server builds (the `.prod.tsx` page extension is
 * not a page extension of the static demo, see next.config.ts), and shown
 * only in production mode.
 */
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { SignInForm } from "@/components/auth/SignInForm";
import { production } from "@/lib/mode";

export const metadata: Metadata = { title: "Sign in" };

export default function SignInPage() {
  if (!production) notFound();
  return (
    <div className="container">
      <Suspense>
        <SignInForm />
      </Suspense>
    </div>
  );
}
