"""Identity copy for the membership pages.

Every piece of wording the association might want to change lives here rather
than inside a template, so a board member can ask for a new sentence and only
one file is touched — with no chance of breaking HTML.

The strings stay in Brazilian Portuguese, because that is what the people
filling in the form read.
"""

NAME = "Clube de Criativos do Pará"
SHORT_NAME = "CCPA"
SHORT_DESCRIPTION = (
    "Associação que valoriza, registra e fomenta a criatividade paraense, "
    "reunindo profissionais, estudantes e empresas de toda a cadeia criativa."
)

EMAIL = "criativosdopara@gmail.com"
SITE_URL = "https://www.ccpara.com.br"

SOCIAL_LINKS = [
    ("Instagram", "https://www.instagram.com/cc.para/"),
    ("LinkedIn", "https://www.linkedin.com/company/101630565/"),
    ("YouTube", "https://www.youtube.com/@clubedecriativosdopara/videos"),
]

# This copy of the project is a public portfolio piece, not the club's
# official site. The notice is shown on every page (signup, panel, e-mails),
# so nobody mistakes a running copy for the real thing.
PORTFOLIO_NOTICE = (
    "Projeto de portfólio inspirado no Clube de Criativos do Pará — não é o site oficial."
)
