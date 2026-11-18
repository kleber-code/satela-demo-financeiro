"""Regras de negocio do financeiro. Todo calculo monetario usa Decimal."""
import calendar
import csv
import hashlib
import io
import re
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (ZERO, CENT, Baixa, Categoria, Cobranca, ContaFinanceira, ItemExtrato, Lancamento,
                     MovimentoCaixa, PeriodoFechado, SessaoCaixa, Transferencia, periodo_fechado, q2)

MULTA_PCT = Decimal("2.00")        # multa por atraso (% unica)
JUROS_MES_PCT = Decimal("1.00")    # juros de mora (% ao mes, pro rata dia)


# ------------------------------------------------------------------ datas
def add_months(d, n):
    m = d.month - 1 + n
    ano = d.year + m // 12
    mes = m % 12 + 1
    dia = min(d.day, calendar.monthrange(ano, mes)[1])
    return date(ano, mes, dia)


def primeiro_dia(d):
    return d.replace(day=1)


def ultimo_dia(d):
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def meses_entre(ini, fim):
    """Lista de primeiros-dias de mes de ini ate fim."""
    res, cur = [], primeiro_dia(ini)
    while cur <= fim:
        res.append(cur)
        cur = add_months(cur, 1)
    return res


# ------------------------------------------------------------------ parcelamento
def parcelar(total, n):
    """Divide total em n parcelas em centavos; a soma e sempre igual ao total.
    O resto de centavos vai uma a uma para as primeiras parcelas."""
    total = q2(total)
    if n < 1:
        raise ValueError("n deve ser >= 1")
    centavos = int((total / CENT).to_integral_value())
    base, resto = divmod(centavos, n)
    return [Decimal(base + (1 if i < resto else 0)) * CENT for i in range(n)]


@transaction.atomic
def criar_lancamentos(*, tipo, descricao, categoria, valor, vencimento, parceiro=None, centro_custo=None,
                      conta=None, observacao="", anexo=None, usuario=None, modo="unico", quantidade=1):
    """modo: unico | parcelado (valor = total dividido) | recorrente (valor repetido todo mes)."""
    valor = q2(valor)
    quantidade = max(int(quantidade or 1), 1) if modo != "unico" else 1
    if modo == "parcelado":
        valores = parcelar(valor, quantidade)
    else:
        valores = [valor] * quantidade
    grupo = uuid.uuid4() if quantidade > 1 else None
    criados = []
    for i, v in enumerate(valores):
        venc = add_months(vencimento, i)
        l = Lancamento(
            tipo=tipo, descricao=descricao, categoria=categoria, valor=v, vencimento=venc,
            competencia=primeiro_dia(venc), parceiro=parceiro, centro_custo=centro_custo, conta=conta,
            observacao=observacao, parcela=i + 1, total_parcelas=quantidade, grupo=grupo,
            recorrente=(modo == "recorrente"), criado_por=usuario)
        if anexo and i == 0:
            l.anexo = anexo
        l.save()
        criados.append(l)
    return criados


# ------------------------------------------------------------------ encargos e baixa
def calcular_encargos(lancamento, data_pag, multa_pct=MULTA_PCT, juros_mes_pct=JUROS_MES_PCT, principal=None):
    """Multa fixa (%) + juros pro rata dia sobre o principal em atraso."""
    principal = q2(principal if principal is not None else lancamento.saldo_aberto)
    dias = (data_pag - lancamento.vencimento).days
    if dias <= 0 or principal <= 0:
        return ZERO, ZERO, max(dias, 0)
    multa = q2(principal * multa_pct / 100)
    juros = q2(principal * (juros_mes_pct / 100) / 30 * dias)
    return juros, multa, dias


def validar_data_operacao(conta, data):
    if data > timezone.localdate():
        raise ValidationError("A data não pode ser futura.")
    if periodo_fechado(data):
        raise ValidationError(f"O período {data:%m/%Y} está fechado.")
    if conta is not None and conta.tipo == ContaFinanceira.CAIXA and \
            SessaoCaixa.objects.filter(conta=conta, data=data, status=SessaoCaixa.FECHADO).exists():
        raise ValidationError(f"O caixa de {data:%d/%m/%Y} já foi fechado.")


