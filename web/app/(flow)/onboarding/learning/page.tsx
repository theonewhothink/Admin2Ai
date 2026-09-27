import type { Metadata } from "next";
import { LearningFlow } from "@/components/flow/LearningFlow";
import { learningCounters, learningUnderstood, oneTapQuestions } from "@/lib/data";

export const metadata: Metadata = { title: "Learning your business" };

export default function LearningPage() {
  return (
    <div className="container">
      <LearningFlow counters={learningCounters} understood={learningUnderstood} questions={oneTapQuestions} />
    </div>
  );
}
