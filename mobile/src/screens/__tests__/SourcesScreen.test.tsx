import { describe, expect, it, jest } from "@jest/globals";
import { fireEvent, render, screen } from "@testing-library/react-native";
import type { ApiClient, UnderstandOutcome } from "../../api/client";
import { sampleSources } from "../../api/sample";
import { ServicesContext, type Services } from "../../app/servicesContext";
import { SourcesScreen } from "../SourcesScreen";

const mockRouter = { back: jest.fn(), navigate: jest.fn(), push: jest.fn() };
jest.mock("expo-router", () => ({ useRouter: () => mockRouter }));
jest.mock("react-native-safe-area-context", () => require("react-native-safe-area-context/jest/mock").default);

function setup(outcome: UnderstandOutcome) {
  const understandSource = jest.fn(async (_text: string) => outcome);
  const api = {
    getSources: async () => ({ data: sampleSources, source: "live" as const, asOf: 1 }),
    understandSource,
  } as unknown as ApiClient;
  const services = { api, lock: {}, offline: {} } as unknown as Services;
  const view = () =>
    render(
      <ServicesContext.Provider value={services}>
        <SourcesScreen />
      </ServicesContext.Provider>,
    );
  return { understandSource, view };
}

describe("Sources screen", () => {
  it("shows the summary, each source's line and the companies", async () => {
    await setup({ ok: false, reason: "demo" }).view();
    expect(await screen.findByText("I read 1 mailbox, 3 bank accounts and 4 cards for your 3 companies.")).toBeTruthy();
    expect(screen.getByText(sampleSources.coverage)).toBeTruthy();
    expect(screen.getByText("3 of 6 payments since 1 September have their invoice or proof; 2 need none and I'm looking for 1.")).toBeTruthy();
    expect(screen.getByText("Companies I cover")).toBeTruthy();
    expect(screen.getByText("laura@hazeltree.es, CaixaBank •••• 0265, Card •••• 5530")).toBeTruthy();
  });

  it("understands what is missing and says it, adding nothing itself", async () => {
    const { understandSource, view } = setup({
      ok: true,
      understood: { kind: "card", fields: { last4: "4821", bank: "Revolut" }, message: "Card •••• 4821 from Revolut. Check the company and tap Add.", already: false },
    });
    await view();
    await fireEvent.changeText(await screen.findByLabelText("Tell me what I'm not reading yet."), "my Revolut card ending 4821");
    await fireEvent.press(screen.getByRole("button", { name: "Add it" }));
    expect(understandSource).toHaveBeenCalledWith("my Revolut card ending 4821");
    expect(await screen.findByText("Card •••• 4821 from Revolut. Check the company and tap Add.")).toBeTruthy();
  });

  it("opens Ask with the text when it is something else", async () => {
    const { view } = setup({ ok: true, understood: { kind: "ask", fields: { text: "my lawyer" }, message: "I'll ask Claude to help with that.", already: false } });
    await view();
    await fireEvent.changeText(await screen.findByLabelText("Tell me what I'm not reading yet."), "my lawyer");
    await fireEvent.press(screen.getByRole("button", { name: "Add it" }));
    await fireEvent.press(await screen.findByRole("button", { name: "Ask about it" }));
    expect(mockRouter.navigate).toHaveBeenCalledWith({ pathname: "/ask", params: { q: "my lawyer" } });
  });
});