@transaction.atomic
def baixar(lancamento, *, data, conta, valor_principal, juros=ZERO, multa=ZERO, desconto=ZERO,
           observacao="", usuario=None):
    lancamento = Lancamento.objects.select_for_update().get(pk=lancamento.pk)
    valor_principal, juros, multa, desconto = (q2(x) for x in (valor_principal, juros, multa, desconto))
    if lancamento.status not in Lancamento.ABERTOS:
        raise ValidationError("Este lançamento não está em aberto.")
    if valor_principal <= 0:
        raise ValidationError("Informe um valor de principal maior que zero.")
    if valor_principal > lancamento.saldo_aberto:
        raise ValidationError(f"Principal maior que o saldo em aberto (R$ {lancamento.saldo_aberto}).")
    if min(juros, multa, desconto) < 0:
        raise ValidationError("Juros, multa e desconto não podem ser negativos.")
    if desconto > valor_principal + juros + multa:
        raise ValidationError("Desconto maior que o valor a pagar.")
    validar_data_operacao(conta, data)
    b = Baixa.objects.create(lancamento=lancamento, data=data, conta=conta, valor_principal=valor_principal,
                             juros=juros, multa=multa, desconto=desconto, observacao=observacao, usuario=usuario)
    lancamento.valor_pago = q2(lancamento.valor_pago + valor_principal)
    if lancamento.saldo_aberto == 0:
        lancamento.status = Lancamento.PAGA
        lancamento.data_pagamento = data
    else:
        lancamento.status = Lancamento.PARCIAL
    lancamento.save()
    return b


@transaction.atomic
def estornar_baixa(baixa):
    l = Lancamento.objects.select_for_update().get(pk=baixa.lancamento_id)
    baixa.delete()  # valida periodo
    l.valor_pago = q2(l.valor_pago - baixa.valor_principal)
    l.status = Lancamento.ABERTA if l.valor_pago == 0 else Lancamento.PARCIAL
    l.data_pagamento = None
    l.save()
    return l


def cancelar_lancamento(l):
    if l.baixas.exists():
        raise ValidationError("Lançamento com baixas não pode ser cancelado; estorne as baixas.")
    l.status = Lancamento.CANCELADA
    l.save()


# ------------------------------------------------------------------ saldos
def soma_baixas(qs):
    a = qs.aggregate(p=Sum("valor_principal"), j=Sum("juros"), m=Sum("multa"), d=Sum("desconto"))
    p, j, m, d = (q2(a[k] or 0) for k in ("p", "j", "m", "d"))
    return {"principal": p, "juros": j, "multa": m, "desconto": d, "total": q2(p + j + m - d)}


def _sum(qs, campo="valor"):
    return q2(qs.aggregate(s=Sum(campo))["s"] or 0)


def saldo_conta(conta, ate=None):
    b = Baixa.objects.filter(conta=conta)
    t_in = Transferencia.objects.filter(destino=conta)
    t_out = Transferencia.objects.filter(origem=conta)
    mv = MovimentoCaixa.objects.filter(sessao__conta=conta, transferencia__isnull=True)
    if ate:
        b = b.filter(data__lte=ate)
        t_in, t_out = t_in.filter(data__lte=ate), t_out.filter(data__lte=ate)
        mv = mv.filter(sessao__data__lte=ate)
    rec = soma_baixas(b.filter(lancamento__tipo=Lancamento.RECEBER))["total"]
    pag = soma_baixas(b.filter(lancamento__tipo=Lancamento.PAGAR))["total"]
    sup = _sum(mv.filter(tipo=MovimentoCaixa.SUPRIMENTO))
    san = _sum(mv.filter(tipo=MovimentoCaixa.SANGRIA))
    return q2(conta.saldo_inicial + rec - pag + _sum(t_in) - _sum(t_out) + sup - san)


def saldo_total(contas=None, ate=None):
    contas = contas if contas is not None else ContaFinanceira.objects.filter(ativa=True)
    return q2(sum((saldo_conta(c, ate) for c in contas), ZERO))


# ------------------------------------------------------------------ transferencia
@transaction.atomic
def transferir(*, origem, destino, valor, data, descricao="", usuario=None):
    valor = q2(valor)
    if origem.pk == destino.pk:
        raise ValidationError("Origem e destino devem ser contas diferentes.")
    if valor <= 0:
        raise ValidationError("Valor deve ser maior que zero.")
    validar_data_operacao(origem, data)
    validar_data_operacao(destino, data)
    if origem.tipo == ContaFinanceira.CAIXA and saldo_conta(origem, data) < valor:
        raise ValidationError("Saldo insuficiente no caixa para esta saída.")
    return Transferencia.objects.create(origem=origem, destino=destino, valor=valor, data=data,
                                        descricao=descricao, usuario=usuario)


