from django import template

from .. import formatos

register = template.Library()


@register.filter
def brl(v):
    return formatos.brl(v)


@register.filter
def num(v):
    return formatos.num_br(v)


@register.filter
def pct(v):
    return "—" if v is None else formatos.num_br(v, 1) + "%"


@register.filter
def mes(d):
    return formatos.mes_br(d)


@register.filter
def sinal_cls(v):
    return "pos" if v and v > 0 else ("neg" if v and v < 0 else "")


@register.filter
def abs_(v):
    return abs(v)


@register.simple_tag(takes_context=True)
def qs(context, **kw):
    """Querystring atual com substituicoes (usada na paginacao)."""
    d = context["request"].GET.copy()
    for k, v in kw.items():
        if v in (None, ""):
            d.pop(k, None)
        else:
            d[k] = v
    return "?" + d.urlencode() if d else "?"
