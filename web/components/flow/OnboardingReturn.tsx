"use client";

/**
 * Production: after Google, Microsoft or the bank sends the owner back into the
 * app (for example to /sources?signin=done), continue onboarding where they
 * left it instead of stranding them on another page.
 */
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { readProgress, returnFrom, saveProgress } from "@/lib/onboarding-progress";

export function OnboardingReturn() {
  const router = useRouter();
  useEffect(() => {
    const progress = readProgress();
    const back = returnFrom(progress, window.location.search, Date.now());
    if (!progress || !back) return;
    // One return per trip: a later visit to this page is not a return.
    saveProgress({ step: progress.step });
    router.replace(`/onboarding?returned=${back.leg}&result=${back.result}`);
  }, [router]);
  return null;
}