# ------------------------------------------------------------------ caixa
DENOMINACOES = [Decimal(x) for x in ("200", "100", "50", "20", "10", "5", "2", "1", "0.50", "0.25", "0.10", "0.05")]


def abrir_caixa(conta, data, usuario=None):
    if conta.tipo != ContaFinanceira.CAIXA:
        raise ValidationError("Apenas contas do tipo Caixa têm abertura/fechamento.")
    if periodo_fechado(data):
        raise ValidationError("Período fechado.")
    if SessaoCaixa.objects.filter(conta=conta, status=SessaoCaixa.ABERTO).exists():
        raise ValidationError("Já existe um caixa aberto para esta conta; feche-o antes.")
    if SessaoCaixa.objects.filter(conta=conta, data=data).exists():
        raise ValidationError("Já existe sessão de caixa para este dia.")
    abertura = saldo_conta(conta, data - timedelta(days=1))
    return SessaoCaixa.objects.create(conta=conta, data=data, valor_abertura=abertura, aberto_por=usuario)


def resumo_sessao(s):
    conta, d = s.conta, s.data
    b = Baixa.objects.filter(conta=conta, data=d)
    rec = soma_baixas(b.filter(lancamento__tipo=Lancamento.RECEBER))["total"]
    pag = soma_baixas(b.filter(lancamento__tipo=Lancamento.PAGAR))["total"]
    mv = s.movimentos.all()
    sup = _sum(mv.filter(tipo=MovimentoCaixa.SUPRIMENTO))
    san = _sum(mv.filter(tipo=MovimentoCaixa.SANGRIA))
    tr_in = _sum(Transferencia.objects.filter(destino=conta, data=d, movimento_caixa__isnull=True))
    tr_out = _sum(Transferencia.objects.filter(origem=conta, data=d, movimento_caixa__isnull=True))
    esperado = q2(s.valor_abertura + rec - pag + sup - san + tr_in - tr_out)
    if s.status == SessaoCaixa.FECHADO and s.valor_esperado is not None:
        esperado = s.valor_esperado
    return {"recebimentos": rec, "pagamentos": pag, "suprimentos": sup, "sangrias": san,
            "transf_entrada": tr_in, "transf_saida": tr_out, "esperado": esperado}


@transaction.atomic
def registrar_movimento_caixa(sessao, tipo, valor, motivo="", contraparte=None, usuario=None):
    sessao = SessaoCaixa.objects.select_for_update().get(pk=sessao.pk)
    valor = q2(valor)
    if sessao.status != SessaoCaixa.ABERTO:
        raise ValidationError("Caixa fechado.")
    if valor <= 0:
        raise ValidationError("Valor deve ser maior que zero.")
    if tipo == MovimentoCaixa.SANGRIA and valor > resumo_sessao(sessao)["esperado"]:
        raise ValidationError("A sangria é maior que o valor esperado em caixa.")
    tr = None
    if contraparte:
        if contraparte.pk == sessao.conta_id:
            raise ValidationError("Contraparte deve ser outra conta.")
        o, dst = (sessao.conta, contraparte) if tipo == MovimentoCaixa.SANGRIA else (contraparte, sessao.conta)
        tr = Transferencia.objects.create(origem=o, destino=dst, valor=valor, data=sessao.data,
                                          descricao=f"{tipo.capitalize()} de caixa", usuario=usuario)
    return MovimentoCaixa.objects.create(sessao=sessao, tipo=tipo, valor=valor, motivo=motivo,
                                         contraparte=contraparte, transferencia=tr, usuario=usuario)


