import csv
from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import charts, services
from .formatos import brl, mes_br, num_br
from .forms import (AbrirCaixaForm, BaixaForm, CategoriaForm, CentroForm, ContaForm, FecharCaixaForm,
                    FiltroLancamentos, ImportarExtratoForm, LancamentoForm, MovimentoCaixaForm, ParceiroForm,
                    PeriodoRelatorioForm, TransferenciaForm)
from .models import (AuditLog, Baixa, Categoria, CentroCusto, Cobranca, ContaFinanceira, ItemExtrato, Lancamento,
                     MovimentoCaixa, Parceiro, PeriodoFechado, Perfil, SessaoCaixa, Transferencia, papel_de, q2)
from .services import add_months, primeiro_dia, ultimo_dia

User = get_user_model()


# ------------------------------------------------------------------ utilitarios
def escrita(view):
    @wraps(view)
    def w(request, *a, **kw):
        if papel_de(request.user) not in (Perfil.ADMIN, Perfil.FINANCEIRO):
            return render(request, "fin/403.html", status=403)
        return view(request, *a, **kw)
    return w


def somente_admin(view):
    @wraps(view)
    def w(request, *a, **kw):
        if papel_de(request.user) != Perfil.ADMIN:
            return render(request, "fin/403.html", status=403)
        return view(request, *a, **kw)
    return w


def paginar(request, qs, por=20):
    return Paginator(qs, por).get_page(request.GET.get("pagina"))


def erros(request, e):
    for m in (e.messages if hasattr(e, "messages") else [str(e)]):
        messages.error(request, m)


def csv_response(nome, cab, linhas):
    r = HttpResponse(content_type="text/csv; charset=utf-8")
    r["Content-Disposition"] = f'attachment; filename="{nome}-{timezone.localdate():%Y%m%d}.csv"'
    r.write("﻿")
    w = csv.writer(r, delimiter=";")
    w.writerow(cab)
    for l in linhas:
        w.writerow(l)
    return r


def filtros_periodo(request, meses_padrao=12):
    hoje = timezone.localdate()
    form = PeriodoRelatorioForm(request.GET or None)
    ini = add_months(primeiro_dia(hoje), -(meses_padrao - 1))
    fim = hoje
    conta = categoria = None
    if form.is_valid():
        ini = form.cleaned_data["de"] or ini
        fim = form.cleaned_data["ate"] or fim
        conta, categoria = form.cleaned_data["conta"], form.cleaned_data["categoria"]
    if ini > fim:
        ini, fim = fim, ini
    return form, ini, fim, conta, categoria


def saude(request):
    return HttpResponse("ok", content_type="text/plain")


# ------------------------------------------------------------------ acesso
def _ip(request):
    return request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "")).split(",")[0].strip()


