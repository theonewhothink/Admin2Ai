"""The built-in chat understands the owner and never answers a different question.

Real owner phrasings (varied wording, typos, lower case, Portuguese month names)
are checked twice: the intent the understanding layer chooses, and the key facts
in the reply, computed by the engine from the demo tenant (frozen at 2 October
2026, bank records from 1 June 2026; June to August are the imported history).
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backoffice.assistant import CHANGING_TOOLS, TOOLS, ClaudeBrain, RuleBrain
from backoffice.service import BackOfficeService
from backoffice.spending import CATEGORIES
from backoffice.understanding import Vocabulary, find_periods, parse_amount, understand

TODAY = date(2026, 10, 2)


@pytest.fixture(scope="module")
def shared():
    """One demo for read-only questions (nothing in these cases changes state)."""
    return BackOfficeService.demo()


@pytest.fixture()
def svc():
    return BackOfficeService.demo()


def chat(svc, text, history=None):
    status, body = svc.dispatch("POST", "/api/chat", {"message": text, "history": history or []})
    assert status == 200, body
    return body


def kinds(body):
    return [c["type"] for c in body["cards"]]


# (message, intent, facts that must be in the reply)
CASES = [
    # -- spending ------------------------------------------------------------------------------
    ("what are my august expenses?", "spending", ["€242.29", "August", "3 payments", "bank history"]),
    ("What are my August expenses", "spending", ["€242.29", "Vodafone €92.40", "IKEA €89.90", "Adobe €59.99"]),
    ("august expenses", "spending", ["€242.29"]),
    ("expenses in agosto", "spending", ["€242.29", "August"]),
    ("gastos de agosto", "spending", ["€242.29"]),
    ("despesas de setembro", "spending", ["€5,024.53", "September"]),
    ("quanto gastamos em setembro", "spending", ["€5,024.53"]),
    ("how much did we spend in september?", "spending",
     ["€5,024.53", "12 payments", "€2,184.37 in taxes", "€13.52 in bank fees", "€500.00 moved between your companies"]),
    ("what did we spend last month", "spending", ["€5,024.53", "September"]),
    ("total spend september", "spending", ["€5,024.53"]),
    ("sept outgoings", "spending", ["€5,024.53"]),
    ("costs for september 2026", "spending", ["€5,024.53"]),
    ("expences for septmber", "spending", ["€5,024.53"]),
    ("how much did Company C spend on software in September", "spending",
     ["Company C spent €59.99 on software in September", "Adobe", "22 September"]),
    ("company c software spend september", "spending", ["€59.99", "Adobe"]),
    ("how much did hazel tree spend in september", "spending", ["Hazel Tree spent €3,565.86"]),
    ("what did company b spend last month", "spending", ["Company B spent €977.56", "Predial Alfama €950.00"]),
    ("what did company c spend in september", "spending",
     ["Company C spent €63.11", "Not counted yet", "€418.00 IKEA", "which company"]),
    ("what did we pay Vodafone this year", "spending",
     ["You paid Vodafone €369.60 in 2026 so far", "4 payments", "1 June 2026", "on hold"]),
    ("how much have we paid vodafone", "spending", ["€369.60", "since 1 June"]),
    ("what did we spend on vodaphone", "spending", ["Vodafone €369.60"]),
    ("vodafone spending by month", "spending", ["By month", "September €92.40"]),
    ("how much did we spend on uber", "spending", ["You paid Uber €42.15"]),
    ("how much rent did we pay in q3", "spending", ["€2,150.00 on rent", "July to September"]),
    ("rent in september", "spending", ["€2,150.00", "Marta Gonçalves €1,200.00", "Predial Alfama €950.00"]),
    ("how much did company b pay for rent", "spending", ["Company B spent €950.00 on rent"]),
    ("how much did we spend on travel in september", "spending", ["€42.15 on travel", "Uber"]),
    ("electicity costs last month", "spending", ["€64.10", "EDP"]),
    ("what did we spend on insurance in september", "spending", ["You spent nothing on insurance in September"]),
    ("what do we spend on software each month", "spending", ["€229.96 on software", "June €54.99"]),
    ("spending by company last month", "spending",
     ["Hazel Tree €3,565.86", "Company B €977.56", "Not decided yet €418.00", "Company C €63.11"]),
    ("spending by category in september", "spending", ["Taxes €2,184.37", "Rent €2,150.00", "Software €59.99"]),
    ("top 3 suppliers this year", "spending", ["Top 3", "Tax office €2,184.37", "€5,688.10"]),
    ("biggest expenses in september", "spending", ["Biggest: Tax office €2,184.37"]),
    ("compare august and july", "spending", ["€242.29", "€31.60 less than July (€273.89)"]),
    ("august vs july spending", "spending", ["€31.60 less than July"]),
    ("compare september with august", "spending", ["€4,782.24 more than August", "not like for like"]),
    ("costs excluding taxes in september", "spending", ["€2,840.16", "I left out taxes"]),
    ("how much did we spend between 1 and 15 september", "spending", ["€2,284.55", "1 to 15 September"]),
    ("expenses from june to august", "spending", ["€663.57", "June €147.39"]),
    ("how much did we spend since august", "spending", ["€5,266.82", "since August"]),
    ("spent in the last 30 days", "spending", ["€2,782.13", "last 30 days"]),
    ("how much tax did we pay this year", "spending", ["You paid €2,184.37 in taxes", "1 June 2026"]),
    ("bank fees in september", "spending", ["€13.52 in bank fees"]),
    ("what went out on 30 september", "spending", ["€13.52", "bank fees"]),
    ("payments yesterday", "spending", ["Nothing went out yesterday"]),
    ("what did we spend this month", "spending", ["Nothing went out in October so far"]),
    ("what did we spend in may", "spending", ["no payment records in May", "1 June 2026 to 2 October 2026"]),
    ("expenses last year", "spending", ["no payment records in 2025"]),
    ("who did we pay the most in september", "spending", ["Biggest: Tax office €2,184.37"]),
    ("which company spent the most in september", "spending", ["Hazel Tree €3,565.86", "Company B €977.56"]),
    ("list my suppliers", "spending", ["You paid 10 suppliers", "Marta Gonçalves €1,200.00", "since 1 June"]),
    ("average monthly spend", "spending", ["On average €1,422.02 a month over 4 whole months, June to September"]),
    ("how much do we spend a month", "spending", ["On average €1,422.02 a month"]),
    ("what did we spend in december", "spending", ["December 2025", "1 June 2026"]),
    # -- money in ------------------------------------------------------------------------------
    ("income last month", "income",
     ["Nothing came in from customers in September", "€500.00 from Hazel Tree to Company C"]),
    ("how much did we receive in september?", "income", ["Nothing came in from customers"]),
    ("revenue this year", "income", ["2026 so far"]),
    ("money in last month", "income", ["transfer between your companies"]),
    ("any refunds?", "income", ["and no refunds"]),
    # -- month status (only when asked) --------------------------------------------------------
    ("is september closed?", "month_status", ["Company B is closed", "Company C needs one answer"]),
    ("Is September complete?", "month_status", ["Company B is closed"]),
    ("has september been closed yet", "month_status", ["Hazel Tree is 90% done"]),
    ("month end status", "month_status", ["Almost."]),
    ("setembro está fechado?", "month_status", ["Company B is closed"]),
    ("is company b's september closed", "month_status", ["Yes. Company B’s September is closed, since 1 October."]),
    ("is august closed?", "month_status", ["I started closing months with September", "bank history"]),
    ("is october done?", "month_status", ["October isn't over yet"]),
    ("is company c closed?", "month_status", ["Not yet. Company C’s September is 66% closed: I need one answer"]),
    ("why is company c not closed", "month_status", ["Company C’s September is 66% closed"]),
    # -- needs you, due, VAT, subscriptions, fraud ---------------------------------------------
    ("anything I need to do?", "needs_me", ["IKEA payment of €418.00", "Vodafone"]),
    ("What still needs my attention?", "needs_me", ["IKEA"]),
    ("what's waiting for me?", "needs_me", ["Two things."]),
    ("do you need anything from me", "needs_me", ["which company it belongs to"]),
    ("which company does the ikea payment belong to", "needs_me", ["IKEA payment of €418.00"]),
    ("renewals coming up", "deadlines", ["Allianz (Company B) renews on 30 November", "Fidelidade"]),
    ("when does the allianz insurance renew", "deadlines", ["Allianz (Company B) renews on 30 November"]),
    ("is the vodafone payment safe", "fraud", ["I blocked the payment"]),
    ("what's due soon", "deadlines", ["Vodafone payment for Hazel Tree, 5 October", "€412.50", "20 October"]),
    ("taxes due", "deadlines", ["Tax payment for Company B, 20 October", "161377902"]),
    ("when is the next tax payment", "deadlines", ["€412.50"]),
    ("what do I have to pay this month", "deadlines", ["due soon"]),
    ("how much VAT did we pay in september", "vat",
     ["€109.04", "5 documents", "€2,184.37 VAT to the tax office on 21 September", "no sales invoices"]),
    ("iva de setembro", "vat", ["€109.04"]),
    ("which subscriptions went up", "subscriptions", ["Adobe: €54.99 → €59.99"]),
    ("Show subscriptions that increased.", "subscriptions", ["Adobe: €54.99 → €59.99"]),
    ("did any prices go up?", "subscriptions", ["Adobe"]),
    ("what are my subscriptions", "subscriptions", ["2 regular costs", "Adobe €59.99 a month", "Vodafone €92.40 a month"]),
    ("any suspicious invoices?", "fraud", ["LT24 •••• 1187", "Only you can release it"]),
    ("did any bank details change?", "fraud", ["Vodafone changed the IBAN"]),
    # -- payments and documents ---------------------------------------------------------------
    ("Did we pay Vodafone?", "payment_lookup", ["September", "FT VF2026/1183", "on hold"]),
    ("did we pay edp?", "payment_lookup", ["€64.10 on 19 September"]),
    ("show me the €418 payment", "payment_lookup", ["€418.00", "IKEA", "29 September"]),
    ("what is the €64.10 payment", "payment_lookup", ["EDP", "I asked EDP"]),
    ("what was the 2184.37 payment", "payment_lookup", ["tax office", "161204587", "nothing is missing"]),
    ("€89.90", "payment_lookup", ["IKEA", "19 August", "bank history"]),
    ("did we pay EDP in august?", "payment_lookup", ["No, I found no payment to EDP in August."]),
    ("did the tax payment go through?", "payment_lookup", ["€2,184.37 to the tax office", "€412.50 is due on 20 October"]),
    ("did we pay the rent in september?", "payment_lookup", ["€950.00 to Predial Alfama", "€1,200.00 to Marta Gonçalves"]),
    ("when did we last pay adobe", "payment_lookup", ["The last payment to Adobe was €59.99 on 22 September",
                                                      "FT AD2026/7734"]),
    ("Find the invoice for the €59.99 payment", "find_document", ["Adobe", "FT AD2026/7734"]),
    ("where is the adobe invoice", "find_document", ["FT AD2026/7734"]),
    ("find the EDP invoice", "find_document", ["I don't have the EDP invoice yet", "I asked EDP"]),
    ("what happened with the EDP invoice", "payment_lookup", ["The last payment to EDP was €64.10", "I asked EDP"]),
    ("which invoices did we get in september", "find_document", ["7 documents in September"]),
    ("find the invoice for the €800 payment yesterday", "find_document",
     ["I can't find an invoice or a payment of €800.00 yesterday."]),
    ("which invoices are missing", "missing_invoices", ["One payment still has no invoice", "EDP", "€64.10"]),
    ("payments without an invoice", "missing_invoices", ["EDP"]),
    ("what receipts are still missing for hazel tree", "missing_invoices", ["€64.10"]),
    # -- the rest -----------------------------------------------------------------------------
    ("is gmail connected?", "connections", ["Gmail (laura@hazeltree.pt) is connected", "09:12"]),
    ("are the banks syncing?", "connections", ["Millennium BCP", "Caixa Geral de Depósitos"]),
    ("What did the accountant ask this month?", "accountant", ["Marta Gonçalves", "Still open"]),
    ("any questions from marc?", "accountant", ["Two questions"]),
    ("who is my accountant", "accountant", ["Marc Vidal at Contabilidade Vidal", "One question from them is still open"]),
    ("what have you done this week", "activity", ["documents collected"]),
    ("how are we doing?", "overview", ["September is 85% closed", "Company B closed"]),
    ("what's my bank balance", "balance", ["I don't see account balances"]),
    ("are we profitable?", "profit", ["I can't work out profit"]),
    ("forecast next quarter spending", "forecast", ["I don't make forecasts"]),
    ("hello", "greeting", ["Hello."]),
    ("what can you do?", "help", ["whether a month is closed"]),
    ("thanks!", "thanks", ["You're welcome."]),
]


@pytest.mark.parametrize(("message", "intent", "facts"), CASES, ids=[c[0] for c in CASES])
def test_owner_phrasings(shared, message, intent, facts):
    understood = RuleBrain(shared.assistant).understand(message)
    assert understood.intent == intent, (understood.intent, understood.scores)
    reply = chat(shared, message)["reply"]
    for fact in facts:
        assert fact in reply, reply


def test_the_reported_failure_is_answered_with_august_spending(shared):
    """The exact case the owner hit: expenses, never month-closed chips."""
    body = chat(shared, "what are my august expenses?")
    assert kinds(body) == ["spending"]
    assert "closed" not in body["reply"].lower()
    card = body["cards"][0]
    assert card["total"] == 242.29 and card["count"] == 3 and card["unit"] == "payment"
    assert card["periodLabel"] == "August"
    assert card["from"] == "2026-08-01" and card["to"] == "2026-08-31"
    assert [r["label"] for r in card["rows"]] == ["Vodafone", "IKEA", "Adobe"]
    assert card["previous"] == {"label": "July", "total": 273.89, "change": -31.6}  # both from the history: like for like
    assert all(p["invoice"] == "history" for p in card["payments"])
    asked = shared.dispatch("POST", "/api/ask", {"question": "what are my august expenses?"})[1]
    assert "€242.29" in asked["answer"] and not any(e["id"].startswith("month:") for e in asked["evidence"])


def test_spending_card_carries_the_payments_behind_it(shared):
    card = chat(shared, "what did we spend in september")["cards"][0]
    assert card["type"] == "spending" and card["total"] == 5024.53 and card["count"] == 12
    assert card["previous"] is None  # August is imported history: not compared unless asked
    assert "Includes €2,184.37 in taxes." in card["notes"] and "Includes €13.52 in bank fees." in card["notes"]
    assert any("€500.00" in n for n in card["notes"])
    assert len(card["payments"]) == 12 and card["more"] == 0
    ikea = next(p for p in card["payments"] if p["label"] == "IKEA")
    assert ikea["company"] == "Not decided yet" and ikea["invoice"] == "matched"
    edp = next(p for p in card["payments"] if p["label"] == "EDP")
    assert edp["invoice"] == "missing" and edp["id"].startswith("ev_")
    for p in card["payments"]:
        shared.repo.evidence(p["id"])  # every payment cites stored evidence
    total = sum(Decimal(str(p["amount"])) for p in card["payments"])
    assert total == Decimal("5024.53")
    report = chat(shared, "create a report for september")["cards"][0]
    assert report["spent"] == card["total"]  # the report and the chat agree


@pytest.mark.parametrize(("message", "question_words"), [
    ("august", ["what you spent in August", "whether August is closed"]),
    ("vodafone", ["Vodafone", "what you paid", "its invoices", "a summary"]),
    ("company c", ["what Company C spent", "Company C’s September is closed"]),
    ("september and costs closed", ["what you spent in September", "whether September is closed"]),
])
def test_unclear_messages_get_one_short_question(shared, message, question_words):
    body = chat(shared, message)
    assert RuleBrain(shared.assistant).understand(message).intent == "clarify"
    assert body["reply"].endswith("?") and body["cards"] == []
    for words in question_words:
        assert words in body["reply"]


@pytest.mark.parametrize("message", [
    "tell me a joke", "What is the meaning of life", "what's the weather in lisbon", "whats the wether", "blah blah",
    "how much", "is it?", "who won the football",
])
def test_honest_fallback_never_a_wrong_answer(shared, message):
    body = chat(shared, message)
    assert RuleBrain(shared.assistant).understand(message).intent == "unknown"
    assert body["reply"].startswith("I can't answer that from your records.") and body["cards"] == []
    assert "What did we spend in September?" in body["reply"]


@pytest.mark.parametrize("message", [
    "Pay the Vodafone invoice", "please transfer €500 to company c", "can you pay EDP", "release the vodafone payment",
    "confirm the new bank details for vodafone", "approve the payment to vodafone",
])
def test_money_never_moves_from_the_chat(svc, message):
    body = chat(svc, message)
    assert body["reply"].startswith("I don't move money.")
    assert body["cards"][0]["items"][0]["id"] == "needs:nd_vodafone_iban"
    assert any(n["id"] == "nd_vodafone_iban" for n in svc.needs_you()["items"])


def test_follow_ups_use_the_conversation(shared):
    def turn(history, text):
        body = chat(shared, text, history)
        return history + [{"role": "user", "content": text}, {"role": "assistant", "content": body["reply"]}], body

    h, _ = turn([], "what did we spend in september")
    _, body = turn(h, "and in august?")
    assert "€242.29" in body["reply"]
    _, body = turn(h, "by company")
    assert "Hazel Tree €3,565.86" in body["reply"]
    _, body = turn(h, "is it closed?")
    assert "Company B is closed" in body["reply"]

    h, _ = turn([], "is september closed?")
    _, body = turn(h, "what about company b?")
    assert "Company B’s September is closed" in body["reply"]

    h, body = turn([], "august")
    assert body["reply"].endswith("?")
    _, body = turn(h, "what I spent")
    assert "€242.29" in body["reply"]
    _, body = turn(h, "whether it is closed")
    assert "I started closing months with September" in body["reply"]
    _, body = turn(h, "the first one")
    assert "€242.29" in body["reply"]

    h, _ = turn([], "vodafone")
    _, body = turn(h, "a summary please")
    assert body["cards"][0]["type"] == "summary" and body["cards"][0]["supplier"] == "Vodafone"
    _, body = turn(h, "what we paid")
    assert "You paid Vodafone €369.60" in body["reply"]
    _, body = turn(h, "the invoices")
    assert body["cards"][0]["type"] == "documents"

    _, body = turn(h, "hello")  # a new start is not a follow-up
    assert body["reply"].startswith("Hello.")


def test_ask_endpoint_shares_the_understanding_and_stays_read_only(svc):
    ask = lambda q: svc.dispatch("POST", "/api/ask", {"question": q})[1]  # noqa: E731
    spent = ask("how much did we spend in september?")
    assert "€5,024.53" in spent["answer"]
    assert spent["evidence"] and all(e.keys() == {"label", "id"} for e in spent["evidence"])
    for e in spent["evidence"]:
        if e["id"].startswith("month:"):  # "and N more": the month view that lists the rest (T9)
            continue
        svc.repo.evidence(e["id"])
    refused = ask("send the vodafone invoice to a@b.pt")
    assert "in the chat" in refused["answer"] and not svc.assistant.outbox
    assert ask("remind me to call marc")["answer"]  # read-only: nothing added
    assert not svc.assistant.tasks
    assert "I started closing months" in ask("is august closed?")["answer"]


def test_send_to_my_accountant_drafts_for_the_accountant(svc):
    body = chat(svc, "send the september report to my accountant")
    assert kinds(body) == ["report", "email"]
    assert body["cards"][1]["to"] == ["marc@contabilidadevidal.pt"] and body["cards"][1]["status"] == "draft"
    body = chat(svc, "Find the Vodafone invoice and send it to my accountant")
    assert kinds(body) == ["documents", "email"] and body["cards"][1]["to"] == ["marc@contabilidadevidal.pt"]
    assert all(m.status == "draft" for m in svc.assistant.outbox.values())


def test_answers_follow_the_owners_answers(svc):
    """After the owner puts IKEA on Company C, Company C's spending counts it (the engine's state, not a copy)."""
    before = chat(svc, "what did company c spend in september")["reply"]
    assert "Not counted yet" in before
    svc.dispatch("POST", "/api/needs-you/nd_ikea_418/answer", {"option_id": "entity:company-c"})
    after = chat(svc, "what did company c spend in september")["reply"]
    assert "Company C spent €481.11" in after and "Not counted yet" not in after


def test_the_owner_answers_which_company_in_words(svc):
    """The same answer as the Needs you tap; changed bank details are never answered this way."""
    assert chat(svc, "is the ikea payment for hazel tree?")["reply"]  # a question, not an answer
    assert any(n["id"] == "nd_ikea_418" for n in svc.needs_you()["items"])
    assert chat(svc, "put the ikea payment on hazel tree")["reply"] == "Done. The IKEA payment is now with Hazel Tree."
    assert [n["id"] for n in svc.needs_you()["items"]] == ["nd_vodafone_iban"]
    assert "Hazel Tree spent €3,983.86" in chat(svc, "what did hazel tree spend in september")["reply"]
    # Nothing left to answer: the words fall through to normal understanding, and bank details stay held.
    assert "Done." not in chat(svc, "put the vodafone payment on hazel tree")["reply"]
    assert any(n["id"] == "nd_vodafone_iban" for n in svc.needs_you()["items"])


def test_personal_answer_and_remembering(svc):
    reply = chat(svc, "the €418 payment is personal, always")["reply"]
    assert reply.startswith("Done. The IKEA payment is set aside")
    assert "IKEA" not in chat(svc, "what did we spend in september")["cards"][0]["rows"].__repr__()


def test_accountant_category_rules_decide_the_category(svc):
    svc.dispatch("POST", "/api/accountant/rules", {"text": "Treat all Uber costs as Software"})
    reply = chat(svc, "how much did we spend on software in september")["reply"]
    assert "€102.14" in reply  # Adobe €59.99 + Uber €42.15: the accountant's rule wins over the wording
    assert "spent nothing on travel" in chat(svc, "travel costs in september")["reply"]


# -- periods and amounts ------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "start", "end"), [
    ("last month", date(2026, 9, 1), date(2026, 9, 30)),
    ("this month", date(2026, 10, 1), date(2026, 10, 31)),
    ("this year", date(2026, 1, 1), date(2026, 12, 31)),
    ("last year", date(2025, 1, 1), date(2025, 12, 31)),
    ("q3", date(2026, 7, 1), date(2026, 9, 30)),
    ("third quarter of 2025", date(2025, 7, 1), date(2025, 9, 30)),
    ("last quarter", date(2026, 7, 1), date(2026, 9, 30)),
    ("between 1 and 15 september", date(2026, 9, 1), date(2026, 9, 15)),
    ("between 1 september 2026 and 30 september 2026", date(2026, 9, 1), date(2026, 9, 30)),
    ("from june to august", date(2026, 6, 1), date(2026, 8, 31)),
    ("since august", date(2026, 8, 1), TODAY),
    ("yesterday", date(2026, 10, 1), date(2026, 10, 1)),
    ("ontem", date(2026, 10, 1), date(2026, 10, 1)),
    ("last week", date(2026, 9, 21), date(2026, 9, 27)),
    ("21/09/2026", date(2026, 9, 21), date(2026, 9, 21)),
    ("2026-09-21", date(2026, 9, 21), date(2026, 9, 21)),
    ("21st of september", date(2026, 9, 21), date(2026, 9, 21)),
    ("september 21", date(2026, 9, 21), date(2026, 9, 21)),
    ("december", date(2025, 12, 1), date(2025, 12, 31)),
    ("setembro de 2026", date(2026, 9, 1), date(2026, 9, 30)),
    ("mes passado", date(2026, 9, 1), date(2026, 9, 30)),
    ("the last 30 days", date(2026, 9, 3), TODAY),
    ("the past 2 years", date(2024, 10, 3), TODAY),
    ("in 2025", date(2025, 1, 1), date(2025, 12, 31)),
])
def test_periods(text, start, end):
    periods, _ = find_periods(text, TODAY)
    assert (periods[0].start, periods[0].end) == (start, end)


@pytest.mark.parametrize("text", ["may i ask something", "money out", "set up a task", "two months ago",
                                  "what is the €92.40 payment", "invoice FT VF2026/1183"])
def test_ordinary_words_are_not_periods(text):
    assert find_periods(text, TODAY)[0] == []


@pytest.mark.parametrize(("raw", "value"), [("1.200,00", "1200.00"), ("1,200.00", "1200.00"), ("92.40", "92.40"),
                                            ("92,40", "92.40"), ("418", "418"), ("1.200", "1200"), ("x", None)])
def test_amounts(raw, value):
    assert parse_amount(raw) == (Decimal(value) if value else None)


def test_vocabulary_comes_from_the_engine(shared):
    vocab = Vocabulary.from_repo(shared.repo, CATEGORIES)
    assert {"hazel-tree", "company-b", "company-c"} == set(vocab.companies)
    assert "marta" in vocab.suppliers["sup-landlord"] and "vodafone" in vocab.suppliers["sup-vodafone"]
    u = understand("how much did hazeltree pay marta in sept 2026", vocab, TODAY)
    assert u.slots.company_ids == ["hazel-tree"] and u.slots.supplier_ids == ["sup-landlord"]
    assert u.slots.period is not None and u.slots.period.label == "September"


# -- Claude uses the same data ---------------------------------------------------------------------


def tool(svc, name, **args):
    status, body = svc.dispatch("POST", "/api/chat/tool", {"name": name, "input": args})
    assert status == 200, body
    return body


def test_claude_tools_answer_money_questions_from_data(shared):
    names = {t["name"] for t in TOOLS}
    new = {"spending_summary", "find_payments", "missing_invoices", "vat_summary", "recurring_costs", "due_soon",
           "accountant_questions"}
    assert new <= names and not new & CHANGING_TOOLS  # read-only
    system = shared.dispatch("GET", "/api/chat/tools", None)[1]["system"]
    assert "spending_summary" in system and "month_status only when" in system

    aug = tool(shared, "spending_summary", date_from="2026-08-01", date_to="2026-08-31", compare_previous=True)
    assert not aug["isError"] and aug["cards"][0]["type"] == "spending"
    r = aug["result"]
    assert r["total_eur"] == 242.29 and r["payments"] == 3 and r["previous_period"]["total_eur"] == 273.89
    assert any("bank history" in n for n in r["notes"])
    sep = tool(shared, "spending_summary", date_from="2026-09-01", date_to="2026-09-30", group_by="company")["result"]
    assert sep["included"]["taxes"] == 2184.37 and sep["included"]["bank_fees"] == 13.52
    assert sep["left_out"]["transfers_between_own_accounts_and_companies_eur"] == 500.0
    soft = tool(shared, "spending_summary", date_from="2026-09-01", date_to="2026-09-30", company_id="company-c",
                category="software")["result"]
    assert soft["total_eur"] == 59.99 and soft["payment_list"][0]["merchant"] == "Adobe"
    income = tool(shared, "spending_summary", date_from="2026-09-01", date_to="2026-09-30", direction="in")["result"]
    assert income["total_eur"] == 0 and income["left_out"]["transfers_between_own_accounts_and_companies_eur"] == 500.0
    may = tool(shared, "spending_summary", date_from="2026-05-01", date_to="2026-05-31")
    assert may["result"]["records_cover_period"] is False and may["cards"] == []

    ikea = tool(shared, "find_payments", amount=418)["result"]["payments"]
    assert ikea[0]["merchant"] == "IKEA" and ikea[0]["company_not_decided_yet"] is True
    missing = tool(shared, "missing_invoices")["result"]
    assert [m["merchant"] for m in missing] == ["EDP"] and "I asked EDP" in missing[0]["what_i_am_doing"]
    vat_out = tool(shared, "vat_summary", date_from="2026-09-01", date_to="2026-09-30")
    assert vat_out["cards"][0]["unit"] == "document" and vat_out["cards"][0]["count"] == 5
    vat = vat_out["result"]
    assert vat["purchase_vat_eur"] == 109.04 and vat["vat_paid_to_tax_office"][0]["amount_eur"] == 2184.37
    assert tool(shared, "recurring_costs")["result"]["price_increases"][0]["name"] == "Adobe"
    due = tool(shared, "due_soon")["result"]
    assert due["unpaid_tax_and_other_obligations"][0]["amount_eur"] == 412.5
    assert len(tool(shared, "accountant_questions")["result"]) == 2

    for bad in ({"date_from": "x", "date_to": "2026-09-30"}, {},
                {"date_from": "2026-09-01", "date_to": "2026-09-30", "category": "unicorns"},
                {"date_from": "2026-09-01", "date_to": "2026-09-30", "company_id": "nope"},
                {"date_from": "2026-09-01", "date_to": "2026-09-30", "supplier": "Nobody Ltd"}):
        assert tool(shared, "spending_summary", **bad)["isError"]


def test_claude_brain_uses_spending_summary(svc):
    turns = iter([
        SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
            type="tool_use", id="t1", name="spending_summary",
            input={"date_from": "2026-08-01", "date_to": "2026-08-31"})]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="€242.29 in August.")]),
    ])
    seen = []

    class Messages:
        def create(self, **kw):
            seen.append(kw)
            return next(turns)

    out = ClaudeBrain(svc.assistant, client=SimpleNamespace(messages=Messages())).handle("what are my august expenses?")
    assert out["reply"] == "€242.29 in August." and [c["type"] for c in out["cards"]] == ["spending"]
    result = next(m for m in seen[1]["messages"] if isinstance(m["content"], list) and m["role"] == "user")["content"][0]
    assert result["type"] == "tool_result" and "242.29" in result["content"]


# --- Regression: an unrelated question never gets the previous topic's answer -------------------------

def _conversation(svc, questions):
    history, replies = [], []
    for q in questions:
        status, body = svc.dispatch("POST", "/api/chat", {"message": q, "history": history})
        assert status == 200, body
        replies.append(body["reply"])
        history += [{"role": "user", "content": q}, {"role": "assistant", "content": body["reply"]}]
    return replies


def test_unrelated_questions_after_a_topic_get_the_honest_fallback():
    svc = BackOfficeService.demo()
    replies = _conversation(svc, ["is september closed?", "what's the weather in porto",
                                  "who won the football yesterday"])
    assert "Company C needs one answer" in replies[0]
    for reply in replies[1:]:
        assert reply.startswith("I can't answer that from your records."), reply
        assert "€" not in reply and "closed" not in reply.split(".")[0]


def test_follow_up_chain_keeps_each_change():
    svc = BackOfficeService.demo()
    replies = _conversation(svc, ["what are my august expenses?", "and in september?", "what about company c?"])
    assert replies[0].startswith("You spent €242.29 in August")
    assert replies[1].startswith("You spent €5,024.53 in September")
    assert replies[2].startswith("Company C spent") and "in September" in replies[2]  # not August