@transaction.atomic
def fechar_caixa(sessao, *, contagem=None, valor_contado=None, observacao="", usuario=None):
    sessao = SessaoCaixa.objects.select_for_update().get(pk=sessao.pk)
    if sessao.status != SessaoCaixa.ABERTO:
        raise ValidationError("Este caixa já está fechado.")
    contagem = {k: int(v) for k, v in (contagem or {}).items() if v}
    if contagem:
        contado = q2(sum((Decimal(k) * v for k, v in contagem.items()), ZERO))
        if valor_contado not in (None, ""):
            contado = q2(contado + Decimal(valor_contado))
    elif valor_contado not in (None, ""):
        contado = q2(valor_contado)
    else:
        raise ValidationError("Informe a contagem de cédulas/moedas ou o valor contado.")
    if contado < 0:
        raise ValidationError("Valor contado inválido.")
    esperado = resumo_sessao(sessao)["esperado"]
    dif = q2(contado - esperado)
    if dif != 0 and not observacao.strip():
        raise ValidationError("Há diferença no fechamento: informe uma observação/justificativa.")
    sessao.status = SessaoCaixa.FECHADO
    sessao.valor_contado, sessao.valor_esperado, sessao.diferenca = contado, esperado, dif
    sessao.contagem, sessao.observacao = {k: v for k, v in contagem.items()}, observacao
    sessao.fechado_por, sessao.fechado_em = usuario, timezone.now()
    sessao.save()
    return sessao


# ------------------------------------------------------------------ periodos
def fechar_periodo(ano, mes, usuario=None):
    ref = date(ano, mes, 1)
    if ultimo_dia(ref) >= timezone.localdate():
        raise ValidationError("Só é possível fechar meses já encerrados.")
    if SessaoCaixa.objects.filter(data__year=ano, data__month=mes, status=SessaoCaixa.ABERTO).exists():
        raise ValidationError("Há caixa aberto neste mês.")
    p, criado = PeriodoFechado.objects.get_or_create(ano=ano, mes=mes, defaults={"fechado_por": usuario})
    if not criado:
        raise ValidationError("Período já fechado.")
    return p


# ------------------------------------------------------------------ relatorios
def _baixas_qs(ini=None, fim=None, conta=None, categoria=None):
    qs = Baixa.objects.select_related("lancamento")
    if ini:
        qs = qs.filter(data__gte=ini)
    if fim:
        qs = qs.filter(data__lte=fim)
    if conta:
        qs = qs.filter(conta=conta)
    if categoria:
        qs = qs.filter(lancamento__categoria=categoria)
    return qs


def fluxo_realizado(ini, fim, conta=None, categoria=None):
    """Entradas/saidas efetivas por mes (baixas), com saldo acumulado."""
    meses = meses_entre(ini, fim)
    ent = {m: ZERO for m in meses}
    sai = {m: ZERO for m in meses}
    for b in _baixas_qs(ini, fim, conta, categoria):
        m = primeiro_dia(b.data)
        if m not in ent:
            continue
        if b.lancamento.tipo == Lancamento.RECEBER:
            ent[m] += b.total
        else:
            sai[m] += b.total
    contas = [conta] if conta else None
    saldo = saldo_total(contas, ini - timedelta(days=1)) if not categoria else ZERO
    linhas = []
    for m in meses:
        saldo = q2(saldo + ent[m] - sai[m])
        linhas.append({"mes": m, "entradas": q2(ent[m]), "saidas": q2(sai[m]), "liquido": q2(ent[m] - sai[m]),
                       "saldo": saldo if not categoria else None})
    return linhas