def entrar(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    chave = f"login:{_ip(request)}"
    if request.method == "POST":
        if cache.get(chave, 0) >= settings.LOGIN_MAX_TENTATIVAS:
            messages.error(request, "Muitas tentativas. Aguarde alguns minutos e tente novamente.")
        else:
            u = authenticate(request, username=request.POST.get("usuario", "").strip(),
                             password=request.POST.get("senha", ""))
            if u:
                cache.delete(chave)
                login(request, u)
                prox = request.GET.get("next", "")
                return redirect(prox if prox.startswith("/") and not prox.startswith("//") else "dashboard")
            cache.set(chave, cache.get(chave, 0) + 1, settings.LOGIN_JANELA_SEGUNDOS)
            messages.error(request, "Usuário ou senha inválidos.")
    return render(request, "fin/entrar.html")


@require_POST
def entrar_demo(request):
    if not settings.DEMO_MODE:
        raise Http404()
    alvo = "leitura" if request.POST.get("perfil") == "leitura" else "financeiro"
    u = User.objects.filter(username=alvo, is_superuser=False).first()
    if not u:
        messages.error(request, "Usuário de demonstração não encontrado. Rode: python manage.py seed_demo")
        return redirect("entrar")
    login(request, u, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("dashboard")


@require_POST
def sair(request):
    logout(request)
    return redirect("entrar")


# ------------------------------------------------------------------ dashboard
def dashboard(request):
    hoje = timezone.localdate()
    form, ini, fim, conta, categoria = filtros_periodo(request)
    contas = [conta] if conta else None
    saldo = services.saldo_total(contas)
    ini_mes, mes_ant = primeiro_dia(hoje), add_months(primeiro_dia(hoje), -1)
    lanc = Lancamento.objects.filter(status__in=Lancamento.ABERTOS)
    if conta:
        lanc = lanc.filter(conta=conta)
    if categoria:
        lanc = lanc.filter(categoria=categoria)
    lim30 = hoje + timedelta(days=30)
    a_receber = q2(sum((l.saldo_aberto for l in lanc.filter(tipo="R", vencimento__gte=hoje, vencimento__lte=lim30)), Decimal(0)))
    a_pagar = q2(sum((l.saldo_aberto for l in lanc.filter(tipo="P", vencimento__gte=hoje, vencimento__lte=lim30)), Decimal(0)))
    ina = services.inadimplencia(hoje)
    fluxo_mes = services.fluxo_realizado(ini_mes, hoje, conta, categoria)[0]
    fluxo_ant = services.fluxo_realizado(mes_ant, mes_ant + timedelta(days=hoje.day - 1), conta, categoria)[0]
    dre_mes = services.dre(ini_mes, hoje)
    var = None
    if fluxo_ant["entradas"]:
        var = q2((fluxo_mes["entradas"] - fluxo_ant["entradas"]) / fluxo_ant["entradas"] * 100)

    fluxo = services.fluxo_realizado(ini, fim, conta, categoria)
    graf_fluxo = charts.barras(
        [mes_br(l["mes"]) for l in fluxo],
        [("Entradas", [l["entradas"] for l in fluxo], "b-ent"), ("Saídas", [l["saidas"] for l in fluxo], "b-sai")],
        "Entradas e saídas realizadas por mês")
    proj = services.fluxo_projetado(90, conta)
    graf_proj = charts.linha([f"{s['ini']:%d/%m}" for s in proj["semanas"]], [s["saldo"] for s in proj["semanas"]],
                             "Saldo projetado para os próximos 90 dias")
    cats = services.despesas_por_categoria(ini, fim, conta, categoria)
    top = cats[:5]
    resto = sum((v for _, v in cats[5:]), Decimal(0))
    if resto:
        top.append(("Outras", q2(resto)))
    rosca, leg = charts.rosca(top, "Despesas pagas por categoria") if top else (None, [])
    top_clientes, _ = services.ranking("R", ini, fim, 5)
    ag = services.aging("R")
    ap_venc = services.aging("P")["vencido"]
    prox_r = Lancamento.objects.filter(tipo="R", status__in=Lancamento.ABERTOS, vencimento__gte=hoje).select_related("parceiro").order_by("vencimento")[:6]
    prox_p = Lancamento.objects.filter(tipo="P", status__in=Lancamento.ABERTOS, vencimento__gte=hoje).select_related("parceiro").order_by("vencimento")[:6]
    caixa_aberto = SessaoCaixa.objects.filter(status="aberto").select_related("conta").first()
    return render(request, "fin/dashboard.html", {
        "form": form, "saldo": saldo, "a_receber": a_receber, "a_pagar": a_pagar, "ina": ina, "fluxo_mes": fluxo_mes,
        "var_entradas": var, "dre_mes": dre_mes, "graf_fluxo": graf_fluxo, "graf_proj": graf_proj, "proj": proj,
        "rosca": rosca, "leg": leg, "top_clientes": top_clientes, "aging": ag, "prox_r": prox_r, "prox_p": prox_p,
        "caixa_aberto": caixa_aberto, "ap_venc": ap_venc, "lim30": lim30, "ini": ini, "fim": fim,
        "contas": ContaFinanceira.objects.filter(ativa=True), "hoje": hoje,
        "max_cliente": max((c["total"] for c in top_clientes), default=Decimal(1)),
        "contas_saldo": [(c, c.saldo()) for c in ContaFinanceira.objects.filter(ativa=True)],
    })


# ------------------------------------------------------------------ lancamentos
def lista_lancamentos(request, tipo):
    hoje = timezone.localdate()
    form = FiltroLancamentos(request.GET or None)
    qs = Lancamento.objects.filter(tipo=tipo).select_related("parceiro", "categoria", "conta")
    if form.is_valid():
        c = form.cleaned_data
        if c["q"]:
            qs = qs.filter(Q(descricao__icontains=c["q"]) | Q(parceiro__nome__icontains=c["q"]))
        s = c["situacao"]
        if s == "aberta":
            qs = qs.filter(status__in=Lancamento.ABERTOS)
        elif s == "vencida":
            qs = qs.filter(status__in=Lancamento.ABERTOS, vencimento__lt=hoje)
        elif s:
            qs = qs.filter(status=s)
        if c["categoria"]:
            qs = qs.filter(categoria=c["categoria"])
        if c["conta"]:
            qs = qs.filter(conta=c["conta"])
        if c["de"]:
            qs = qs.filter(vencimento__gte=c["de"])
        if c["ate"]:
            qs = qs.filter(vencimento__lte=c["ate"])
    elif not request.GET:
        qs = qs.filter(status__in=Lancamento.ABERTOS)
        form = FiltroLancamentos(initial={"situacao": "aberta"})
    qs = qs.order_by("vencimento", "id")
    if request.GET.get("formato") == "csv":
        return csv_response("receber" if tipo == "R" else "pagar",
                            ["Vencimento", "Descrição", "Parceiro", "Categoria", "Parcela", "Valor", "Pago", "Saldo", "Situação"],
                            [[l.vencimento.strftime("%d/%m/%Y"), l.descricao, l.parceiro or "", l.categoria.nome,
                              f"{l.parcela}/{l.total_parcelas}", num_br(l.valor), num_br(l.valor_pago),
                              num_br(l.saldo_aberto), l.situacao_label] for l in qs])
    from django.db.models import Sum
    tot = qs.aggregate(v=Sum("valor"), p=Sum("valor_pago"))
    pag = paginar(request, qs)
    return render(request, "fin/lancamentos.html", {
        "tipo": tipo, "form": form, "pag": pag, "total": q2(tot["v"] or 0), "total_pago": q2(tot["p"] or 0),
        "titulo": "Contas a receber" if tipo == "R" else "Contas a pagar", "hoje": hoje})


def receber(request):
    return lista_lancamentos(request, "R")


def pagar(request):
    return lista_lancamentos(request, "P")


@escrita
def lancamento_novo(request, tipo):
    if tipo not in ("R", "P"):
        raise Http404()
    form = LancamentoForm(request.POST or None, request.FILES or None, tipo=tipo)
    if request.method == "POST" and form.is_valid():
        c = form.cleaned_data
        try:
            criados = services.criar_lancamentos(
                tipo=tipo, descricao=c["descricao"], categoria=c["categoria"], valor=c["valor"],
                vencimento=c["vencimento"], parceiro=c["parceiro"], centro_custo=c["centro_custo"], conta=c["conta"],
                observacao=c["observacao"], anexo=c.get("anexo"), usuario=request.user,
                modo=c.get("modo") or "unico", quantidade=c.get("quantidade") or 1)
        except ValidationError as e:
            erros(request, e)
        else:
            messages.success(request, f"{len(criados)} lançamento(s) criado(s).")
            return redirect("lancamento", pk=criados[0].pk)
    return render(request, "fin/lancamento_form.html", {"form": form, "tipo": tipo, "novo": True,
                  "titulo": "Nova conta a receber" if tipo == "R" else "Nova conta a pagar"})


@escrita
def lancamento_editar(request, pk):
    l = get_object_or_404(Lancamento, pk=pk)
    form = LancamentoForm(request.POST or None, request.FILES or None, instance=l)
    if request.method == "POST" and form.is_valid():
        try:
            obj = form.save(commit=False)
            obj.competencia = obj.vencimento.replace(day=1)
            obj.save()
        except ValidationError as e:
            erros(request, e)
        else:
            messages.success(request, "Lançamento atualizado.")
            return redirect("lancamento", pk=l.pk)
    return render(request, "fin/lancamento_form.html", {"form": form, "tipo": l.tipo, "novo": False, "obj": l,
                  "titulo": f"Editar: {l}"})


def lancamento_detalhe(request, pk):
    l = get_object_or_404(Lancamento.objects.select_related("parceiro", "categoria", "centro_custo", "conta"), pk=pk)
    baixas = list(l.baixas.select_related("conta"))
    logs = AuditLog.objects.filter(
        Q(modelo="Lancamento", objeto_id=str(l.pk)) | Q(modelo="Baixa", objeto_id__in=[str(b.pk) for b in baixas]))[:30]
    irmaos = Lancamento.objects.filter(grupo=l.grupo).order_by("parcela") if l.grupo else []
    juros, multa, dias = services.calcular_encargos(l, timezone.localdate())
    return render(request, "fin/lancamento_detalhe.html", {
        "l": l, "baixas": baixas, "logs": logs, "irmaos": irmaos, "cobrancas": l.cobrancas.all()[:5],
        "juros_hoje": juros, "multa_hoje": multa})


@escrita
def lancamento_baixar(request, pk):
    l = get_object_or_404(Lancamento, pk=pk)
    if l.status not in Lancamento.ABERTOS:
        messages.error(request, "Este lançamento não está em aberto.")
        return redirect("lancamento", pk=pk)
    hoje = timezone.localdate()
    item = ItemExtrato.objects.filter(pk=request.GET.get("item") or request.POST.get("item") or 0).first()
    conta_padrao = l.conta or (item.conta if item else ContaFinanceira.objects.filter(ativa=True, tipo="banco").first())
    if request.method == "POST":
        dados = request.POST.copy()
        form = BaixaForm(dados)
        if "recalcular" in request.POST:
            try:
                d = form.fields["data"].clean(form.data.get("data"))
                pr = form.fields["valor_principal"].clean(form.data.get("valor_principal"))
                j, m, _ = services.calcular_encargos(l, d, principal=pr)
                form = BaixaForm(initial={"data": d, "conta": form.data.get("conta"), "valor_principal": pr,
                                          "juros": j, "multa": m, "desconto": 0,
                                          "observacao": form.data.get("observacao", "")})
                messages.info(request, f"Encargos recalculados: multa {services.MULTA_PCT}% + juros {services.JUROS_MES_PCT}% a.m. pro rata.")
            except ValidationError as e:
                erros(request, e)
        elif form.is_valid():
            c = form.cleaned_data
            try:
                b = services.baixar(l, data=c["data"], conta=c["conta"], valor_principal=c["valor_principal"],
                                    juros=c["juros"], multa=c["multa"], desconto=c["desconto"],
                                    observacao=c["observacao"], usuario=request.user)
            except ValidationError as e:
                erros(request, e)
            else:
                if item:
                    services.conciliar(item, b) if b.conta_id == item.conta_id else None
                messages.success(request, f"Baixa de {brl(b.total)} registrada.")
                return redirect("lancamento", pk=pk)
    else:
        try:
            d = datetime.strptime(request.GET["data"], "%Y-%m-%d").date() if request.GET.get("data") else hoje
        except ValueError:
            d = hoje
        j, m, _ = services.calcular_encargos(l, d)
        form = BaixaForm(initial={"data": d, "conta": conta_padrao, "valor_principal": l.saldo_aberto,
                                  "juros": j, "multa": m, "desconto": 0})
    return render(request, "fin/baixa_form.html", {"form": form, "l": l, "item": item,
                  "dias_atraso": max((hoje - l.vencimento).days, 0)})


@escrita
@require_POST
def baixa_estornar(request, pk):
    b = get_object_or_404(Baixa, pk=pk)
    try:
        services.estornar_baixa(b)
        messages.success(request, "Baixa estornada.")
    except ValidationError as e:
        erros(request, e)
    return redirect("lancamento", pk=b.lancamento_id)


@escrita
@require_POST
def lancamento_cancelar(request, pk):
    l = get_object_or_404(Lancamento, pk=pk)
    try:
        services.cancelar_lancamento(l)
        messages.success(request, "Lançamento cancelado.")
    except ValidationError as e:
        erros(request, e)
    return redirect("lancamento", pk=pk)


@escrita
@require_POST
def lancamento_excluir(request, pk):
    l = get_object_or_404(Lancamento, pk=pk)
    try:
        l.delete()
        messages.success(request, "Lançamento excluído.")
    except ValidationError as e:
        erros(request, e)
        return redirect("lancamento", pk=pk)
    return redirect("receber" if l.tipo == "R" else "pagar")


@escrita
@require_POST
def cobranca_emitir(request, pk):
    l = get_object_or_404(Lancamento, pk=pk)
    try:
        c = services.gerar_cobranca(l, request.POST.get("tipo", "boleto"), request.user)
    except ValidationError as e:
        erros(request, e)
        return redirect("lancamento", pk=pk)
    return redirect("cobranca", pk=c.pk)


def cobranca(request, pk):
    c = get_object_or_404(Cobranca.objects.select_related("lancamento__parceiro"), pk=pk)
    return render(request, "fin/cobranca.html", {
        "c": c, "l": c.lancamento,
        "qr": services.qr_simulado_svg(c.codigo) if c.tipo == "pix" else None})


def anexo(request, pk):
    l = get_object_or_404(Lancamento, pk=pk)
    if not l.anexo:
        raise Http404()
    try:
        return FileResponse(l.anexo.open("rb"), as_attachment=True, filename=l.anexo.name.split("/")[-1])
    except FileNotFoundError:
        raise Http404()


# ------------------------------------------------------------------ transferencias
def transferencias(request):
    form = TransferenciaForm(request.POST or None) if papel_de(request.user) in ("admin", "financeiro") else None
    if request.method == "POST":
        if form is None:
            return render(request, "fin/403.html", status=403)
        if form.is_valid():
            c = form.cleaned_data
            try:
                services.transferir(origem=c["origem"], destino=c["destino"], valor=c["valor"], data=c["data"],
                                    descricao=c["descricao"], usuario=request.user)
            except ValidationError as e:
                erros(request, e)
            else:
                messages.success(request, "Transferência registrada.")
                return redirect("transferencias")
    qs = Transferencia.objects.select_related("origem", "destino")
    return render(request, "fin/transferencias.html", {"form": form, "pag": paginar(request, qs, 15)})


# ------------------------------------------------------------------ caixa
def caixa_lista(request):
    aberto = SessaoCaixa.objects.filter(status="aberto").select_related("conta")
    form = AbrirCaixaForm(request.POST or None) if papel_de(request.user) in ("admin", "financeiro") else None
    if request.method == "POST":
        if form is None:
            return render(request, "fin/403.html", status=403)
        if form.is_valid():
            try:
                s = services.abrir_caixa(form.cleaned_data["conta"], form.cleaned_data["data"], request.user)
            except ValidationError as e:
                erros(request, e)
            else:
                messages.success(request, f"Caixa aberto com {brl(s.valor_abertura)}.")
                return redirect("caixa", pk=s.pk)
    qs = SessaoCaixa.objects.select_related("conta", "fechado_por")
    return render(request, "fin/caixa_lista.html", {"aberto": aberto, "form": form, "pag": paginar(request, qs, 15)})


def caixa_detalhe(request, pk):
    s = get_object_or_404(SessaoCaixa.objects.select_related("conta", "aberto_por", "fechado_por"), pk=pk)
    pode = papel_de(request.user) in ("admin", "financeiro")
    form = None
    if pode and s.status == "aberto":
        form = MovimentoCaixaForm(request.POST or None, conta=s.conta)
        if request.method == "POST" and form.is_valid():
            c = form.cleaned_data
            try:
                services.registrar_movimento_caixa(s, c["tipo"], c["valor"], c["motivo"], c["contraparte"], request.user)
            except ValidationError as e:
                erros(request, e)
            else:
                messages.success(request, "Movimento registrado.")
                return redirect("caixa", pk=pk)
    elif request.method == "POST":
        return render(request, "fin/403.html", status=403)
    resumo = services.resumo_sessao(s)
    baixas = Baixa.objects.filter(conta=s.conta, data=s.data).select_related("lancamento__parceiro")
    logs = AuditLog.objects.filter(modelo__in=["SessaoCaixa"], objeto_id=str(s.pk))
    contagem = sorted(((Decimal(k), v) for k, v in (s.contagem or {}).items()), reverse=True)
    return render(request, "fin/caixa_detalhe.html", {
        "s": s, "r": resumo, "form": form, "baixas": baixas, "movs": s.movimentos.select_related("contraparte"),
        "logs": logs, "contagem": [(d, v, d * v) for d, v in contagem]})


@escrita
def caixa_fechar(request, pk):
    s = get_object_or_404(SessaoCaixa, pk=pk)
    if s.status != "aberto":
        messages.error(request, "Este caixa já está fechado.")
        return redirect("caixa", pk=pk)
    form = FecharCaixaForm(request.POST or None)
    r = services.resumo_sessao(s)
    if request.method == "POST" and form.is_valid():
        try:
            services.fechar_caixa(s, contagem=form.contagem(), valor_contado=form.cleaned_data["valor_contado"],
                                  observacao=form.cleaned_data["observacao"], usuario=request.user)
        except ValidationError as e:
            erros(request, e)
        else:
            messages.success(request, "Caixa fechado e conferido.")
            return redirect("caixa", pk=pk)
    return render(request, "fin/caixa_fechar.html", {"s": s, "form": form, "r": r})


# ------------------------------------------------------------------ cadastros
@escrita
def _crud_form(request, modelo, form_cls, pk, destino, titulo):
    obj = get_object_or_404(modelo, pk=pk) if pk else None
    form = form_cls(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Salvo com sucesso.")
        return redirect(destino)
    return render(request, "fin/form_generico.html", {"form": form, "titulo": titulo + (" — editar" if obj else " — novo"),
                                                      "voltar": destino})


def contas(request):
    linhas = [(c, c.saldo()) for c in ContaFinanceira.objects.all()]
    total = q2(sum((s for c, s in linhas if c.ativa), Decimal(0)))
    return render(request, "fin/contas.html", {"linhas": linhas, "total": total})


def conta_form(request, pk=None):
    return _crud_form(request, ContaFinanceira, ContaForm, pk, "contas", "Conta financeira")


def parceiros(request):
    qs = Parceiro.objects.all()
    q = request.GET.get("q", "").strip()
    t = request.GET.get("tipo", "")
    if q:
        qs = qs.filter(Q(nome__icontains=q) | Q(documento__icontains=q))
    if t in ("cliente", "fornecedor"):
        qs = qs.filter(tipo__in=[t, "ambos"])
    return render(request, "fin/parceiros.html", {"pag": paginar(request, qs, 20), "q": q, "t": t})


def parceiro_form(request, pk=None):
    return _crud_form(request, Parceiro, ParceiroForm, pk, "parceiros", "Cliente/fornecedor")


def plano_contas(request):
    return render(request, "fin/plano_contas.html", {
        "categorias": Categoria.objects.all(), "centros": CentroCusto.objects.all()})


def categoria_form(request, pk=None):
    return _crud_form(request, Categoria, CategoriaForm, pk, "plano_contas", "Categoria")


def centro_form(request, pk=None):
    return _crud_form(request, CentroCusto, CentroForm, pk, "plano_contas", "Centro de custo")


# ------------------------------------------------------------------ conciliacao
def conciliacao(request):
    pode = papel_de(request.user) in ("admin", "financeiro")
    form = ImportarExtratoForm(request.POST or None, request.FILES or None) if pode else None
    if request.method == "POST":
        if form is None:
            return render(request, "fin/403.html", status=403)
        if form.is_valid():
            try:
                novos, dup = services.importar_extrato(form.cleaned_data["conta"], form.cleaned_data["arquivo"].name,
                                                       form.cleaned_data["arquivo"].read())
            except (ValueError, ValidationError) as e:
                messages.error(request, f"Falha na importação: {e}")
            else:
                messages.success(request, f"{novos} linha(s) importada(s); {dup} já existiam.")
                return redirect("conciliacao")
    status = request.GET.get("status", "pendente")
    conta_id = request.GET.get("conta", "")
    qs = ItemExtrato.objects.select_related("conta", "baixa__lancamento")
    if status in ("pendente", "conciliado", "ignorado"):
        qs = qs.filter(status=status)
    if conta_id.isdigit():
        qs = qs.filter(conta_id=conta_id)
    qs = qs.order_by("data", "id") if status == "pendente" else qs
    pag = paginar(request, qs, 12)
    for it in pag:
        if it.status == "pendente":
            it.sug = services.sugerir_pareamento(it)
            it.sug_l = services.sugerir_lancamentos(it) if not it.sug else []
    resumo = {s: ItemExtrato.objects.filter(status=s).count() for s in ("pendente", "conciliado", "ignorado")}
    return render(request, "fin/conciliacao.html", {
        "form": form, "pag": pag, "status": status, "conta_id": conta_id, "resumo": resumo,
        "contas": ContaFinanceira.objects.exclude(tipo="cartao")})


@escrita
@require_POST
def conciliar_item(request, pk):
    it = get_object_or_404(ItemExtrato, pk=pk)
    acao = request.POST.get("acao")
    try:
        if acao == "conciliar":
            services.conciliar(it, get_object_or_404(Baixa, pk=request.POST.get("baixa")))
        elif acao == "ignorar":
            it.status = "ignorado"
            it.save()
        elif acao == "desfazer":
            it.status, it.baixa = "pendente", None
            it.save()
    except ValidationError as e:
        erros(request, e)
    return redirect(request.POST.get("voltar") or "conciliacao")


@escrita
@require_POST
def conciliar_auto(request):
    n = 0
    for it in ItemExtrato.objects.filter(status="pendente"):
        s = services.sugerir_pareamento(it)
        if s and s[0]["score"] >= 100 and (len(s) == 1 or s[0]["score"] > s[1]["score"]):
            services.conciliar(it, s[0]["baixa"])
            n += 1
    messages.success(request, f"{n} item(ns) conciliado(s) automaticamente (sugestões de alta confiança).")
    return redirect("conciliacao")


# ------------------------------------------------------------------ relatorios
def relatorios(request):
    return render(request, "fin/relatorios.html")


def rel_fluxo(request):
    form, ini, fim, conta, categoria = filtros_periodo(request)
    real = services.fluxo_realizado(ini, fim, conta, categoria)
    proj = services.fluxo_projetado(90, conta, incluir_inadimplentes=bool(request.GET.get("inad")))
    if request.GET.get("formato") == "csv":
        linhas = [[mes_br(l["mes"]), "Realizado", num_br(l["entradas"]), num_br(l["saidas"]), num_br(l["liquido"]),
                   num_br(l["saldo"]) if l["saldo"] is not None else ""] for l in real]
        linhas += [[f"{s['ini']:%d/%m/%Y}-{s['fim']:%d/%m/%Y}", "Projetado", num_br(s["entradas"]), num_br(s["saidas"]),
                    num_br(s["entradas"] - s["saidas"]), num_br(s["saldo"])] for s in proj["semanas"]]
        return csv_response("fluxo-de-caixa", ["Período", "Tipo", "Entradas", "Saídas", "Líquido", "Saldo"], linhas)
    g1 = charts.barras([mes_br(l["mes"]) for l in real], [("Entradas", [l["entradas"] for l in real], "b-ent"),
                       ("Saídas", [l["saidas"] for l in real], "b-sai")], "Fluxo de caixa realizado por mês")
    g2 = charts.linha([f"{s['ini']:%d/%m}" for s in proj["semanas"]], [s["saldo"] for s in proj["semanas"]],
                      "Saldo projetado, 90 dias")
    return render(request, "fin/rel_fluxo.html", {"form": form, "real": real, "proj": proj, "g1": g1, "g2": g2,
                  "ini": ini, "fim": fim, "inad": bool(request.GET.get("inad"))})


def rel_dre(request):
    form, ini, fim, _, _ = filtros_periodo(request, meses_padrao=3)
    d = services.dre(ini, fim)
    if request.GET.get("formato") == "csv":
        linhas = []
        for l in d["linhas"]:
            linhas.append([l["rotulo"], num_br(l["valor"]), num_br(l["pct"]) if l["pct"] is not None else ""])
            for n, v in l["detalhes"]:
                linhas.append([f"    {n}", num_br(v), ""])
        return csv_response("dre", ["Conta", "Valor (R$)", "% da receita bruta"], linhas)
    return render(request, "fin/rel_dre.html", {"form": form, "d": d, "ini": ini, "fim": fim})


def rel_aging(request):
    ar, ap = services.aging("R"), services.aging("P")
    if request.GET.get("formato") == "csv":
        linhas = [[t, f["rotulo"], num_br(f["valor"]), f["qtd"], num_br(f["pct"])]
                  for t, a in (("A receber", ar), ("A pagar", ap)) for f in a["faixas"]]
        return csv_response("aging", ["Carteira", "Faixa", "Valor (R$)", "Títulos", "%"], linhas)
    return render(request, "fin/rel_aging.html", {"ar": ar, "ap": ap})


def rel_inadimplencia(request):
    i = services.inadimplencia()
    if request.GET.get("formato") == "csv":
        return csv_response("inadimplencia", ["Cliente", "Valor vencido (R$)", "Títulos", "Maior atraso (dias)"],
                            [[d["nome"], num_br(d["valor"]), d["qtd"], d["max_dias"]] for d in i["devedores"]])
    vencidos = Lancamento.objects.filter(tipo="R", status__in=Lancamento.ABERTOS, vencimento__lt=timezone.localdate()) \
        .select_related("parceiro").order_by("vencimento")
    return render(request, "fin/rel_inadimplencia.html", {"i": i, "pag": paginar(request, vencidos, 12)})


def rel_ranking(request):
    form, ini, fim, _, _ = filtros_periodo(request)
    cli, tc = services.ranking("R", ini, fim, 10)
    forn, tf = services.ranking("P", ini, fim, 10)
    if request.GET.get("formato") == "csv":
        linhas = [["Cliente", x["nome"], num_br(x["total"]), num_br(x["pago"]), num_br(x["aberto"])] for x in cli]
        linhas += [["Fornecedor", x["nome"], num_br(x["total"]), num_br(x["pago"]), num_br(x["aberto"])] for x in forn]
        return csv_response("ranking", ["Tipo", "Nome", "Total (R$)", "Pago (R$)", "Em aberto (R$)"], linhas)
    return render(request, "fin/rel_ranking.html", {"form": form, "cli": cli, "forn": forn, "tc": tc, "tf": tf,
                  "ini": ini, "fim": fim})


def rel_comparativo(request):
    linhas = services.comparativo_mensal()
    if request.GET.get("formato") == "csv":
        return csv_response("comparativo-mensal",
                            ["Mês", "Receitas", "Despesas", "Resultado", "Var. receita %", "Entradas caixa", "Saídas caixa"],
                            [[mes_br(l["mes"]), num_br(l["receitas"]), num_br(l["despesas"]), num_br(l["resultado"]),
                              num_br(l["var_receita"]) if l["var_receita"] is not None else "",
                              num_br(l["caixa_entradas"]), num_br(l["caixa_saidas"])] for l in linhas])
    g = charts.barras([mes_br(l["mes"]) for l in linhas], [("Receitas", [l["receitas"] for l in linhas], "b-ent"),
                      ("Despesas", [l["despesas"] for l in linhas], "b-sai")], "Receitas e despesas por mês de competência")
    g2 = charts.linha([mes_br(l["mes"]) for l in linhas], [l["resultado"] for l in linhas], "Resultado por mês")
    return render(request, "fin/rel_comparativo.html", {"linhas": linhas, "g": g, "g2": g2})


# ------------------------------------------------------------------ periodos e auditoria
def periodos(request):
    hoje = timezone.localdate()
    fechados = {(p.ano, p.mes): p for p in PeriodoFechado.objects.select_related("fechado_por")}
    meses, cur = [], add_months(primeiro_dia(hoje), -1)
    for _ in range(18):
        meses.append({"d": cur, "p": fechados.get((cur.year, cur.month))})
        cur = add_months(cur, -1)
    return render(request, "fin/periodos.html", {"meses": meses})


@escrita
@require_POST
def periodo_fechar(request):
    try:
        services.fechar_periodo(int(request.POST["ano"]), int(request.POST["mes"]), request.user)
        messages.success(request, "Período fechado: lançamentos, baixas e transferências ficam imutáveis.")
    except (ValidationError, ValueError, KeyError) as e:
        erros(request, e) if isinstance(e, ValidationError) else messages.error(request, "Período inválido.")
    return redirect("periodos")


@somente_admin
@require_POST
def periodo_reabrir(request):
    p = PeriodoFechado.objects.filter(ano=request.POST.get("ano"), mes=request.POST.get("mes")).first()
    if p:
        p.delete()
        messages.success(request, "Período reaberto.")
    return redirect("periodos")


def auditoria(request):
    if papel_de(request.user) not in ("admin", "financeiro"):
        return render(request, "fin/403.html", status=403)
    qs = AuditLog.objects.all()
    modelo, q = request.GET.get("modelo", ""), request.GET.get("q", "").strip()
    if modelo:
        qs = qs.filter(modelo=modelo)
    if q:
        qs = qs.filter(Q(objeto_repr__icontains=q) | Q(usuario_nome__icontains=q))
    modelos = AuditLog.objects.order_by().values_list("modelo", flat=True).distinct()
    return render(request, "fin/auditoria.html", {"pag": paginar(request, qs, 25), "modelo": modelo, "q": q,
                                                  "modelos": sorted(modelos)})
