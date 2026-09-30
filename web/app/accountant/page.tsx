import { AccountantHomeView } from "@/components/accountant/AccountantHomeView";
import { LiveAccountantHome } from "@/components/live/pages";
import { clientRendered, getAccountantClients } from "@/lib/api";

export default async function AccountantHome() {
  if (clientRendered) return <LiveAccountantHome />;
  return <AccountantHomeView clients={await getAccountantClients()} />;
}