def fluxo_projetado(dias=90, conta=None, incluir_inadimplentes=False, hoje=None):
    hoje = hoje or timezone.localdate()
    limite = hoje + timedelta(days=dias)
    saldo = saldo_total([conta] if conta else None, hoje)
    abertos = Lancamento.objects.filter(status__in=Lancamento.ABERTOS, vencimento__lte=limite)
    if conta:
        abertos = abertos.filter(conta=conta)
    n = (dias + 6) // 7
    sem = [{"ini": hoje + timedelta(days=7 * i), "fim": hoje + timedelta(days=7 * i + 6),
            "entradas": ZERO, "saidas": ZERO} for i in range(n)]
    for l in abertos:
        if l.vencimento < hoje:
            if l.tipo == Lancamento.RECEBER and not incluir_inadimplentes:
                continue
            idx = 0
        else:
            idx = min((l.vencimento - hoje).days // 7, n - 1)
        if l.tipo == Lancamento.RECEBER:
            sem[idx]["entradas"] += l.saldo_aberto
        else:
            sem[idx]["saidas"] += l.saldo_aberto
    acc, menor = saldo, None
    for s in sem:
        s["entradas"], s["saidas"] = q2(s["entradas"]), q2(s["saidas"])
        acc = q2(acc + s["entradas"] - s["saidas"])
        s["saldo"] = acc
        if menor is None or acc < menor["saldo"]:
            menor = s
    return {"saldo_inicial": saldo, "semanas": sem, "saldo_final": acc, "menor": menor,
            "total_entradas": q2(sum((s["entradas"] for s in sem), ZERO)),
            "total_saidas": q2(sum((s["saidas"] for s in sem), ZERO))}


def dre(ini, fim, centro=None):
    lanc = Lancamento.objects.exclude(status=Lancamento.CANCELADA).filter(
        competencia__gte=primeiro_dia(ini), competencia__lte=fim).select_related("categoria")
    if centro:
        lanc = lanc.filter(centro_custo=centro)
    por_grupo = defaultdict(lambda: defaultdict(lambda: ZERO))
    for l in lanc:
        por_grupo[l.categoria.grupo][l.categoria.nome] += l.valor
    bx = Baixa.objects.filter(data__gte=ini, data__lte=fim).select_related("lancamento")
    if centro:
        bx = bx.filter(lancamento__centro_custo=centro)
    for b in bx:
        enc = q2(b.juros + b.multa)
        if b.lancamento.tipo == Lancamento.RECEBER:
            if enc:
                por_grupo["receita_financeira"]["Juros e multas recebidos"] += enc
            if b.desconto:
                por_grupo["deducao"]["Descontos concedidos"] += b.desconto
        else:
            if enc:
                por_grupo["despesa_financeira"]["Juros e multas pagos"] += enc
            if b.desconto:
                por_grupo["receita_financeira"]["Descontos obtidos"] += b.desconto

    def tot(g):
        return q2(sum(por_grupo[g].values(), ZERO))

    def det(g):
        return sorted(((k, q2(v)) for k, v in por_grupo[g].items()), key=lambda x: -x[1])

    rb, ded, cus = tot("receita_operacional"), tot("deducao"), tot("custo")
    rl = rb - ded
    lb = rl - cus
    dop = tot("despesa_operacional")
    ro = lb - dop
    rf, df = tot("receita_financeira"), tot("despesa_financeira")
    ll = ro + rf - df

    def pct(v):
        return (q2(v / rb * 100) if rb else None)
    linhas = [
        ("Receita operacional bruta", rb, "linha", det("receita_operacional")),
        ("(−) Deduções e impostos sobre a receita", -ded, "linha", det("deducao")),
        ("(=) Receita líquida", rl, "subtotal", []),
        ("(−) Custos dos serviços prestados", -cus, "linha", det("custo")),
        ("(=) Lucro bruto", lb, "subtotal", []),
        ("(−) Despesas operacionais", -dop, "linha", det("despesa_operacional")),
        ("(=) Resultado operacional", ro, "subtotal", []),
        ("(+) Receitas financeiras", rf, "linha", det("receita_financeira")),
        ("(−) Despesas financeiras", -df, "linha", det("despesa_financeira")),
        ("(=) Resultado líquido do período", ll, "total", []),
    ]
    return {"linhas": [{"rotulo": a, "valor": q2(b), "tipo": c, "detalhes": d, "pct": pct(b)}
                       for a, b, c, d in linhas],
            "receita_bruta": rb, "lucro_bruto": lb, "resultado_operacional": ro, "resultado_liquido": ll,
            "margem_liquida": pct(ll)}


FAIXAS = [("a_vencer", "A vencer"), ("1_30", "1 a 30 dias"), ("31_60", "31 a 60 dias"), ("60_mais", "Mais de 60 dias")]


def faixa_atraso(venc, hoje):
    d = (hoje - venc).days
    if d <= 0:
        return "a_vencer"
    if d <= 30:
        return "1_30"
    if d <= 60:
        return "31_60"
    return "60_mais"


def aging(tipo, hoje=None):
    hoje = hoje or timezone.localdate()
    tot = {k: ZERO for k, _ in FAIXAS}
    qt = {k: 0 for k, _ in FAIXAS}
    for l in Lancamento.objects.filter(tipo=tipo, status__in=Lancamento.ABERTOS):
        f = faixa_atraso(l.vencimento, hoje)
        tot[f] += l.saldo_aberto
        qt[f] += 1
    total = q2(sum(tot.values(), ZERO))
    return {"faixas": [{"chave": k, "rotulo": r, "valor": q2(tot[k]), "qtd": qt[k],
                        "pct": (q2(tot[k] / total * 100) if total else ZERO)} for k, r in FAIXAS],
            "total": total, "vencido": q2(total - tot["a_vencer"])}


def inadimplencia(hoje=None):
    hoje = hoje or timezone.localdate()
    ag = aging(Lancamento.RECEBER, hoje)
    fat = q2(Lancamento.objects.filter(tipo=Lancamento.RECEBER, competencia__gt=add_months(primeiro_dia(hoje), -12),
                                       competencia__lte=hoje).exclude(status=Lancamento.CANCELADA)
             .aggregate(s=Sum("valor"))["s"] or 0)
    devedores = defaultdict(lambda: {"valor": ZERO, "qtd": 0, "max_dias": 0})
    for l in Lancamento.objects.filter(tipo=Lancamento.RECEBER, status__in=Lancamento.ABERTOS,
                                       vencimento__lt=hoje).select_related("parceiro"):
        d = devedores[l.parceiro.nome if l.parceiro else "(sem cliente)"]
        d["valor"] += l.saldo_aberto
        d["qtd"] += 1
        d["max_dias"] = max(d["max_dias"], l.dias_atraso)
    lista = sorted(({"nome": k, **v, "valor": q2(v["valor"])} for k, v in devedores.items()), key=lambda x: -x["valor"])
    return {"aging": ag, "vencido": ag["vencido"], "faturamento_12m": fat,
            "pct_carteira": (q2(ag["vencido"] / ag["total"] * 100) if ag["total"] else ZERO),
            "pct_faturamento": (q2(ag["vencido"] / fat * 100) if fat else ZERO), "devedores": lista}


def ranking(tipo, ini, fim, limite=10):
    qs = Lancamento.objects.filter(tipo=tipo, competencia__gte=primeiro_dia(ini), competencia__lte=fim) \
        .exclude(status=Lancamento.CANCELADA).select_related("parceiro")
    agg = defaultdict(lambda: {"total": ZERO, "pago": ZERO, "qtd": 0})
    for l in qs:
        a = agg[l.parceiro.nome if l.parceiro else "(sem parceiro)"]
        a["total"] += l.valor
        a["pago"] += l.valor_pago
        a["qtd"] += 1
    lista = sorted(({"nome": k, "total": q2(v["total"]), "pago": q2(v["pago"]), "aberto": q2(v["total"] - v["pago"]),
                     "qtd": v["qtd"]} for k, v in agg.items()), key=lambda x: -x["total"])
    soma = q2(sum((x["total"] for x in lista), ZERO))
    for x in lista:
        x["pct"] = q2(x["total"] / soma * 100) if soma else ZERO
    return lista[:limite], soma


def comparativo_mensal(fim=None, meses=12):
    fim = primeiro_dia(fim or timezone.localdate())
    ini = add_months(fim, -(meses - 1))
    rec = {m: ZERO for m in meses_entre(ini, fim)}
    des = dict(rec)
    for l in Lancamento.objects.exclude(status=Lancamento.CANCELADA).filter(
            competencia__gte=ini, competencia__lte=fim):
        (rec if l.tipo == Lancamento.RECEBER else des)[l.competencia] += l.valor
    cx = {r["mes"]: r for r in fluxo_realizado(ini, ultimo_dia(fim))}
    linhas, ant = [], None
    for m in rec:
        res = q2(rec[m] - des[m])
        var = None
        if ant is not None and ant["receitas"]:
            var = q2((rec[m] - ant["receitas"]) / ant["receitas"] * 100)
        l = {"mes": m, "receitas": q2(rec[m]), "despesas": q2(des[m]), "resultado": res, "var_receita": var,
             "caixa_entradas": cx[m]["entradas"], "caixa_saidas": cx[m]["saidas"]}
        linhas.append(l)
        ant = l
    return linhas


def despesas_por_categoria(ini, fim, conta=None, categoria=None, tipo=Lancamento.PAGAR):
    agg = defaultdict(lambda: ZERO)
    for b in _baixas_qs(ini, fim, conta, categoria).filter(lancamento__tipo=tipo).select_related("lancamento__categoria"):
        agg[b.lancamento.categoria.nome] += b.total
    return sorted(((k, q2(v)) for k, v in agg.items()), key=lambda x: -x[1])


# ------------------------------------------------------------------ cobranca simulada
def gerar_cobranca(lancamento, tipo, usuario=None):
    if lancamento.tipo != Lancamento.RECEBER:
        raise ValidationError("Cobrança só para contas a receber.")
    if lancamento.status not in Lancamento.ABERTOS:
        raise ValidationError("Lançamento não está em aberto.")
    valor = lancamento.saldo_aberto
    cent = int(valor * 100)
    if tipo == Cobranca.BOLETO:
        codigo = f"SIMUL.00000 {lancamento.pk:05d}.00000 {cent:010d}.0 0 SIMULACAO"
    else:
        h = hashlib.sha256(f"{lancamento.pk}|{cent}|{timezone.now():%Y%m%d%H%M%S}".encode()).hexdigest()[:20].upper()
        codigo = f"SIMULACAO-PIX-NAO-FUNCIONAL|{lancamento.pk}|{cent}|{h}"
    return Cobranca.objects.create(lancamento=lancamento, tipo=tipo, codigo=codigo, valor=valor, usuario=usuario)


def qr_simulado_svg(texto, n=25):
    """Desenha um padrao pseudo-QR deterministico. NAO e um QR code valido."""
    bits = bin(int(hashlib.sha256(texto.encode()).hexdigest(), 16))[2:].zfill(256)
    cel, partes = 8, []

    def finder(x0, y0):
        partes.append(f'<rect x="{x0*cel}" y="{y0*cel}" width="{7*cel}" height="{7*cel}" fill="currentColor"/>')
        partes.append(f'<rect x="{(x0+1)*cel}" y="{(y0+1)*cel}" width="{5*cel}" height="{5*cel}" fill="#fff"/>')
        partes.append(f'<rect x="{(x0+2)*cel}" y="{(y0+2)*cel}" width="{3*cel}" height="{3*cel}" fill="currentColor"/>')
    k = 0
    for y in range(n):
        for x in range(n):
            if (x < 8 and y < 8) or (x >= n - 8 and y < 8) or (x < 8 and y >= n - 8):
                continue
            if bits[k % 256] == "1":
                partes.append(f'<rect x="{x*cel}" y="{y*cel}" width="{cel}" height="{cel}" fill="currentColor"/>')
            k += 1
    finder(0, 0), finder(n - 7, 0), finder(0, n - 7)
    w = n * cel
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {w}" role="img" '
            f'aria-label="QR Pix de exemplo, simulação, não funcional" class="qr-sim">'
            f'<rect width="{w}" height="{w}" fill="#fff"/>{"".join(partes)}'
            f'<g transform="rotate(-30 {w/2} {w/2})"><rect x="0" y="{w/2-16}" width="{w}" height="32" fill="#b42318" opacity=".9"/>'
            f'<text x="{w/2}" y="{w/2+6}" text-anchor="middle" font-size="18" font-weight="700" fill="#fff" '
            f'font-family="sans-serif">SIMULAÇÃO</text></g></svg>')


