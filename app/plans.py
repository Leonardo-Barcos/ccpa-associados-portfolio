"""Membership plan catalogue.

Amounts are stored in CENTS on purpose: money in floats eventually produces a
rounding error, and this is the number the member is charged.
"""

from dataclasses import dataclass, field, replace

# The annual plan is twelve months for the price of eleven. Keeping it as a
# multiplier means the monthly price stays the single source of truth: change
# 44.90 and the annual plan follows, instead of drifting out of step.
ANNUAL_MONTHS = 11


def _brl(cents: int) -> str:
    """Brazilian formatting: thousands with '.', decimals with ','."""
    return f"{cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@dataclass(frozen=True)
class Plan:
    code: str
    name: str
    audience: str  # "PF" for individuals, "PJ" for companies
    amount_cents: int
    cycle: str = "MONTHLY"
    description: str = ""
    benefits: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_yearly(self) -> bool:
        return self.cycle == "YEARLY"

    @property
    def amount_brl(self) -> str:
        return _brl(self.amount_cents)

    @property
    def cycle_label(self) -> str:
        return "/ano" if self.is_yearly else "/mês"

    @property
    def charge_noun(self) -> str:
        """What the member is asked to pay, in the copy they read."""
        return "anuidade" if self.is_yearly else "mensalidade"

    @property
    def renewal_label(self) -> str:
        return "Renova todo ano" if self.is_yearly else "Renova todo mês"

    @property
    def monthly_equivalent_brl(self) -> str:
        """What the annual price works out to per month.

        The comparison the member actually makes is against the monthly plan,
        so the page does the division instead of leaving it to them.
        """
        return _brl(round(self.amount_cents / 12))

    @property
    def savings_brl(self) -> str:
        """How much the annual plan saves against paying twelve months."""
        monthly = round(self.amount_cents / ANNUAL_MONTHS)
        return _brl(monthly * 12 - self.amount_cents)


def _yearly(plan: Plan) -> Plan:
    return replace(
        plan,
        code=f"{plan.code}_ANUAL",
        amount_cents=plan.amount_cents * ANNUAL_MONTHS,
        cycle="YEARLY",
    )


# Os quatro benefícios definitivos do clube, nas palavras da diretoria. Quem
# tem direito a quê sai da própria audiência que cada texto declara:
#   Diagnóstico Criativo  -> "freelancers, pequenos negócios e profissionais
#                             independentes"        = Profissional e Empresa
#   Portfolio Review      -> "estudantes e profissionais"  = Estudante e Profissional
#   40% de desconto       -> "associados do CCPA"          = todos
#   Égua, Bora!           -> "estudantes ... agências e empresas" = todos
DIAGNOSTICO = (
    "Diagnóstico Criativo — análise do seu posicionamento, desafios e "
    "oportunidades no mercado criativo paraense"
)
PORTFOLIO = (
    "Portfolio Review — portfólio avaliado por profissionais premiados, "
    "atuantes no mercado nacional e internacional"
)
TODOS: tuple[str, ...] = (
    "Égua, Bora! — encontros e rodadas de networking do clube",
    "40% de desconto em coleções de camisas paraenses, com estampas "
    "exclusivas de artistas locais",
)

MONTHLY_PLANS: dict[str, Plan] = {
    "ESTUDANTE": Plan(
        code="ESTUDANTE",
        name="Estudante",
        audience="PF",
        amount_cents=2990,
        cycle="MONTHLY",
        description="Para estudantes de cursos da cadeia criativa.",
        benefits=(PORTFOLIO, *TODOS),
    ),
    "PROFISSIONAL": Plan(
        code="PROFISSIONAL",
        name="Profissional",
        audience="PF",
        amount_cents=4490,
        cycle="MONTHLY",
        description="Para profissionais atuantes na cadeia criativa.",
        benefits=(DIAGNOSTICO, PORTFOLIO, *TODOS),
    ),
    "EMPRESA": Plan(
        code="EMPRESA",
        name="Empresa",
        audience="PJ",
        amount_cents=6990,
        cycle="MONTHLY",
        description="Para empresas que querem entender e melhorar o próprio "
        "negócio junto com a rede criativa do Pará.",
        benefits=(DIAGNOSTICO, *TODOS),
    ),
}

# Monthly first, then the annual twin of each — so a listing keeps the tiers in
# the same order whichever cycle is on screen.
PLANS: dict[str, Plan] = {
    **MONTHLY_PLANS,
    **{plan.code: plan for plan in map(_yearly, MONTHLY_PLANS.values())},
}

CYCLES: tuple[tuple[str, str], ...] = (("MONTHLY", "Mensal"), ("YEARLY", "Anual"))

CHAPTERS: tuple[str, ...] = ("Belém", "Canaã dos Carajás", "Paragominas", "Santarém")

GENDERS: tuple[str, ...] = (
    "Feminino",
    "Masculino",
    "Não-binário",
    "Outro",
    "Prefiro não informar",
)

STATES: tuple[str, ...] = (
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
    "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
    "SP", "SE", "TO",
)  # fmt: skip


def plans_for(audience: str, cycle: str = "MONTHLY") -> list[Plan]:
    return [p for p in PLANS.values() if p.audience == audience and p.cycle == cycle]
