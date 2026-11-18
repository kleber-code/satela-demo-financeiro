"""Graficos SVG gerados no servidor (sem bibliotecas externas)."""
import math

from django.utils.html import escape
from django.utils.safestring import mark_safe

from .formatos import brl, compacto


def _escala(minimo, maximo, ticks=4):
    if maximo <= minimo:
        maximo = minimo + 1
    bruto = (maximo - minimo) / ticks
    mag = 10 ** math.floor(math.log10(bruto))
    for m in (1, 2, 2.5, 5, 10):
        passo = m * mag
        if passo >= bruto:
            break
    lo = math.floor(minimo / passo) * passo
    hi = math.ceil(maximo / passo) * passo
    return lo, hi, passo


def barras(labels, series, desc, w=760, h=280):
    """series: [(nome, [valores], classe)]"""
    ml, mr, mt, mb = 56, 10, 12, 32
    vals = [float(v) for _, vs, _ in series for v in vs] or [0]
    lo, hi, passo = _escala(min(0, min(vals)), max(vals))
    pw, ph = w - ml - mr, h - mt - mb
    y = lambda v: mt + ph - (v - lo) / (hi - lo) * ph
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="{escape(desc)}">']
    t = lo
    while t <= hi + 1e-9:
        out.append(f'<line x1="{ml}" x2="{w-mr}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>'
                   f'<text x="{ml-6}" y="{y(t)+4:.1f}" text-anchor="end" class="eixo">{compacto(t)}</text>')
        t += passo
    n, k = len(labels), len(series)
    grupo = pw / max(n, 1)
    larg = max(min(grupo * 0.8 / k, 26), 3)
    for i, lab in enumerate(labels):
        x0 = ml + grupo * i + (grupo - larg * k) / 2
        for j, (nome, vs, cls) in enumerate(series):
            v = float(vs[i])
            top, base = y(max(v, 0)), y(min(v, 0))
            out.append(f'<rect x="{x0+j*larg:.1f}" y="{top:.1f}" width="{larg-1:.1f}" height="{max(base-top,0.5):.1f}" '
                       f'rx="2" class="{cls}"><title>{escape(lab)} · {escape(nome)}: {brl(vs[i])}</title></rect>')
        if n <= 14 or i % 2 == 0:
            out.append(f'<text x="{ml+grupo*i+grupo/2:.1f}" y="{h-10}" text-anchor="middle" class="eixo">{escape(lab)}</text>')
    out.append(f'<line x1="{ml}" x2="{w-mr}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="zero"/></svg>')
    return mark_safe("".join(out))


def linha(labels, valores, desc, w=760, h=280, classe="c1"):
    ml, mr, mt, mb = 56, 14, 12, 32
    vs = [float(v) for v in valores] or [0]
    lo, hi, passo = _escala(min(0, min(vs)), max(vs))
    pw, ph = w - ml - mr, h - mt - mb
    y = lambda v: mt + ph - (v - lo) / (hi - lo) * ph
    n = len(vs)
    x = lambda i: ml + (pw * i / max(n - 1, 1))
    out = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="{escape(desc)}">']
    t = lo
    while t <= hi + 1e-9:
        out.append(f'<line x1="{ml}" x2="{w-mr}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>'
                   f'<text x="{ml-6}" y="{y(t)+4:.1f}" text-anchor="end" class="eixo">{compacto(t)}</text>')
        t += passo
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vs))
    out.append(f'<polygon points="{x(0):.1f},{y(0):.1f} {pts} {x(n-1):.1f},{y(0):.1f}" class="area {classe}"/>')
    out.append(f'<polyline points="{pts}" class="linha {classe}"/>')
    for i, v in enumerate(vs):
        out.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3.5" class="ponto {classe}">'
                   f'<title>{escape(labels[i])}: {brl(valores[i])}</title></circle>')
        if n <= 14 or i % 2 == 0:
            out.append(f'<text x="{x(i):.1f}" y="{h-10}" text-anchor="middle" class="eixo">{escape(labels[i])}</text>')
    out.append(f'<line x1="{ml}" x2="{w-mr}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="zero"/></svg>')
    return mark_safe("".join(out))


def rosca(itens, desc, tamanho=200):
    """itens: [(nome, valor)] -> (svg, legenda[(nome, valor, pct, classe)])"""
    total = float(sum(v for _, v in itens)) or 1
    r, c = 70, 100
    circ = 2 * math.pi * r
    out = [f'<svg viewBox="0 0 200 200" width="{tamanho}" height="{tamanho}" class="rosca" role="img" '
           f'aria-label="{escape(desc)}"><circle cx="{c}" cy="{c}" r="{r}" class="rosca-fundo"/>']
    acc, leg = 0.0, []
    for i, (nome, v) in enumerate(itens):
        fr = float(v) / total
        cls = f"s{i % 6 + 1}"
        out.append(f'<circle cx="{c}" cy="{c}" r="{r}" class="fatia {cls}" stroke-dasharray="{fr*circ:.2f} {circ:.2f}" '
                   f'stroke-dashoffset="{-acc*circ:.2f}" transform="rotate(-90 {c} {c})"><title>{escape(nome)}: {brl(v)}</title></circle>')
        acc += fr
        leg.append((nome, v, round(fr * 100, 1), cls))
    out.append(f'<text x="{c}" y="{c-2}" text-anchor="middle" class="rosca-t">Total</text>'
               f'<text x="{c}" y="{c+16}" text-anchor="middle" class="rosca-v">{compacto(total)}</text></svg>')
    return mark_safe("".join(out)), leg