# ------------------------------------------------------------------ conciliacao
def _num(s):
    s = str(s).strip().replace("R$", "").replace(" ", "")
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-") or s.endswith("-")
    s = s.strip("()-+")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    v = Decimal(s)
    return -v if neg else v


def _data(s):
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y", "%Y%m%d"):
        try:
            return __import__("datetime").datetime.strptime(s[:10] if fmt != "%Y%m%d" else s[:8], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"data inválida: {s!r}")


def parse_csv(texto):
    texto = texto.lstrip("﻿")
    amostra = texto[:2000]
    delim = ";" if amostra.count(";") >= amostra.count(",") else ","
    linhas = [l for l in csv.reader(io.StringIO(texto), delimiter=delim) if any(c.strip() for c in l)]
    if not linhas:
        raise ValueError("Arquivo vazio.")
    cab = [c.strip().lower() for c in linhas[0]]
    i_d = i_t = i_v = i_c = i_db = None
    try:
        _data(linhas[0][0])
        dados, i_d, i_t, i_v = linhas, 0, 1, 2
    except ValueError:
        dados = linhas[1:]
        for i, c in enumerate(cab):
            if i_d is None and c in ("data", "date", "dt"):
                i_d = i
            elif i_t is None and any(k in c for k in ("descri", "hist", "memo", "lanç", "lanc")):
                i_t = i
            elif i_v is None and c in ("valor", "amount", "value", "valor (r$)"):
                i_v = i
            elif c in ("crédito", "credito", "entrada"):
                i_c = i
            elif c in ("débito", "debito", "saída", "saida"):
                i_db = i
        if i_d is None or i_t is None or (i_v is None and i_c is None and i_db is None):
            raise ValueError("Cabeçalho não reconhecido. Use colunas: data, descricao, valor.")
    itens = []
    for n, l in enumerate(dados, start=2):
        try:
            if i_v is not None:
                v = _num(l[i_v])
            else:
                v = (_num(l[i_c]) if i_c is not None and l[i_c].strip() else ZERO) - \
                    (abs(_num(l[i_db])) if i_db is not None and l[i_db].strip() else ZERO)
            itens.append((_data(l[i_d]), l[i_t].strip()[:200], q2(v)))
        except (ValueError, IndexError, ArithmeticError) as e:
            raise ValueError(f"Linha {n}: {e}")
    return itens


