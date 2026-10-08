import type { Metadata } from "next";
import { Suspense } from "react";
import { PaymentDetailView } from "@/components/documents/PaymentDetail";
import { Loading } from "@/components/live/Loading";

export const metadata: Metadata = { title: "Payment" };

/** One payment (`?id=`), read in the browser: new payments arrive at run time. */
export default function PaymentPage() {
  return (
    <Suspense fallback={<Loading />}>
      <PaymentDetailView />
    </Suspense>
  );
}
