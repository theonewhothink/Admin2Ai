"""What Portuguese letters say (§24 for Portugal): the Portugal pack's obligation wording.

Folded phrases (lower case, no accents) by category, read with the core's English by
:mod:`backoffice.closure.obligations` whenever a business has a Portuguese company: the Autoridade
Tributária and the Segurança Social, "data limite de pagamento", "prazo", "total a pagar", "renovação",
"comprovativo de entrega", the grant agencies (IFAP, Portugal 2030, ...), the consequences a letter names
("coima", "juros de mora"), the month names and the words of relative deadlines and payment references.
Unverified letter conventions (verified_as_of: never); extend as letters are seen.

Categories besides the core's (see ``closure.obligations``): ``consequence:<label>`` (what the letter says
may happen, by the core's English label), ``agency:<folded name>`` (a grant agency's name for the owner),
``month:<n>`` and ``month_abbr:<n>`` (month names), ``month_ambiguous`` (a month name that is also an ordinary
word), ``relative_lead`` and ``relative_days`` ("no prazo de 15 dias úteis") and ``reference_label``
("referência para pagamento: 123 456 789").
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = ["VOCABULARY"]

# Agencies that run grants and subsidies, as their letters name them (folded), with their names for the owner.
_GRANT_AGENCIES: dict[str, str] = {
    "ifap": "IFAP", "instituto de financiamento da agricultura e pescas": "IFAP", "pepac": "PEPAC",
    "portugal 2030": "Portugal 2030", "pt2030": "Portugal 2030", "compete 2030": "COMPETE 2030",
    "norte 2030": "Norte 2030", "centro 2030": "Centro 2030", "lisboa 2030": "Lisboa 2030",
    "alentejo 2030": "Alentejo 2030", "algarve 2030": "Algarve 2030", "balcao dos fundos": "Balcão dos Fundos",
    "recuperar portugal": "Recuperar Portugal", "iapmei": "IAPMEI", "fundo ambiental": "Fundo Ambiental",
}  # fmt: skip

_GRANT_CORE = ("candidatura", "candidaturas", "apoio", "apoios", "incentivo", "incentivos", "comparticipacao")

_MONTHS = ("janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
           "novembro", "dezembro")
_ABBREVIATIONS = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6, "jul": 7, "ago": 8, "set": 9,
                  "out": 10, "nov": 11, "dez": 12}

VOCABULARY: Mapping[str, tuple[str, ...]] = MappingProxyType({
    # Who wrote it.
    "issuer:tax_authority": ("autoridade tributaria", "autoridade tributaria e aduaneira", "portal das financas",
                             "servico de financas", "servicos de financas", "direcao de financas"),
    "issuer:social_security": ("seguranca social", "instituto da seguranca social", "igfss"),
    "issuer:bank": ("o seu banco", "o banco"),
    "issuer:landlord": ("senhorio", "arrendamento", "contrato de arrendamento"),
    "issuer:insurer": ("seguradora", "companhia de seguros", "apolice"),
    "issuer:municipality": ("camara municipal", "municipio de", "municipio do", "municipio da", "junta de freguesia"),
    # What it asks for.
    "kyc": ("conheca o seu cliente", "atualizacao de dados", "actualizacao de dados", "atualizar os seus dados",
            "atualize os seus dados", "actualizar os seus dados", "comprovativo de morada", "beneficiario efetivo",
            "beneficiario efectivo"),
    "payment": ("pagamento", "pagar", "a pagar", "valor a pagar", "total a pagar", "liquidar", "liquidacao",
                "contribuicoes", "contribuicao", "prestacao"),
    "filing": ("declaracao", "declaracao periodica", "entrega da declaracao", "declaracao anual", "modelo 22", "ies"),
    "request": ("notificacao", "notificado", "notificada", "pedido de", "esclarecimento", "esclarecimentos",
                "solicitamos", "apresentar", "documentos", "audiencia previa"),
    "debt": ("cobranca de divida", "cobranca de dividas", "recuperacao de credito", "recuperacao de creditos",
             "injuncao", "divida", "divida em atraso", "ultimo aviso", "aviso final", "penhora"),
    "renewal": ("renovacao", "renovar", "renova", "renove", "expira", "expiracao", "caducidade", "caduca"),
    "insurance": ("seguro", "apolice", "seguradora"),
    "license": ("licenca", "alvara", "certificacao", "certificado"),
    "rent": ("renda", "rendas", "senhorio", "arrendamento"),
    "bank_request": ("documentacao", "documentos em falta", "pedido de documentos", "pedido de informacao"),
    "payment_deadline": ("pagamento ate", "data limite de pagamento", "data limite para pagamento",
                         "data de vencimento", "vencimento:", "vence em", "vence a", "valor a pagar", "total a pagar"),
    # The municipal tourist tax (Lisbon, Porto, ... charge it per night; the operator declares and pays monthly).
    "tourist_tax": ("taxa turistica", "taxas turisticas", "taxa municipal turistica", "taxas municipais turisticas",
                    "taxa de dormida", "taxa de dormidas"),
    # Grants: words enough on their own, core words that need a second one, and what is never a grant.
    "grant_alone": ("subsidio", "subsidios", "subvencao", "subvencoes", "fundo perdido", "apoio financeiro",
                    "incentivo financeiro"),
    "grant_core": _GRANT_CORE,
    "grant_paired": (*_GRANT_CORE, "financiamento", "aprovada", "aprovado", "aprovacao", "beneficiario",
                     "investimento", "fundos"),
    "not_grant": ("subsidio de ferias", "subsidio de natal", "subsidio de refeicao", "subsidio de alimentacao",
                  "subsidio de desemprego", "subsidio de doenca", "subsidio de parentalidade", "subsidio de turno",
                  "subsidios de ferias", "subsidios de natal", "apoio ao cliente", "linha de apoio", "apoio tecnico"),
    "grant_documents": ("submeter", "submissao", "enviar", "entregar", "apresentar", "documentos", "documentacao",
                        "elementos em falta", "comprovativos", "pedido de pagamento", "termo de aceitacao"),
    "grant_paid": ("pagamento", "pago", "paga", "transferencia", "transferido", "transferida", "aprovada", "aprovado",
                   "aprovacao"),
    "grant_agency": tuple(_GRANT_AGENCIES),
    **{f"agency:{folded}": (name,) for folded, name in _GRANT_AGENCIES.items()},
    "auto_renew": ("renova automaticamente", "renovacao automatica", "renovado automaticamente", "tacitamente"),
    # Where the date and the amount are. "vencimento" alone also means "salary": only its unambiguous forms anchor.
    "strong_date": ("prazo", "prazo limite", "data limite", "data-limite", "pagamento ate", "ate ao dia", "ate dia",
                    "data de vencimento", "vencimento:", "vence em", "vence a", "data de renovacao", "expira em",
                    "expira a", "valido ate", "valida ate", "termina em"),
    "weak_date": ("ate", "antes de"),
    "strong_amount": ("total a pagar", "valor a pagar", "montante a pagar", "importancia a pagar", "quantia a pagar",
                      "valor em divida"),
    "weak_amount": ("montante", "valor", "importancia", "quantia", "renda", "premio"),
    # What the letter says may happen.
    "consequence:a fine": ("coima", "coimas", "multa", "multas", "penalidade", "penalizacao"),
    "consequence:interest": ("juros", "juros de mora"),
    "consequence:a late fee": ("taxa de atraso",),
    "consequence:suspension": ("suspensao", "suspender", "bloqueio", "bloquear"),
    "consequence:cancellation": ("cancelamento", "cancelar", "resolucao do contrato"),
    "consequence:legal action": ("execucao fiscal", "penhora", "tribunal", "acao judicial", "injuncao"),
    # Dates, relative deadlines ("no prazo de 15 dias") and payment references ("referência MB: 123 456 789").
    **{f"month:{i}": (name,) for i, name in enumerate(_MONTHS, start=1)},
    **{f"month_abbr:{i}": (abbr,) for abbr, i in _ABBREVIATIONS.items()},
    "month_ambiguous": ("marco",),  # "março" folded, also a first name
    "relative_lead": ("prazo de", "no prazo de"),
    "relative_days": ("dias uteis", "dias"),
    "reference_label": ("referencia", "referencia de pagamento", "referencia para pagamento",
                        "referencia multibanco", "referencia mb"),
    # Confirmations: a letter saying what was asked for was done (or is still missing).
    "still_asking": ("ainda precisamos", "ainda necessitamos", "continua em falta", "continuam em falta",
                     "documentos em falta", "documentacao em falta", "falta enviar", "faltam", "queira enviar",
                     "por favor envie", "solicitamos", "se nao pagar", "se nao efetuar", "se nao for paga",
                     "se nao for pago", "caso nao pague", "caso nao efetue"),
    "decided": ("contrato cessado", "cessacao do contrato", "cessacao do seu contrato", "contrato terminado",
                "contrato rescindido", "rescisao do contrato", "confirmamos a denuncia", "confirmamos a cessacao",
                "confirmamos o cancelamento", "cancelamento confirmado", "apolice anulada", "apolice cancelada",
                "nao sera renovado", "nao sera renovada"),
    "renewed": ("foi renovado", "foi renovada", "foram renovados", "renovado ate", "renovada ate",
                "renovacao efetuada", "renovacao efectuada", "renovacao concluida", "renovacao confirmada",
                "confirmamos a renovacao", "renovado com sucesso", "renovada com sucesso"),
    "submitted": ("comprovativo de entrega", "declaracao submetida", "declaracao entregue", "declaracao foi submetida",
                  "declaracao foi entregue", "declaracao recebida", "entregue com sucesso", "submetida com sucesso"),
    "answered": ("recebemos os seus documentos", "recebemos a sua documentacao", "recebemos a documentacao",
                 "recebemos os documentos", "documentos recebidos", "documentacao recebida", "recebemos a sua resposta",
                 "resposta recebida", "confirmamos a rececao", "confirmamos a recepcao", "pedido concluido",
                 "processo concluido", "processo encerrado", "dados atualizados", "dados actualizados",
                 "atualizacao concluida", "actualizacao concluida", "verificacao concluida"),
    "contract": ("contrato", "subscricao", "assinatura", "avenca"),
    "validity": ("ate", "valido ate", "valida ate", "validade", "nova validade", "nova data de validade", "expira em",
                 "expira a", "termina em", "renovado ate", "renovada ate"),
})  # fmt: skip
