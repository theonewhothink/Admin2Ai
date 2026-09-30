/**
 * Create an account. Built only into server builds (see next.config.ts) and
 * shown only in production mode.
 */
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { SignUpForm } from "@/components/auth/SignUpForm";
import { production } from "@/lib/mode";

export const metadata: Metadata = { title: "Create your account" };

export default function SignUpPage() {
  if (!production) notFound();
  return (
    <div className="container">
      <Suspense>
        <SignUpForm />
      </Suspense>
    </div>
  );
}