def parse_ofx(texto):
    itens = []
    for bloco in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|$)", texto, flags=re.S | re.I):
        g = lambda tag: (re.search(rf"<{tag}>([^<\r\n]*)", bloco, re.I) or [None, ""])[1].strip()
        try:
            itens.append((_data(g("DTPOSTED")), (g("MEMO") or g("NAME"))[:200], q2(_num(g("TRNAMT")))))
        except (ValueError, ArithmeticError) as e:
            raise ValueError(f"OFX inválido: {e}")
    if not itens:
        raise ValueError("Nenhuma transação encontrada no OFX.")
    return itens


@transaction.atomic
def importar_extrato(conta, nome_arquivo, conteudo_bytes):
    try:
        texto = conteudo_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = conteudo_bytes.decode("latin-1")
    itens = parse_ofx(texto) if nome_arquivo.lower().endswith(".ofx") or "<STMTTRN>" in texto.upper() else parse_csv(texto)
    novos = ignorados = 0
    vistos = defaultdict(int)
    for d, desc, v in itens:
        base = f"{conta.pk}|{d}|{desc}|{v}"
        vistos[base] += 1
        h = hashlib.sha1(f"{base}|{vistos[base]}".encode()).hexdigest()
        _, criado = ItemExtrato.objects.get_or_create(conta=conta, hash=h,
                                                      defaults={"data": d, "descricao": desc, "valor": v})
        novos += criado
        ignorados += (not criado)
    return novos, ignorados


