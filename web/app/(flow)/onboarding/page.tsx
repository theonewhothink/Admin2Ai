import type { Metadata } from "next";
import { Suspense } from "react";
import { OnboardingFlow } from "@/components/flow/OnboardingFlow";
import { OnboardingSetup } from "@/components/flow/OnboardingSetup";
import { production } from "@/lib/mode";

export const metadata: Metadata = { title: "Get started" };

export default function OnboardingPage() {
  return (
    <div className="container">
      {production ? (
        // Real connections for a signed-in owner; the demo keeps its preview flow.
        <Suspense>
          <OnboardingSetup />
        </Suspense>
      ) : (
        <OnboardingFlow />
      )}
    </div>
  );
}
