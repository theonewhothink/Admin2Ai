"""Portugal's own words for the core's readers and writers (§49-50): the Portugal pack's ``vocabulary()``.

Each key is a concept a core module asks for (:mod:`backoffice.countries.wording`), named after that module; the
module says what its entries are. Unless a comment says otherwise, entries are regular-expression alternatives
over the module's folded text (lower case, no accents), in the order the module tries them; "plain" entries are
words or phrases compared as they are. A key ending in ":2", ":3" ... is the second, third ... place in an ordered
list of the core's where the pack's words go (``backoffice.countries.wording.spliced``).

Conventions from Portuguese documents, bank statements, tills, messages and exports, not legal facts
(verified_as_of: never); extend them as they are seen. Letters to suppliers are in :mod:`.letters`.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = ["VOCABULARY"]

_V: dict[str, tuple[str, ...]] = {}

# ===================================================================================== shared by several readers

# Months (every reader of dates): "month:<n>" the full name, "month_abbr:<n>" its abbreviation (plain, folded).
_MONTHS = ("janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
           "novembro", "dezembro")
_ABBREVIATIONS = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")
_V.update({f"month:{n}": (name,) for n, name in enumerate(_MONTHS, 1)})
_V.update({f"month_abbr:{n}": (abbr,) for n, abbr in enumerate(_ABBREVIATIONS, 1)})

# The label printed before a tax number ("NIF: ...", any case), wherever a line is cut at it.
_V["tax_id_label"] = (r"nif", r"nipc", r"contribuinte")
# A tax number written in a text (backoffice._reading.tax_ids): one pattern, its first group the number. A NIF:
# 9 digits, not starting with 0.
_V["tax_ids.pattern"] = (r"(?<![\dA-Z])(?:PT\s?)?([1-9]\d{8})(?!\d)",)

# Glossary (backoffice.accountant_questions): an English word -> the words a Portuguese document prints for it
# (plain, folded; a description's word may start with one).
_GLOSSARY: dict[str, tuple[str, ...]] = {
    "rent": ("renda", "arrendamento", "aluguer"),
    "rental": ("renda", "arrendamento", "aluguer"),
    "lease": ("renda", "arrendamento", "aluguer", "locacao"),
    "studio": ("estudio",),
    "office": ("escritorio",),
    "shop": ("loja",),
    "warehouse": ("armazem",),
    "flat": ("apartamento",),
    "apartment": ("apartamento",),
    "electricity": ("eletricidade", "electricidade", "energia"),
    "energy": ("energia", "eletricidade", "electricidade"),
    "gas": ("gas",),
    "water": ("agua",),
    "phone": ("telefone", "telemovel", "movel", "comunicacoes"),
    "telephone": ("telefone", "telemovel", "comunicacoes"),
    "mobile": ("telemovel", "movel"),
    "internet": ("internet", "fibra"),
    "subscription": ("subscricao", "assinatura"),
    "trip": ("viagem",),
    "ride": ("viagem",),
    "taxi": ("viagem", "taxi"),
    "travel": ("viagem", "viagens"),
    "furniture": ("moveis", "mobiliario", "movel"),
    "insurance": ("seguro", "seguros"),
    "cleaning": ("limpeza",),
    "maintenance": ("manutencao",),
    "repair": ("reparacao",),
}
_V.update({f"glossary:{english}": words for english, words in _GLOSSARY.items()})

# ===================================================================================== fraud (backoffice.fraud.phrases)

# Suspicious payment-instruction language (§26), by category.
_V["fraud.bank_change"] = (
    r"nov[oa]s? dados bancarios",
    r"novo iban",
    r"nova conta bancaria",
    r"novo nib",
    r"alteracao (?:de|do|dos|da) (?:iban|nib|dados bancarios|conta bancaria|banco)",
    r"(?:alteramos|mudamos|alterou|mudou) (?:o |a |de |os )?(?:nosso |nossa |nossos )?"
    r"(?:iban|nib|banco|conta bancaria|dados bancarios)",
    r"mudanca (?:de|do|dos|da) (?:banco|conta bancaria|iban|nib|dados bancarios)",
    r"atualizacao (?:de|do|dos) (?:iban|nib|dados bancarios)",
    r"(?:nao|por favor nao) (?:utilize|use|pague para) (?:a|o) (?:conta|iban|nib) antig[oa]",
    r"conta bancaria diferente",
)
_V["fraud.urgency"] = (
    r"urgente",
    r"com urgencia",
    r"imediatamente|de imediato|pagamento imediato",
    r"hoje sem falta|sem demora",
)
_V["fraud.secrecy"] = (
    r"confidencial(?:mente)?",
    r"sigilo(?:so|sa)?",
    r"(?:nao) (?:ligue|telefone|contacte|partilhe|comente)",
    r"discricao",
)

# ===================================================================================== the owner's chat

# backoffice.understanding. Month abbreviations that are also ordinary words, and words before a month that make
# one of them a month ("em set"), plain.
_V["chat.month_ambiguous"] = ("ago", "set", "out")
_V["chat.month_context"] = ("de", "em", "no", "na", "desde", "ate", "e")
# Number words by value (plain), and the words of rolling periods ("nos ultimos 3 meses").
_V["chat.number:1"] = ("um", "uma")
_V["chat.number:2"] = ("dois", "duas")
_V["chat.number:3"] = ("tres",)
_V["chat.number:6"] = ("seis",)
_V["chat.number:12"] = ("doze",)
_V["chat.unit:day"] = ("dias",)
_V["chat.unit:week"] = ("semanas",)
_V["chat.unit:month"] = ("meses",)
_V["chat.unit:year"] = ("anos",)
_V["chat.rolling_last"] = (r"ultim[oa]s", r"nos ultim[oa]s")
# Days and ranges: "de 1 a 15 de setembro de 2026", "1o de setembro".
_V["chat.range_from"] = (r"de\s+", r"entre\s+")
_V["chat.range_to"] = ("a", "e", "ate")
_V["chat.of"] = (r"de\s+",)
_V["chat.ordinal"] = ("o",)
# Relative periods, by the core's name for them (whole alternatives, with their word boundaries).
_V["chat.relative:before_yesterday"] = (r"\banteontem\b",)
_V["chat.relative:yesterday"] = (r"\bontem\b",)
_V["chat.relative:today"] = (r"\bhoje\b",)
_V["chat.relative:this_week"] = (r"\besta semana\b",)
_V["chat.relative:last_week"] = (r"\bsemana passada\b",)
_V["chat.relative:this_month"] = (r"\beste mes\b",)
_V["chat.relative:last_month"] = (r"\bmes passado\b", r"\bultimo mes\b", r"\bmes anterior\b")
_V["chat.relative:this_quarter"] = (r"\beste trimestre\b",)
_V["chat.relative:last_quarter"] = (r"\btrimestre (?:passado|anterior)\b", r"\bultimo trimestre\b")
_V["chat.relative:this_year"] = (r"\beste ano\b",)
_V["chat.relative:last_year"] = (r"\bano passado\b", r"\bano anterior\b")
# A quarter: one pattern per entry, its first group the quarter's number and its second the year.
_V["chat.quarter"] = (r"\b([1-4])(?:o)?\s+trimestre\b(?:\s+(?:de\s+)?(\d{4}))?",)
# Details of a question (whole alternatives): an average, money in, every company, "without", to the
# accountant, "month" as a noun.
_V["chat.average"] = (r"\bmedia\b", r"\bpor mes\b")
_V["chat.money_in"] = (r"receitas", r"recebi\w*")
_V["chat.all_companies"] = (r"\btodas as empresas\b",)
_V["chat.excluding"] = ("sem",)
_V["chat.accountant"] = ("contabilista",)
_V["chat.month_noun"] = (r"\bmes\b",)
# Words of a payment, a document and a tax (alternatives inside the core's own word boundaries).
_V["chat.payment_words"] = (r"pagamentos?", r"faturas?")
_V["chat.document_words"] = (r"faturas?", r"recibos?")
_V["chat.tax_words"] = (r"iva", r"impostos?", r"irs", r"irc", r"seguranca social")
# Words typos are corrected towards, and words never taken for a supplier's name on their own (plain).
_V["chat.keywords"] = ("contabilista", "despesas", "gastos", "custos", "faturas", "recibos", "pagamentos", "receitas",
                       "impostos")
_V["chat.name_stopwords"] = ("portugal", "lisboa", "porto")
# What a question is about: "<weight> <pattern>", the pattern searched in the folded message.
_V["chat.cue:spending"] = (r"1.0 \b(?:despesas?|gastos?|gastei|gastamos|gastaram|custos?)\b",
                           r"0.5 \bpagamentos\b",
                           r"1.0 \bquanto\b.*\b(?:gast|pag)")
_V["chat.cue:income"] = (r"1.0 \b(?:receitas?|recebemos|recebido|recebi|vendas|faturacao)\b",)
_V["chat.cue:vat"] = (r"1.1 \biva\b",)
_V["chat.cue:payment_lookup"] = (r"0.45 \bpagamentos?\b",)
_V["chat.cue:report"] = (r"1.3 \brelatorios?\b",)
_V["chat.cue:missing_invoices"] = (r"1.0 \bem falta\b|\bfaltam?\b",)
_V["chat.cue:month_status"] = (r"0.4 \b(?:fechad[oa]|fechar|concluid[oa])\b", r"1.1 \bfecho (?:do|de) mes\b")
_V["chat.cue:deadlines"] = (r"1.0 \bprazos?\b|\bvencimentos?\b",)
_V["chat.cue:subscriptions"] = (r"1.0 \bassinaturas?\b", r"1.2 \baument\w*\b|\bsubiu\b|\bsubiram\b")
_V["chat.cue:fraud"] = (r"1.0 \bfraude\b",)
_V["chat.cue:accountant"] = (r"1.0 \bcontabilist\w*\b|\bcontabilidade\b",)
_V["chat.cue:balance"] = (r"1.0 \bsaldo\b",)
_V["chat.cue:profit"] = (r"1.1 \blucro\b",)
# Greetings, thanks and follow-ups ("e em agosto?"): alternatives at the core's place in each pattern; plain filler.
_V["chat.greeting"] = ("ola", "oi", "bom dia", "boa tarde", "boa noite")
_V["chat.thanks"] = (r"obrigad[oa]",)
_V["chat.follow_up"] = ("e", "e em", "e no", "e na")
_V["chat.follow_filler"] = tuple("e em no na nos nas de do da dos das para o a os as tambem mesmo e sobre ano mes "
                                 "semana".split())

# backoffice.assistant: whole alternatives ("assistant.increase", "assistant.statement*", "assistant.unit_price"),
# else alternatives inside the core's word boundaries.
_V["assistant.increase"] = (r"\baument\w*", r"\bsubiu\b", r"\bsubiram\b")
_V["assistant.document_words"] = (r"faturas?",)
_V["assistant.tax_words"] = (r"iva", r"impostos?")
_V["assistant.statement"] = (r"\bextratos?\b", r"\bconta\s+corrente\b")  # any case
_V["assistant.statement_word"] = (r"\bextratos?\b",)
_V["assistant.insurance"] = (r"seguros?",)
_V["assistant.unit_price"] = (r"\bpor (?:kg|quilo|kilo|litro|unidade|saco|caixa)\b",
                              r"\bprecos? (?:por|unitarios?)\b")
_V["assistant.basis:kg"] = (r"quilos?",)
_V["assistant.basis:l"] = (r"litros?",)
_V["assistant.basis:unit"] = (r"unidades?",)
_V["assistant.basis:bag"] = (r"sacos?",)
_V["assistant.basis:box"] = (r"caixas?",)

# backoffice.accountant_questions: document nouns and a judgment word (plain), and the start of a document line
# that is a field, not a description.
_V["accountant_questions.document_nouns"] = ("fatura", "faturas", "recibo", "recibos")
_V["accountant_questions.never"] = ("iva",)
_V["accountant_questions.field_line"] = ("nipc", "atcud", "fatura", "isento", "contribuinte", "vencimento", "entidade",
                                         "montante")

# backoffice.tax_profiles: the accountant's sentence about a company's tax regime ("IVA mensal", "não tem
# trabalhadores", "pagamentos por conta", "Modelo 10").
_V["tax_profiles.no"] = (r"sem", r"nao\s+tem", r"nao\s+faz")
_V["tax_profiles.vat"] = (r"iva",)
_V["tax_profiles.monthly"] = (r"mensal", r"mensalmente")
_V["tax_profiles.quarterly"] = (r"trimestral", r"trimestralmente")
_V["tax_profiles.exempt"] = (r"isento", r"isenta", r"isencao")
_V["tax_profiles.staff"] = (r"trabalhadores", r"funcionarios", r"salarios")
_V["tax_profiles.has"] = (r"tem", r"com", r"paga")
_V["tax_profiles.invoice"] = (r"faturas?",)
_V["tax_profiles.advance"] = (r"pagamentos?\s+por\s+conta",)
_V["tax_profiles.other_form"] = (r"modelo\s+10",)  # the yearly return of rents and fees with tax withheld

# ===================================================================================== documents

# backoffice.orchestrator: what a document calls itself, tried before the core's names of the kind.
_V["doc_kind:invoice_word"] = (r"fatura",)  # in "<word> pró-forma", "<word>-recibo", "<word> simplificada"
_V["doc_kind:quote"] = (r"orcamento",)
_V["doc_kind:delivery_note"] = (r"guia\s+de\s+(?:remessa|transporte)",)
_V["doc_kind:order_confirmation"] = (r"confirmacao\s+(?:de|da)\s+encomenda", r"nota\s+de\s+encomenda")
_V["doc_kind:supplier_statement"] = (r"extrato\s+(?:de\s+)?conta[\s-]+corrente",)
_V["doc_kind:receipt"] = (r"talao(?:\s+de\s+venda)?",)
_V["foreign_title_words"] = ("fatura", "atcud")  # plain: a line starting with one is never a supplier's name
# The invoice a credit note corrects ("referente à fatura FT 2026/183", "Documento de origem: FT 2026/183"; any case).
_V["invoice_reference.verb"] = (r"referente", r"relativ[ao]", r"respeitante", r"correspondente", r"retifica",
                                r"rectifica", r"anula", r"corrige")
_V["invoice_reference.preposition"] = (r"[àa]o?", r"da", r"do", r"de")
_V["invoice_reference.invoice_word"] = (r"fatura",)
_V["invoice_reference.origin"] = (r"documento\s+de\s+origem", r"doc\.?\s+(?:de\s+)?origem")
_V["invoice_reference.series"] = ("FT", "FR", "FS", "ND", "VD")
# Paid in cash, and a payment that is not cash; "cash e carry".
_V["cash.paid"] = (r"numerario", r"(?:pago|pagamento|paga)\s+em\s+dinheiro", r"dinheiro")
_V["cash.not_cash"] = (r"cartao", r"multibanco", r"mb\s*way", r"transferencia", r"debito\s+direto", r"tpa")
_V["cash.and"] = (r"e",)
# A statement's header fields and the tax number's label after its supplier's name.
_V["statement.field"] = (r"nipc",)
_V["statement.tax_id_label"] = (r"NIPC",)

# backoffice.extraction.addressee: who an invoice is billed to (any case), and a postal code ("4000-123").
_V["addressee.name_prefix"] = (r"dados\s+do\s+",)
_V["addressee.name"] = (r"adquirente", r"faturado\s+a")
_V["addressee.name_word"] = (r"nome",)
_V["addressee.address"] = (r"morada", r"endere[cç]o")
_V["addressee.address_suffix"] = (r"\s+de\s+fatura[cç][aã]o",)
_V["addressee.street"] = (r"rua", r"r\.", r"pra[cç]a", r"largo", r"travessa", r"tv\.", r"estrada", r"alameda",
                          r"cal[cç]ada", r"beco", r"rotunda", r"urbaniza[cç][aã]o", r"quinta")
_V["addressee.postal_code"] = (r"(?<!\d)\d{4}\s*-\s*\d{3}(?!\d)",)

# backoffice.ocr.providers.claude: the examples the model is given (plain).
_V["ocr.example:invoice_number"] = ("FT 2026/183",)
_V["ocr.tax_id_name"] = ("NIF",)
_V["ocr.example:payment_reference"] = ("Multibanco entity/reference",)

# backoffice.line_prices: unit words by unit, packing words, filler words and raw materials (plain, folded); a
# table's column headings and the totals' labels.
_V["line_prices.unit:kg"] = ("quilo", "quilos", "kilograma", "kilogramas")
_V["line_prices.unit:g"] = ("grama", "gramas")
_V["line_prices.unit:unit"] = ("unidade", "unidades")
_V["line_prices.unit:box"] = ("cx", "caixa", "caixas")
_V["line_prices.unit:dozen"] = ("dz", "duzia", "duzias")
_V["line_prices.unit:pack"] = ("emb", "embalagem")
_V["line_prices.unit:bottle"] = ("gf", "garrafa", "garrafas")
_V["line_prices.packing"] = ("embalado", "embalada", "avulso", "pacote", "palete")
_V["line_prices.stop"] = ("do", "da", "dos", "das", "e", "com", "o", "em")
_MATERIALS: dict[str, tuple[str, ...]] = {
    "flour": ("farinha",), "sugar": ("acucar",), "butter": ("manteiga",), "milk": ("leite",), "eggs": ("ovo", "ovos"),
    "yeast": ("fermento", "levedura"), "oil": ("oleo", "azeite"), "cheese": ("queijo",), "cream": ("natas",),
    "chocolate": ("cacau",), "chicken": ("frango",), "fish": ("peixe",), "potatoes": ("batata", "batatas"),
    "onions": ("cebola", "cebolas"), "almonds": ("amendoa", "amendoas"), "wine": ("vinho",), "beer": ("cerveja",),
    "cement": ("cimento",), "steel": ("aco",),
}
_V.update({f"line_prices.material:{concept}": words for concept, words in _MATERIALS.items()})
_V["line_prices.head:code"] = (r"codigo", r"artigo n", r"art")
_V["line_prices.head:desc"] = (r"descricao", r"designacao", r"produto", r"servico", r"artigo")
_V["line_prices.head:qty"] = (r"qtd", r"qtde", r"quant", r"quantidade")
_V["line_prices.head:price"] = (r"preco",)
_V["line_prices.head:total"] = (r"montante", r"liquido")
_V["line_prices.head:vat"] = (r"imposto", r"taxa")
_V["line_prices.head:disc"] = (r"desconto",)
_V["line_prices.base_label"] = (r"base tributavel", r"base de incidencia", r"incidencia", r"valor liquido",
                                r"total liquido", r"total sem iva")
_V["line_prices.vat_label"] = (r"imposto",)
_V["line_prices.column:unit"] = (r"unidade",)
_V["line_prices.column:vat_amount"] = (r"valor iva", r"montante iva")
_V["line_prices.column:gross"] = (r"total c iva", r"total com iva", r"valor c iva")
_V["line_prices.table_end"] = (r"totais", r"portes", r"resumo", r"descontos?")

# backoffice.payroll: a payslip's title and labels, and a figure's labels (plain).
_V["payroll.title"] = (r"recibos?\s+de\s+vencimentos?", r"recibo\s+de\s+(?:salario|remuneracoes?)",
                       r"folha\s+de\s+vencimentos?")
_V["payroll.employee"] = (r"nome\s+do\s+(?:trabalhador|funcionario|colaborador)", r"trabalhador", r"funcionario",
                          r"colaborador", r"nome")
_V["payroll.employer"] = (r"entidade\s+(?:patronal|empregadora)", r"empregador", r"empresa")
_V["payroll.period"] = (r"periodo(?:\s+de\s+processamento)?", r"mes", r"referente\s+a")
_V["payroll.date"] = (r"data(?:\s+de\s+(?:emissao|pagamento))?",)
_V["payroll.label:gross"] = ("total iliquido", "remuneracao iliquida", "vencimento iliquido", "total de remuneracoes",
                             "total abonos", "total de abonos")
_V["payroll.label:social"] = ("seguranca social", "seg social", "seg. social", "tsu")
_V["payroll.label:income_tax"] = ("retencao irs", "retencao de irs", "irs", "retencao na fonte")
_V["payroll.label:other"] = ("outros descontos", "quotizacao sindical")
_V["payroll.label:deductions"] = ("total de descontos", "total descontos")
_V["payroll.label:net"] = ("liquido a receber", "valor liquido", "total liquido", "liquido a pagar", "liquido")

# backoffice.leases: leasing and renting contracts.
_V["leases.title"] = (r"contrato\s+(?:de\s+)?(?:locacao\s+financeira|leasing|renting|aluguer\s+(?:de\s+)?"
                      r"(?:longa\s+duracao|operacional|de\s+viatura|de\s+equipamento)|ald)",
                      r"locacao\s+financeira\s+(?:mobiliaria|imobiliaria)")
_V["leases.document"] = (r"fatura", r"atcud", r"nota\s+de\s+credito")  # an invoice's own heading, not a contract
_V["leases.renting"] = (r"aluguer", r"ald")
_V["leases.article"] = (r"o\s+",)
_V["leases.lessor"] = (r"locador(?:a)?", r"entidade\s+locadora", r"financiador(?:a)?")
_V["leases.customer"] = (r"locatari[oa]", r"cliente")
_V["leases.contract"] = (r"contrato",)  # before its number (any case)
_V["leases.asset"] = (r"bem\s+locado", r"bem\s+alugado", r"bem", r"equipamento", r"viatura", r"veiculo", r"objeto",
                      r"objecto")
_V["leases.plate"] = (r"matricula",)  # a vehicle's registration (folded)
_V["leases.plate_label"] = (r"matr[ií]cula",)  # the same, as printed (any case)
_V["leases.plate_word"] = ("Matrícula",)  # plain: how a vehicle's plate is named in the asset's text
_V["leases.start"] = (r"data\s+de\s+inicio", r"inicio(?:\s+do\s+contrato)?", r"data\s+da\s+primeira\s+renda",
                      r"primeira\s+renda", r"1\.?\s*[ªa]?\s*renda", r"vencimento\s+da\s+primeira\s+renda")
_V["leases.term"] = (r"prazo", r"duracao", r"periodo", r"n\.?\s*[ºo]?\s*de\s+rendas", r"numero\s+de\s+rendas")
_V["leases.term_unit"] = (r"meses", r"rendas", r"prestacoes")
_V["leases.term_alone"] = (r"rendas\s+mensais", r"prestacoes\s+mensais", r"meses")
_V["leases.instalment"] = (r"renda", r"rendas", r"prestacao", r"mensalidade")
_V["leases.not_instalment"] = (r"numero\s+de", r"n\.?\s*[ºo]?\s*de", r"primeira", r"inicial", r"vencimento", r"data",
                               r"dia\s+de", r"caucao", r"entrada")
_V["leases.gross"] = (r"com\s+iva", r"c/\s*iva", r"iva\s+incluido", r"incluindo\s+iva")
_V["leases.net"] = (r"sem\s+iva", r"s/\s*iva", r"excluindo\s+iva", r"\+\s*iva", r"acresce\s+iva", r"antes\s+de\s+iva")
_V["leases.vat"] = (r"iva",)
_V["leases.monthly"] = (r"renda", r"prestacao", r"mensal")  # searched anywhere in a line
_V["leases.plus_vat"] = (r"\+\s*iva", r"acresce")
_V["leases.plus_vat_final"] = (r"acresce\s+iva", r"\+\s*iva")
_V["leases.residual"] = (r"valor\s+residual", r"opcao\s+de\s+compra")

# backoffice.deposits: deposits, staged payments and amounts held back.
_V["deposits.deposit"] = (r"sinal", r"adiantamentos?", r"adiant", r"reserva", r"provisao(?:\s+de\s+fundos)?",
                          r"pagamento\s+antecipado", r"pago\s+antecipadamente")
_V["deposits.deposit_out"] = (r"sinal", r"adiantamentos?", r"adiant", r"pagamento\s+antecipado",
                              r"pago\s+antecipadamente")
_V["deposits.security"] = (r"caucao", r"caucoes", r"caucionamento", r"deposito\s+(?:de\s+)?(?:garantia|caucao)",
                           r"depositos\s+de\s+garantia", r"garantia\s+(?:de\s+)?aluguer")
_V["deposits.giving_back"] = (r"devolucao", r"devol", r"devolvida", r"devolvido", r"restituicao", r"reembolso")
_V["deposits.cash_in"] = (r"numerario", r"deposito\s+(?:em\s+)?(?:numerario|dinheiro|cheque)")
_V["deposits.reference"] = (r"orc(?:amento)?", r"proposta", r"contrato")  # "ORC 2026/14"
_V["deposits.reference_label:orcamento"] = ("ORC",)  # plain: how a reference word is shown
_V["deposits.held"] = (r"retencao", r"retencoes", r"valor\s+retido", r"retido", r"retida")
_V["deposits.guarantee"] = (r"garantia",)
_V["deposits.not_held"] = (r"na\s+fonte", r"fonte", r"irs", r"irc", r"imposto")  # withholding tax
_V["deposits.release"] = (r"libert\w*", r"devol\w*", r"reembols\w*", r"vence\w*", r"ate", r"a\s+pagar\s+em")
_V["deposits.deduction"] = (r"sinal", r"adiantamentos?", r"adiant", r"provisao(?:\s+de\s+fundos)?",
                            r"pagamento\s+antecipado")
_V["deposits.taken_off"] = (r"a\s+deduzir", r"deduzir", r"deduzido", r"deducao", r"menos", r"recebido", r"recebida",
                            r"pago", r"paga", r"regularizacao", r"regularizado", r"abatido", r"descontado")
_V["deposits.due"] = (r"(?:total|valor|montante|saldo)\s+a\s+pagar", r"a\s+pagar",
                      r"saldo(?:\s+(?:em\s+divida|final|remanescente))?", r"(?:valor|montante)\s+em\s+divida")
_V["deposits.advance_title"] = (r"(?:fatura|factura)(?:[\s-]+recibo)?\s+(?:de\s+)?(?:adiantamento|sinal)",)
_V["deposits.customer"] = (r"cliente", r"adquirente")  # "Cliente: ..." (any case)
_V["deposits.invoice_word"] = (r"fatura",)  # a line that starts with the document's own name

# backoffice.supplier_statements: a supplier's account statement.
_V["supplier_statements.title"] = (r"extrato\s+(?:de\s+)?conta[\s-]+corrente", r"extrato\s+de\s+cliente",
                                   r"conta[\s-]+corrente\s+(?:de\s+)?cliente")
_V["supplier_statements.opening"] = (r"saldo\s+(?:anterior|inicial|transitado|de\s+abertura)",)
_V["supplier_statements.closing"] = (
    r"saldo\s+(?:final|atual|actual|em\s+divida|devedor|a\s+pagar|em\s+aberto|em\s+\d)",
    r"total\s+em\s+(?:divida|aberto)", r"valor\s+em\s+divida")
_V["supplier_statements.credit_note"] = (r"nota\s+de\s+credito", r"devolucao")
_V["supplier_statements.debit_note"] = (r"nota\s+de\s+debito",)
_V["supplier_statements.invoice_receipt"] = (r"fa[c]?tura[\s-]+recibo",)
_V["supplier_statements.payment"] = (r"pagamento", r"pago", r"liquidacao", r"cobranca", r"debito\s+direto")
_V["supplier_statements.invoice"] = (r"fatura", r"fatura\s+simplificada")
_V["supplier_statements.period_from"] = (r"periodo", r"entre")
_V["supplier_statements.period_to"] = (r"a", r"ate", r"e")
_V["supplier_statements.as_of"] = (r"data\s+do\s+extrato", r"extrato\s+em", r"saldo\s+em", r"em\s+aberto\s+(?:a|em)")
# Document series (SAF-T codes and their usual variants) by kind, plain.
_V["supplier_statements.prefix:invoice"] = ("FT", "FTR", "FR", "FS")
_V["supplier_statements.prefix:credit_note"] = ("NC", "NCR")
_V["supplier_statements.prefix:debit_note"] = ("ND",)
_V["supplier_statements.prefix:payment"] = ("RG", "PG", "LQ", "PAG")
# A CSV export's headings by column (plain, folded).
_V["supplier_statements.column:date"] = ("data", "data doc", "data documento", "data mov", "data movimento",
                                         "data do documento", "data lancamento")
_V["supplier_statements.column:number"] = ("documento", "n documento", "no documento", "numero documento",
                                           "n fatura", "numero fatura")
_V["supplier_statements.column:description"] = ("descricao", "detalhe", "tipo", "tipo documento", "movimento")
_V["supplier_statements.column:debit"] = ("debitos", "a debito")
_V["supplier_statements.column:credit"] = ("creditos", "a credito")
_V["supplier_statements.column:balance"] = ("saldo acumulado", "saldo corrente")
_V["supplier_statements.column:amount"] = ("montante",)

# backoffice.imports: an order, a bill of lading (any case), customs and freight wording (plain).
_V["imports.order"] = (r"encomenda", r"nota\s+de\s+encomenda")
_V["imports.bill"] = (r"conhecimento\s+de\s+embarque",)
_V["imports.customs"] = ("declaracao aduaneira", "declaracao de importacao", "documento administrativo unico")
_V["imports.freight"] = ("frete", "transitario", "conhecimento de embarque", "desalfandegamento")
_V["imports.deposit"] = ("sinal", "adiantamento")
_V["imports.customs_word"] = ("alfandega", "aduaneira")  # next to an MRN, a customs document

# backoffice.purchases (plain) and backoffice.sensitivity (plain phrases, folded before use).
_V["purchases.equipment"] = ("maquina", "maquinas", "maquinaria", "equipamento", "equipamentos", "computador",
                             "computadores", "portatil", "portateis", "servidor", "servidores", "viatura", "viaturas",
                             "veiculo", "veiculos", "mobiliario")
_V["sensitivity.medical"] = (
    "receita medica", "prescricao medica", "numero de utente", "n.º de utente", "no de utente", "nome do utente",
    "utente n", "processo clinico", "relatorio medico", "historial clinico", "historia clinica", "dados de saude",
    "diagnostico", "atestado medico", "boletim de analises", "resultado de analises", "ficha clinica")
_V["sensitivity.legal"] = (
    "processo judicial", "processo n.º", "proc. n.º", "peticao inicial", "contestacao", "sentenca",
    "acordao", "citacao", "sigilo profissional", "segredo de justica", "mandatario judicial", "procuracao forense",
    "tribunal judicial", "tribunal da relacao", "juizo de", "acao judicial", "parecer juridico",
    "documento confidencial advogado")
_V["sensitivity.hr"] = (
    "recibo de vencimento", "recibo de salario", "folha de vencimento", "folha de salarios",
    "contrato de trabalho", "avaliacao de desempenho", "processo disciplinar", "baixa medica",
    "certificado de incapacidade", "rescisao do contrato de trabalho", "carta de despedimento")

# backoffice.verification.normalize: labels before a tax number (compact, upper case).
_V["normalize.tax_label"] = ("NUMERODECONTRIBUINTE", "CONTRIBUINTE", "NIPC")

# ===================================================================================== tills, receipts lists, payouts

# backoffice.tills: a Z report's title, the words before its number, the words of each line, and a till
# software's CSV headings by role (plain, at each of the core's places for them).
_V["tills.title"] = (r"relatorio\s+(?:z|de\s+fecho|diario\s+de\s+vendas)", r"fecho\s+(?:de\s+|do\s+)?(?:caixa|dia)")
_V["tills.number"] = (r"relatorio\s+z", r"fecho(?:\s+de\s+caixa)?")
_V["tills.other"] = (r"mb\s?way", r"outros")
_V["tills.card"] = (r"multibanco", r"mb", r"cartao", r"cartoes", r"tpa")
_V["tills.cash"] = (r"numerario", r"dinheiro")
_V["tills.total"] = (r"vendas\s+(?:brutas|totais)",)
_V["tills.skip"] = (r"imposto", r"troco", r"devolucoes", r"descontos?", r"anulacoes", r"fundo\s+(?:de\s+)?caixa")
_V["tills.date_suffix"] = (r"(?:do\s+)?(?:fecho|relatorio)",)
_V["tills.csv:date"], _V["tills.csv:date:2"] = ("data fecho", "data do fecho"), ("fecho",)
_V["tills.csv:number"] = ("relatorio", "n relatorio")
_V["tills.csv:cash"], _V["tills.csv:cash:2"] = ("numerario", "dinheiro"), ("total numerario",)
_V["tills.csv:card"], _V["tills.csv:card:2"] = ("multibanco", "mb", "cartao", "cartoes"), ("tpa",)
_V["tills.csv:card:3"] = ("total multibanco", "cartao multibanco")
_V["tills.csv:other"], _V["tills.csv:other:2"] = ("outros",), ("mb way", "mbway")
_V["tills.csv:total"], _V["tills.csv:total:2"] = ("total vendas", "vendas"), ("total geral",)
_V["tills.csv:tax_id"], _V["tills.csv:tax_id:2"] = ("contribuinte",), ("nipc",)

# backoffice.memberships: a receipts list's headings by role (plain, at each of the core's places for them), its
# school terms, cash, and a returned direct debit on a bank line (upper case).
_V["memberships.column:number"] = ("recibo", "n recibo", "no recibo", "n o recibo", "numero recibo",
                                   "numero do recibo")
_V["memberships.column:number:2"] = ("n documento", "documento")
_V["memberships.column:number:3"] = ("fatura recibo", "numero")
_V["memberships.column:date"] = ("data",)
_V["memberships.column:date:2"] = ("data emissao", "data de emissao")
_V["memberships.column:date:3"] = ("data pagamento", "data do pagamento")
_V["memberships.column:member"] = ("aluno", "aluna", "socio", "socia", "membro", "utente", "atleta", "cliente",
                                   "nome")
_V["memberships.column:payer"] = ("encarregado de educacao", "encarregado", "pagador", "pago por")
_V["memberships.column:payer:2"] = ("titular", "responsavel")
_V["memberships.column:tax_id"] = ("nif", "contribuinte", "nif cliente", "nif do cliente")
_V["memberships.column:period"] = ("referente a", "periodo", "mes", "mes de referencia", "mensalidade")
_V["memberships.column:period:2"] = ("referencia", "descricao")
_V["memberships.column:amount"] = ("valor", "montante")
_V["memberships.column:amount:2"] = ("valor pago",)
_V["memberships.column:method"] = ("forma de pagamento", "meio de pagamento", "pagamento", "metodo",
                                   "metodo de pagamento")
_V["memberships.till_columns"] = ("numerario", "multibanco", "cartao")  # a till report's headings (plain)
_V["memberships.term"] = (r"periodo", r"trimestre", r"semestre")  # "1.º período"
_V["memberships.term_lead"] = (r"periodo",)  # "período 1"
_V["memberships.cash"] = (r"numerario", r"dinheiro")
_V["memberships.returned"] = (r"DEVOLUCAO", r"DEVOL\.?", r"DEV\.?\s+(?:DD|DEB\w*|COBR\w*|SEPA)", r"DD\s+DEVOLVIDO",
                              r"DEBITO\s+(?:DIRECTO\s+|DIRETO\s+)?DEVOLVIDO", r"COBRANCA\s+DEVOLVIDA", r"ESTORNO")

# backoffice.settlements.parse: a card terminal's (SIBS / Multibanco, REDUNIQ, Comercia) export headings by role
# (plain, normalized, at each of the core's places for them), the headings that tell one, what a line's type
# says it is, and a totals line.
_V["settlements.column:provider"] = ("rede", "adquirente", "entidade", "processador")
_V["settlements.column:payout_id"] = ("no lote", "n lote", "numero lote", "numero do lote", "id lote", "lote",
                                      "id liquidacao", "referencia liquidacao")
_V["settlements.column:payout_date"] = ("data liquidacao", "data de liquidacao", "data valor", "data credito",
                                        "data de credito")
_V["settlements.column:currency"] = ("moeda",)
_V["settlements.column:type"] = ("tipo", "tipo movimento", "tipo operacao", "tipo de operacao")
_V["settlements.column:reference"] = ("id transacao", "referencia", "numero autorizacao", "codigo autorizacao",
                                      "autorizacao")
_V["settlements.column:on"] = ("data movimento", "data operacao", "data transacao", "data da transacao", "data")
_V["settlements.column:gross"] = ("montante bruto", "valor bruto", "montante", "valor", "valor transacao",
                                  "valor da transacao")
_V["settlements.column:fee"] = ("comissao", "comissoes", "taxa servico", "taxa de servico")
_V["settlements.column:refund"] = ("reembolsos",)
_V["settlements.column:adjustment"] = ("ajustes", "acertos")
_V["settlements.column:net"] = ("montante liquido", "valor liquido", "liquido")
_V["settlements.terminal"] = ("montante bruto", "valor bruto", "montante liquido", "valor liquido", "comissao",
                              "comissoes", "taxa de servico", "taxa servico", "data liquidacao", "data de liquidacao",
                              "lote", "no lote", "n lote", "numero lote", "numero do lote")
_V["settlements.kind:payout"] = ("liquidacao", "pagamento ao comerciante")
_V["settlements.kind:chargeback"] = ("contestacao", "retrocessao", "disputa")
_V["settlements.kind:refund"] = ("devolucao", "devolucoes", "estorno", "anulacao", "reembolso")
_V["settlements.kind:fee"] = ("comissao", "comissoes")
_V["settlements.kind:adjustment"] = ("ajuste", "ajustes", "acerto", "acertos", "correcao")
_V["settlements.kind:sale"] = ("venda", "vendas", "compra", "compras", "pagamento")
_V["settlements.total_row"] = ("totais",)

# ===================================================================================== bank lines

# backoffice.reconciliation (folded, upper case): paying off a credit card (plain phrases); cash taken out, moved
# to the cash box, paid in; the exchange rate's word (plain).
_V["bank.card_repayment"] = ("LIQUIDACAO CARTAO", "LIQ CARTAO", "LIQUIDACAO DE CARTAO", "PAGAMENTO CARTAO CREDITO",
                             "PAGAMENTO CARTAO DE CREDITO", "PAG CARTAO CREDITO")
_V["bank.fx_rate"] = ("TAXA",)
_V["cash.withdrawal"] = (r"LEVANTAMENTO", r"LEVANT\.?", r"LEV\.?\s*(?:MB|ATM|NUM\w*|MULTIBANCO)")
_V["cash.cash_box"] = (r"FUNDO\s+(?:DE\s+)?MANEIO", r"FUNDO\s+FIXO(?:\s+DE\s+CAIXA)?", r"CAIXA\s+PEQUENA",
                       r"REFORCO\s+(?:DE\s+|DO\s+|DA\s+)?(?:CAIXA|FUNDO)")
_V["cash.deposit"] = (r"DEP(?:OSITO|\.)?\s*(?:EM\s+)?(?:NUMERARIO|NUM\.?|DINHEIRO|NOTAS)",)
# backoffice.reconciliation.payouts: a provider's bank wording (plain, upper case).
_V["payouts.bank:card_terminal"] = ("TPA", "VENDAS TPA", "LIQ TPA", "LIQUIDACAO TPA", "TERMINAL PAGAMENTO",
                                    "TERMINAL DE PAGAMENTO", "VENDAS MULTIBANCO", "LIQ MULTIBANCO",
                                    "LIQUIDACAO MULTIBANCO", "MULTIBANCO TPA")
# backoffice.chargebacks: a chargeback's words (folded).
_V["chargebacks.said"] = (r"disputas?", r"contestacao", r"contestacoes", r"contestada", r"contestado", r"retrocessao")
_V["chargebacks.reversal"] = (r"estornos?", r"reversao")
_V["chargebacks.sale"] = (r"tpa", r"venda", r"vendas", r"adquirente")
_V["chargebacks.fee"] = (r"comissao", r"comissoes", r"encargos?", r"custos?", r"despesas?")
# backoffice.connectors.open_banking: before a card's last four digits (any case).
_V["open_banking.card"] = (r"\bcart[ãa]o\s*(?:n[º°.]?\s*)?",)
# backoffice.recharges: a bank line that pays costs back.
_V["recharges.pays_back"] = (r"reembols\w*", r"refatura\w*", r"despesas", r"meios", r"adiantamento")

# ===================================================================================== suppliers, names and places

# backoffice.reconciliation.suppliers (plain, upper case for bank descriptors) and the other name readers (plain,
# folded): noise before a merchant, small words, legal forms, invoice series typed next to a number, and the
# cities card descriptors append.
_V["suppliers.noise"] = ("PAG", "PAGAMENTO", "PAGAMENTOS", "PAGTO", "DIRETO", "CARTAO", "TPA", "MB", "MBWAY",
                         "ELETRONICA")
_V["suppliers.stop"] = ("DA", "DO", "DOS", "DAS")
_V["suppliers.legal_forms"] = ("LDA", "UNIPESSOAL", "UNIP", "SGPS", "EIRL")
_V["suppliers.filler"] = ("FT", "FR", "FS", "FATURA")
_V["suppliers.cities"] = ("LISBOA", "PORTO", "BRAGA", "COIMBRA", "FARO", "FUNCHAL", "AVEIRO", "SETUBAL", "CASCAIS",
                          "OEIRAS", "AMADORA", "SINTRA")
_V["duplicates.legal_forms"] = ("lda", "unipessoal")
_V["keys.legal_forms"] = ("lda", "unipessoal", "sgps")
_V["keys.legal_form_dotted"] = (r"L\.?d\.?a\.?",)  # "L.da" (any case)
_V["staff.legal_forms"] = ("lda", "unipessoal")
# backoffice.learning.entity: a postal code (its parts as groups: 1200-384) and words never part of an address.
_V["entity.postal_code"] = (r"(?<!\d)(\d{4})\s*-\s*(\d{3})(?!\d)",)
_V["entity.address_filler"] = ("o", "a", "da", "do", "das", "dos", "e")
# backoffice.cost_centers and backoffice.learning.cost_centers: money for a property's rent or stay, a cost bought
# for a client to pay back, and kinds of cost center that are a property (plain).
_V["cost_centers.rent"] = (r"renda", r"rendas", r"aluguer", r"arrendamento", r"alojamento", r"estadia", r"reserva",
                           r"hospede")
_V["cost_centers.for_the_client"] = (r"cliente\s+final", r"utilizador\s+final", r"em\s+nome\s+(?:de|do|da)",
                                     r"por\s+conta\s+(?:de|do|da)", r"a\s+refaturar", r"para\s+refaturar",
                                     r"refaturacao", r"reembols\w*", r"despesas?\s+(?:do|de)\s+cliente")
_V["cost_centers.property_kinds"] = ("imovel", "apartamento", "fracao", "moradia", "predio", "casa")
# backoffice.evidence_search: words never searched for (plain).
_V["search.common"] = ("para", "com", "sua", "seu", "dos", "das")

# backoffice.spending: the owner's words for a cost category (plain), and the words on invoices, bank lines and
# merchant names at each of the core's places for them (plain; Portuguese suppliers' names too).
_V["spending.asked:tax"] = ("impostos", "imposto", "financas", "seguranca social", "irs", "irc", "retencoes")
_V["spending.asked:bank_fees"] = ("comissoes",)
_V["spending.asked:payroll"] = ("ordenados", "salarios", "vencimentos")
_V["spending.asked:loan"] = ("emprestimo", "emprestimos")
_V["spending.asked:rent"] = ("renda", "rendas", "arrendamento", "aluguer")
_V["spending.asked:telecom"] = ("telemovel", "telecomunicacoes", "comunicacoes")
_V["spending.asked:energy"] = ("eletricidade", "electricidade", "energia", "luz", "agua")
_V["spending.asked:travel"] = ("viagens", "deslocacoes", "combustivel", "portagens")
_V["spending.asked:meals"] = ("refeicoes", "restaurantes", "almocos", "jantares")
_V["spending.asked:office"] = ("moveis", "mobiliario", "material de escritorio")
_V["spending.asked:insurance"] = ("seguro", "seguros")
_V["spending.evidence:rent"] = ("renda", "rendas")
_V["spending.evidence:rent:2"] = ("arrendamento", "aluguer", "senhorio")
_V["spending.evidence:telecom"] = ("comunicacoes", "telecomunicacoes")
_V["spending.evidence:telecom:2"] = ("telemovel", "servicos moveis", "fibra")
_V["spending.evidence:telecom:3"] = ("meo", "nos comunicacoes", "nowo")
_V["spending.evidence:energy"] = ("eletricidade", "electricidade")
_V["spending.evidence:energy:2"] = ("energia",)
_V["spending.evidence:energy:3"] = ("agua",)
_V["spending.evidence:energy:4"] = ("goldenergy", "epal")
_V["spending.evidence:software"] = ("licenca",)
_V["spending.evidence:travel"] = ("viagem", "viagens")
_V["spending.evidence:travel:2"] = ("tap air", "comboios", "via verde", "portagens", "combustivel")
_V["spending.evidence:travel:3"] = ("estacionamento",)
_V["spending.evidence:meals"] = ("restaurante",)
_V["spending.evidence:meals:2"] = ("pastelaria",)
_V["spending.evidence:meals:3"] = ("refeicao", "refeicoes")
_V["spending.evidence:meals:4"] = ("cervejaria", "tasca")
_V["spending.evidence:office"] = ("moveis e decoracao", "mobiliario")
_V["spending.evidence:office:2"] = ("estante", "secretaria", "cadeira", "material de escritorio", "papelaria")
_V["spending.evidence:office:3"] = ("worten",)
_V["spending.evidence:insurance"] = ("seguro", "seguros")
_V["spending.evidence:insurance:2"] = ("apolice", "fidelidade")
_V["spending.evidence:insurance:3"] = ("tranquilidade",)
_V["spending.vat"] = (r"iva",)  # a tax payment's line that names VAT

# ===================================================================================== emails, web pages and privacy

# backoffice.evidence (any case): an invoice's own words, a link's action, what is never a document, an invoice
# link's path; a sign-in form's user field, a verification step, a sign-in page; "the invoice I sent earlier";
# a screenshot's file name.
_V["email.invoice"] = (r"faturas?", r"fatura[s]?-recibo", r"extratos?", r"segunda via", r"2[ªa] via")
_V["email.action"] = (r"veja", r"visualize", r"descarregar", r"descarregue", r"baixar", r"baixe", r"aceder", r"aceda",
                      r"acessar", r"acesse", r"obter")
_V["email.not_a_document"] = (r"(cancelar|anular|remover) (a )?(sua )?subscri[çc][ãa]o", r"deixar de receber",
                              r"termos (de|e) ", r"condi[çc][õo]es gerais", r"vers[ãa]o (web|online)",
                              r"(no|em) (browser|navegador)", r"ajuda", r"suporte", r"contato", r"palavra-passe",
                              r"aplica[çc][ãa]o")
_V["email.invoice_path"] = (r"fatura", r"extrato")
_V["html.login_name"] = (r"utilizador", r"usuário")
_V["links.mfa"] = (r"c[oó]digo de (verifica[çc][ãa]o|seguran[çc]a|confirma[çc][ãa]o|acesso|autentica[çc][ãa]o)",
                   r"autentica[çc][ãa]o (de dois fatores|em dois passos|forte)", r"(introduza|insira) o c[oó]digo",
                   r"c[oó]digo (que )?envi[aá]mos")
_V["links.login"] = (r"iniciar sess[ãa]o", r"inicie sess[ãa]o", r"aceder [àa] (sua )?conta")
_V["retrieval.document"] = (r"faturas?", r"fatura-recibo")
_V["retrieval.sent"] = (r"enviei", r"envi[aá]mos", r"mandei", r"mand[aá]mos", r"reenviei", r"anexei", r"em anexo",
                        r"seguiu", r"segue")
_V["retrieval.earlier"] = (r"abaixo",)
_V["retrieval.see"] = (r"veja",)
_V["retrieval.reply_prefix"] = (r"res", r"enc")  # "Res:", "Enc:" (a reply, a forward)
_V["retrieval.quote_start"] = (r"em .{0,200} escreveu:",)
_V["share.screenshot"] = (r"captura de ecr[ãa]", r"captura de tela")

# backoffice.policy.privacy: redaction before an external AI. Full patterns, one per entry (a street address, its
# house number and floor, any case; a postal code, its "code" and "locality" groups; a phone number written
# nationally: 9 digits, mobiles 9.., landlines 2..), else alternatives (any case).
_V["privacy.phone_keyword"] = (r"tlm", r"telem\w*")
_V["privacy.tax_label"] = (r"nipc", r"contribuinte")
_V["privacy.national_phone"] = (r"^(?:[29]\d{2}[ .-]?\d{3}[ .-]?\d{3}|2\d[ .-]\d{3}[ .-]\d{2}[ .-]\d{2})$",)
_PRIVACY_WORD = r"[A-Za-zÀ-ÿ0-9][\wÀ-ÿ'.ºª-]*"
_V["privacy.street"] = (
    r"(?<![\w])(?:Rua|R\.|Avenida|Av\.|Avª|Travessa|Trav\.|Tv\.|Largo|Lg\.|Praça|Pç\.|"
    r"Praceta|Estrada|Estr\.|Alameda|Calçada|Beco|Rotunda|Urbanização|Urb\.|Bairro|"
    r"Quinta|Caminho)"
    rf"[ \t]+{_PRIVACY_WORD}(?:[ \t]+{_PRIVACY_WORD}){{0,7}}?"
    # house number, then an optional floor; neither may be the start of a
    # postal code such as "1200-820" (that belongs to the postal pattern)
    r",?[ \t]*(?:n\.?[ \t]?º|nº|n\.|no\.|número)?[ \t]*\d{1,5}[A-Za-z]?\b(?!-\d)"
    r"(?:,?[ \t]*\d{1,2}(?![\d-])[ \t]?(?:\.?[ºª°]|\.)?[ \t]*(?:andar|esq\.?|esquerdo|dto\.?|dt\.?|direito|"
    r"frente|frt\.?|[A-D]\b)?)?",)
_V["privacy.postal"] = (  # "1200-820 Lisboa"
    r"(?<![\w/-])(?P<code>\d{4}-\d{3})(?![\w-])"
    r"(?P<locality>[ \t]+[A-ZÀ-Ý][A-Za-zÀ-ÿ'.-]{2,}"
    r"(?:[ \t]+(?:de|do|da|dos|das)[ \t]+[A-ZÀ-Ý][A-Za-zÀ-ÿ'.-]+){0,2})?",)
_V["privacy.postal_label"] = (r"c[óo]digo\s+postal", r"c\.?\s?p\.?")
_V["privacy.doc_series"] = ("FT", "FR", "FS", "NC", "ND", "RC", "FA", "GT", "OR")  # document series (upper case)
_V["privacy.locality_not"] = (r"fatura",)

# ===================================================================================== invitations

# backoffice.invitations and the server: a client company's tax number, its digits only (a full-match pattern),
# what the owner is told when it is not one, and its name (plain).
_V["invitations.tax_id"] = (r"\d{9}",)
_V["invitations.tax_id_problem"] = ("A NIF has 9 digits. Check the company numbers.",)
_V["invitations.tax_id_name"] = ("NIF",)

VOCABULARY: Mapping[str, tuple[str, ...]] = MappingProxyType(_V)