def sugerir_pareamento(item, janela=5):
    """Baixas da mesma conta com o mesmo valor (com sinal) e data proxima, ainda nao conciliadas."""
    usadas = set(ItemExtrato.objects.filter(baixa__isnull=False).values_list("baixa_id", flat=True))
    cands = []
    qs = Baixa.objects.filter(conta=item.conta, data__gte=item.data - timedelta(days=janela),
                              data__lte=item.data + timedelta(days=janela)).select_related("lancamento__parceiro")
    palavras = {p for p in re.findall(r"\w{4,}", item.descricao.lower())}
    for b in qs:
        if b.pk in usadas or b.valor_assinado != item.valor:
            continue
        score = 100 - 10 * abs((b.data - item.data).days)
        nome = (b.lancamento.parceiro.nome if b.lancamento.parceiro else "").lower()
        if palavras & set(re.findall(r"\w{4,}", nome + " " + b.lancamento.descricao.lower())):
            score += 20
        cands.append({"baixa": b, "score": score})
    return sorted(cands, key=lambda c: -c["score"])[:3]


def sugerir_lancamentos(item, janela=15):
    sinal = Lancamento.RECEBER if item.valor > 0 else Lancamento.PAGAR
    alvo = abs(item.valor)
    qs = Lancamento.objects.filter(tipo=sinal, status__in=Lancamento.ABERTOS,
                                   vencimento__gte=item.data - timedelta(days=janela),
                                   vencimento__lte=item.data + timedelta(days=janela)).select_related("parceiro")
    return [l for l in qs if l.saldo_aberto == alvo][:3]


def conciliar(item, baixa):
    if baixa.conta_id != item.conta_id:
        raise ValidationError("A baixa é de outra conta.")
    item.baixa, item.status = baixa, ItemExtrato.CONCILIADO
    item.save()
