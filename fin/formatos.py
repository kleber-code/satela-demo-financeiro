from decimal import Decimal

from .models import q2

MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def num_br(v, casas=2):
    if v is None:
        return "—"
    v = Decimal(v).quantize(Decimal(1).scaleb(-casas))
    s = f"{abs(v):,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if v < 0 else "") + s


def brl(v):
    if v is None:
        return "—"
    v = q2(v)
    return ("-R$ " if v < 0 else "R$ ") + num_br(abs(v))


def compacto(v):
    v = float(v)
    a = abs(v)
    if a >= 1_000_000:
        return f"{v/1_000_000:.1f} mi".replace(".", ",")
    if a >= 1_000:
        return f"{v/1_000:.0f} mil"
    return f"{v:.0f}"


def mes_br(d):
    return f"{MESES[d.month-1]}/{d.year % 100:02d}"
