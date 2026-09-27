import type { Metadata } from "next";
import { OnboardingFlow } from "@/components/flow/OnboardingFlow";

export const metadata: Metadata = { title: "Get started" };

export default function OnboardingPage() {
  return (
    <div className="container">
      <OnboardingFlow />
    </div>
  );
}
