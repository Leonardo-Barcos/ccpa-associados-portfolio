"""Brazilian document validators (CPF, CNPJ, postal code, phone).

Written by hand rather than pulled from a library: the check-digit rules are a
dozen lines each and are worth reading, and a dependency here would be a
dependency in the critical path of every signup.
"""

import re

_NON_DIGITS = re.compile(r"\D")


def digits_only(value: str | None) -> str:
    return _NON_DIGITS.sub("", value or "")


def is_valid_cpf(cpf: str) -> bool:
    """CPF is the Brazilian individual taxpayer number: 11 digits, the last
    two being check digits computed from the first nine."""
    cpf = digits_only(cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for length in (9, 10):
        total = sum(int(cpf[i]) * (length + 1 - i) for i in range(length))
        check = (total * 10) % 11 % 10
        if check != int(cpf[length]):
            return False
    return True


_NON_ALPHANUMERIC = re.compile(r"[^0-9A-Z]")


def normalize_cnpj(value: str | None) -> str:
    """Strip the punctuation and upper-case the letters.

    Since July 2026 the Receita Federal issues alphanumeric CNPJs: the first
    twelve positions may be letters (A-Z), the two check digits stay numeric.
    Existing all-digit CNPJs remain valid, so both forms coexist for good.
    """
    return _NON_ALPHANUMERIC.sub("", (value or "").upper())


def is_valid_cnpj(cnpj: str) -> bool:
    """CNPJ is the Brazilian company registration number: 14 positions, the
    last two check digits computed modulo 11.

    Each of the first twelve characters counts as its ASCII code minus 48,
    which leaves 0-9 unchanged and makes A = 17, B = 18 ... Z = 42. That is
    the Receita's rule for the alphanumeric format, and for an all-digit CNPJ
    it is exactly the classic calculation.
    """
    cnpj = normalize_cnpj(cnpj)
    if len(cnpj) != 14 or not cnpj[12:].isdigit() or cnpj == cnpj[0] * 14:
        return False
    values = [ord(ch) - 48 for ch in cnpj]
    weights_first = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    weights_second = [6] + weights_first
    for weights, position in ((weights_first, 12), (weights_second, 13)):
        total = sum(values[i] * weights[i] for i in range(position))
        remainder = total % 11
        check = 0 if remainder < 2 else 11 - remainder
        if check != values[position]:
            return False
    return True


def is_valid_postal_code(code: str) -> bool:
    return len(digits_only(code)) == 8


def is_valid_phone(phone: str) -> bool:
    """Accepts landline (10 digits) or mobile (11), area code included."""
    return len(digits_only(phone)) in (10, 11)


def format_cpf(cpf: str) -> str:
    d = digits_only(cpf)
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}" if len(d) == 11 else cpf


def format_cnpj(cnpj: str) -> str:
    d = normalize_cnpj(cnpj)
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}" if len(d) == 14 else cnpj
