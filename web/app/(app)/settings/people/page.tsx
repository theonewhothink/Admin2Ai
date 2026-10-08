import type { Metadata } from "next";
import { PeopleView } from "@/components/settings/PeopleView";

export const metadata: Metadata = { title: "People and expenses" };

export default function PeoplePage() {
  return <PeopleView />;
}
