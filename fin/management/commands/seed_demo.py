"""Dados ficticios de uma empresa de servicos (12 meses de movimento coerente).

Uso: python manage.py seed_demo [--reset] [--se-vazio]
Idempotente: se ja houver dados, nao duplica (use --reset para recriar).
"""
import hashlib
import random
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from fin import services, signals
from fin.models import (ZERO, AuditLog, Baixa, Categoria, CentroCusto, Cobranca, ContaFinanceira, ItemExtrato,
                        Lancamento, MovimentoCaixa, Parceiro, PeriodoFechado, Perfil, SessaoCaixa, Transferencia, q2)
from fin.services import add_months, primeiro_dia

User = get_user_model()
D = Decimal


def dec(x):
    return q2(D(str(x)))


def garantir_usuarios(stdout):
    def user(nome, papel, first):
        u, novo = User.objects.get_or_create(username=nome, defaults={"first_name": first})
        if novo or u.has_usable_password():
            u.set_unusable_password()  # acesso so pelo botao de demonstracao
            u.save()
        Perfil.objects.update_or_create(usuario=u, defaults={"papel": papel})
        return u
    fin = user("financeiro", Perfil.FINANCEIRO, "Financeiro (demo)")
    user("leitura", Perfil.LEITURA, "Somente leitura (demo)")
    marina = user("marina", Perfil.FINANCEIRO, "Marina")
    senha = settings.DEMO_ADMIN_PASSWORD
    if senha:
        a, _ = User.objects.get_or_create(username="admin", defaults={"is_staff": True, "is_superuser": True})
        a.is_staff = a.is_superuser = True
        a.set_password(senha)
        a.save()
        Perfil.objects.update_or_create(usuario=a, defaults={"papel": Perfil.ADMIN})
        stdout.write("Superusuario 'admin' configurado via DEMO_ADMIN_PASSWORD.")
    return fin, marina


def apagar_tudo():
    for m in (ItemExtrato, AuditLog, Cobranca, MovimentoCaixa, SessaoCaixa, Transferencia, Baixa, Lancamento,
              PeriodoFechado, Parceiro, Categoria, CentroCusto, ContaFinanceira):
        m.objects.all().delete()


class Command(BaseCommand):
    help = "Popula o banco com dados ficticios de demonstracao."

    def add_arguments(self, p):
        p.add_argument("--reset", action="store_true", help="apaga os dados financeiros e recria")
        p.add_argument("--se-vazio", action="store_true", help="so popula se o banco estiver vazio")

    def handle(self, *a, reset=False, se_vazio=False, **kw):
        fin_u, marina = garantir_usuarios(self.stdout)
        if reset:
            apagar_tudo()
        elif Lancamento.objects.exists() or ContaFinanceira.objects.exists():
            if not se_vazio:
                self.stdout.write("Ja existem dados; nada a fazer (use --reset para recriar).")
            return
        signals.pausado = True
        try:
            with transaction.atomic():
                Gerador(self.stdout, fin_u, marina).rodar()
        finally:
            signals.pausado = False


class Gerador:
    def __init__(self, out, fin_u, marina):
        self.out, self.fin_u, self.marina = out, fin_u, marina
        self.rng = random.Random(2026)
        self.hoje = timezone.localdate()
        self.inicio = add_months(primeiro_dia(self.hoje), -11)
        self.logs = []

    # -------- utilitarios
    def user(self):
        return self.rng.choice([self.fin_u, self.marina])

    def log(self, quando, user, acao, modelo, obj, mud):
        self.logs.append(AuditLog(quando=quando, usuario=user, usuario_nome=user.username, acao=acao,
                                  modelo=modelo, objeto_id=str(obj.pk), objeto_repr=str(obj)[:200], mudancas=mud))

    def dt(self, d, h=None):
        h = h if h is not None else self.rng.randint(8, 17)
        return timezone.make_aware(datetime.combine(d, time(h, self.rng.randint(0, 59))))

    def mk(self, tipo, desc, parceiro, cat, centro, conta, valor, venc, comp=None, parcela=1, total=1,
           grupo=None, recorrente=False, obs=""):
        l = Lancamento(tipo=tipo, descricao=desc, parceiro=parceiro, categoria=cat, centro_custo=centro, conta=conta,
                       valor=dec(valor), vencimento=venc, competencia=comp or primeiro_dia(venc), parcela=parcela,
                       total_parcelas=total, grupo=grupo, recorrente=recorrente, observacao=obs,
                       criado_por=self.fin_u)
        l.save()
        return l

    def baixa(self, l, data, conta, principal, juros=ZERO, multa=ZERO, desconto=ZERO, obs=""):
        b = Baixa.objects.create(lancamento=l, data=data, conta=conta, valor_principal=dec(principal), juros=juros,
                                 multa=multa, desconto=desconto, observacao=obs, usuario=self.user())
        l.valor_pago = q2(l.valor_pago + b.valor_principal)
        l.status = Lancamento.PAGA if l.saldo_aberto == 0 else Lancamento.PARCIAL
        l.data_pagamento = data if l.status == Lancamento.PAGA else None
        l.save()
        if data >= self.hoje - timedelta(days=30):
            self.log(self.dt(data), b.usuario, "criou", "Baixa", b,
                     {"lancamento_id": ["", str(l.pk)], "valor_principal": ["", str(b.valor_principal)]})
        return b

    def baixa_com_encargos(self, l, data, conta, principal, desconto_multa=False):
        j, m, _ = services.calcular_encargos(l, data, principal=principal)
        desc = m if (desconto_multa and m) else ZERO
        return self.baixa(l, data, conta, principal, j, m, desc)

    # -------- cadastros
    def cadastros(self):
        C = lambda *x: Categoria.objects.create(codigo=x[0], nome=x[1], tipo=x[2], grupo=x[3])
        self.cat = {c.codigo: c for c in [
            C("1.01", "Consultoria de dados e IA", "R", "receita_operacional"),
            C("1.02", "Licenciamento e suporte mensal", "R", "receita_operacional"),
            C("1.03", "Projetos de implantação ERP/IA", "R", "receita_operacional"),
            C("1.90", "Rendimentos de aplicação", "R", "receita_financeira"),
            C("2.01", "Impostos sobre serviços", "D", "deducao"),
            C("3.01", "Equipe técnica (salários e encargos)", "D", "custo"),
            C("3.02", "Parceiros e freelancers", "D", "custo"),
            C("4.01", "Folha administrativa", "D", "despesa_operacional"),
            C("4.02", "Aluguel e condomínio", "D", "despesa_operacional"),
            C("4.03", "Software e nuvem", "D", "despesa_operacional"),
            C("4.04", "Marketing e eventos", "D", "despesa_operacional"),
            C("4.05", "Contabilidade, jurídico e seguros", "D", "despesa_operacional"),
            C("4.06", "Energia, internet e telefonia", "D", "despesa_operacional"),
            C("4.07", "Material de escritório e copa", "D", "despesa_operacional"),
            C("4.08", "Viagens e deslocamentos", "D", "despesa_operacional"),
            C("4.09", "Pró-labore dos sócios", "D", "despesa_operacional"),
            C("5.01", "Tarifas bancárias", "D", "despesa_financeira"),
        ]}
        self.cc = {c.codigo: c for c in [CentroCusto.objects.create(codigo=a, nome=b) for a, b in
                   [("100", "Comercial"), ("200", "Projetos"), ("300", "Suporte"), ("900", "Administrativo")]]}
        i0 = self.inicio
        mk = lambda n, t, i, **k: ContaFinanceira.objects.create(nome=n, tipo=t, saldo_inicial=dec(i),
                                                                 data_saldo_inicial=i0, **k)
        self.caixa = mk("Caixa Matriz", "caixa", 3000, instituicao="Cofre da empresa")
        self.banco = mk("Banco Aurora · Conta Corrente", "banco", 110000, instituicao="Banco Aurora (fictício)")
        self.reserva = mk("Banco Aurora · Reserva (aplicação)", "banco", 70000, instituicao="Banco Aurora (fictício)")
        self.cartao = mk("Cartão Corporativo", "cartao", 0, instituicao="Cartão Visa (fictício)")

        self.clientes_def = [
            # nome, fee, idx_inicio, dia, perfil, inadimplente_desde
            ("Vento Sul Logística", 24500, 0, 5, "pontual", None), ("Casa Verde Alimentos", 18900, 0, 10, "pontual", None),
            ("Praça Nova Varejo", 21300, 0, 10, "atrasa", None), ("Horizonte Saúde Clínicas", 16800, 0, 15, "pontual", None),
            ("Rio Claro Transportes", 14200, 0, 15, "parcial", None), ("Mirante Educação", 12600, 0, 20, "pontual", None),
            ("Atlas Engenharia", 27800, 0, 20, "pontual", None), ("Pétala Cosméticos", 11400, 0, 5, "atrasa", None),
            ("Quartzo Mineração", 19500, 0, 15, "inad", 8), ("Maré Alta Pescados", 9800, 3, 10, "inad", 9),
            ("Estrela do Norte Hotéis", 22700, 0, 25, "pontual", None), ("Cobalto Seguros", 31500, 0, 5, "pontual", None),
            ("Lume Energia", 17300, 4, 20, "atrasa", None), ("Ipê Agro", 13900, 5, 10, "pontual", None),
            ("Faro Contabilidade", 7800, 0, 15, "pontual", None), ("Trilha Turismo", 8900, 6, 10, "inad", 10),
        ]
        self.cli = {}
        extras = ["Pedra Branca Construtora", "Sol Nascente Farma", "Balcão (clientes avulsos)"]
        for i, nome in enumerate([c[0] for c in self.clientes_def] + extras):
            slug = "".join(ch for ch in nome.lower().replace(" ", "") if ch.isalnum())
            self.cli[nome] = Parceiro.objects.create(
                nome=nome, tipo="cliente", documento=f"{10+i}.{300+i*7}.{500+i*3}/0001-{(i*13)%100:02d}",
                email=f"financeiro@{slug}.example", telefone=f"(11) 5550-{1000+i*37:04d}")
        sup = [("Colaboradores (folha de pagamento)", "fornecedor"), ("Dev Parceiros Freelancers", "fornecedor"),
               ("Imobiliária Centro Norte", "fornecedor"), ("Nuvem Forte Hospedagem", "fornecedor"),
               ("Suíte Office Cloud", "fornecedor"), ("Agência Pixel Norte", "fornecedor"),
               ("Contábil Prisma", "fornecedor"), ("Conecta Telecom", "fornecedor"), ("Energia Luz do Vale", "fornecedor"),
               ("Rota Certa Viagens", "fornecedor"), ("Papelaria Ponto Certo", "fornecedor"),
               ("Fazenda (tributos municipais e federais)", "fornecedor"), ("Banco Aurora", "ambos"),
               ("Seguros Escudo", "fornecedor"), ("Mercadinho do Bairro", "fornecedor"), ("Sócios (pró-labore)", "fornecedor")]
        self.forn = {}
        for i, (nome, tipo) in enumerate(sup):
            slug = "".join(ch for ch in nome.lower().split(" (")[0].replace(" ", "") if ch.isalnum())
            self.forn[nome] = Parceiro.objects.create(nome=nome, tipo=tipo, documento=f"{50+i}.{200+i*11}.{700+i}/0001-{(i*17)%100:02d}",
                                                      email=f"cobranca@{slug}.example", telefone=f"(11) 5551-{2000+i*41:04d}")

    # -------- receitas
    def receitas(self):
        rng, hoje = self.rng, self.hoje
        self.rec_mes = {}  # competencia -> total receita operacional
        for nome, fee, idx0, dia, perfil, desde in self.clientes_def:
            grupo = uuid.uuid4()
            cliente = self.cli[nome]
            n = 0
            for idx in range(idx0, 15):
                n += 1
                ms = add_months(self.inicio, idx)
                venc = ms.replace(day=dia)
                valor = q2(D(fee) * D("0.85") * (D("1.05") if idx >= 7 else 1))
                cat = self.cat["1.02" if idx % 2 == 0 or fee < 15000 else "1.01"]
                centro = self.cc["300" if cat.codigo == "1.02" else "200"]
                titulo = ("Suporte e licenciamento mensal" if cat.codigo == "1.02" else "Consultoria mensal de dados e IA")
                l = self.mk("R", f"{titulo} · {nome}", cliente, cat, centro, self.banco, valor, venc, ms,
                            parcela=n, total=15 - idx0, grupo=grupo, recorrente=True)
                self.rec_mes[ms] = self.rec_mes.get(ms, ZERO) + l.valor
                self.receber(l, perfil, desde, idx)
        projetos = [(1, "Atlas Engenharia", 60000, 3, "pontual"), (3, "Cobalto Seguros", 80000, 4, "pontual"),
                    (5, "Horizonte Saúde Clínicas", 40000, 2, "atrasa"), (7, "Vento Sul Logística", 70000, 3, "pontual"),
                    (8, "Casa Verde Alimentos", 50000, 3, "pontual"), (9, "Pedra Branca Construtora", 45000, 3, "atrasa"),
                    (10, "Sol Nascente Farma", 30000, 2, "pontual")]
        for idx, nome, total, n, perfil in projetos:
            grupo = uuid.uuid4()
            for i, v in enumerate(services.parcelar(D(total), n)):
                ms = add_months(self.inicio, idx + i)
                l = self.mk("R", f"Projeto de implantação ERP/IA · {nome}", self.cli[nome], self.cat["1.03"], self.cc["200"],
                            self.banco, v, ms.replace(day=28), ms, parcela=i + 1, total=n, grupo=grupo)
                self.rec_mes[ms] = self.rec_mes.get(ms, ZERO) + v
                # Pedra Branca: 2a parcela fica em aberto (vencida)
                if nome == "Pedra Branca Construtora" and i >= 1:
                    continue
                self.receber(l, perfil, None, idx + i)
        # rendimentos da aplicacao
        for idx in range(0, 12):
            ms = add_months(self.inicio, idx)
            v = D(1050) + D(idx) * D(55) + D(rng.randint(0, 90))
            l = self.mk("R", "Rendimento de aplicação (CDB)", self.forn["Banco Aurora"], self.cat["1.90"], self.cc["900"],
                        self.reserva, v, ms.replace(day=28), ms)
            if l.vencimento <= hoje:
                self.baixa(l, l.vencimento, self.reserva, v)
        # avulsas canceladas
        ms = add_months(self.inicio, 8)
        for nome in ("Sol Nascente Farma", "Pétala Cosméticos"):
            l = self.mk("R", f"Proposta de workshop (cancelada) · {nome}", self.cli[nome], self.cat["1.01"],
                        self.cc["100"], self.banco, 9000, ms.replace(day=20), ms)
            l.status = Lancamento.CANCELADA
            l.save()

    def receber(self, l, perfil, desde, idx):
        rng, hoje = self.rng, self.hoje
        venc = l.vencimento
        if perfil == "inad" and desde is not None and idx >= desde:
            return
        if perfil == "pontual":
            d = venc + timedelta(days=rng.choice([0, 0, 1, 2]))
            if d <= hoje:
                self.baixa(l, d, self.banco, l.valor)
        elif perfil == "atrasa":
            d = venc + timedelta(days=rng.randint(6, 20))
            if d <= hoje:
                self.baixa_com_encargos(l, d, self.banco, l.valor, desconto_multa=rng.random() < .2)
        elif perfil == "parcial":
            p1 = q2(l.valor * D("0.6"))
            d1 = venc + timedelta(days=2)
            if d1 <= hoje:
                self.baixa(l, d1, self.banco, p1, obs="Pagamento parcial (acordo)")
                d2 = venc + timedelta(days=24)
                if d2 <= hoje:
                    self.baixa_com_encargos(l, d2, self.banco, l.saldo_aberto)
        else:  # inad antes do periodo de calote
            d = venc + timedelta(days=rng.choice([0, 3]))
            if d <= hoje:
                self.baixa(l, d, self.banco, l.valor)

    # -------- despesas
    def despesas(self):
        rng, hoje, F = self.rng, self.hoje, self.forn
        self.cartao_mes = {}
        defs = [  # chave, fornecedor, cat, centro, valor(idx), dia, offset_mes, conta
            ("Folha de pagamento · equipe técnica", "Colaboradores (folha de pagamento)", "3.01", "200", lambda i: 80000 + i * 3700, 5, 1, "b"),
            ("Parceiros e freelancers de desenvolvimento", "Dev Parceiros Freelancers", "3.02", "200", lambda i: 9000 + i * 800 + rng.randint(-600, 900), 10, 1, "b"),
            ("Folha de pagamento · administrativo", "Colaboradores (folha de pagamento)", "4.01", "900", lambda i: 30000 + i * 600, 5, 1, "b"),
            ("Pró-labore dos sócios", "Sócios (pró-labore)", "4.09", "900", lambda i: 14000, 5, 1, "b"),
            ("Aluguel e condomínio da sede", "Imobiliária Centro Norte", "4.02", "900", lambda i: 14000 if i < 7 else 14700, 8, 0, "b"),
            ("Hospedagem e nuvem", "Nuvem Forte Hospedagem", "4.03", "200", lambda i: 7500 + i * 350 + rng.randint(-250, 400), 12, 0, "c"),
            ("Licenças de software (suíte de escritório)", "Suíte Office Cloud", "4.03", "900", lambda i: 2200, 12, 0, "c"),
            ("Campanhas e mídia", "Agência Pixel Norte", "4.04", "100", lambda i: 6000 + rng.randint(-800, 1500), 22, 0, "b"),
            ("Contabilidade", "Contábil Prisma", "4.05", "900", lambda i: 4500, 15, 0, "b"),
            ("Seguro empresarial", "Seguros Escudo", "4.05", "900", lambda i: 1800, 9, 0, "b"),
            ("Internet e telefonia", "Conecta Telecom", "4.06", "900", lambda i: 1250, 18, 0, "b"),
            ("Energia elétrica", "Energia Luz do Vale", "4.06", "900", lambda i: 1900 + rng.randint(-200, 350), 20, 0, "b"),
            ("Viagens corporativas", "Rota Certa Viagens", "4.08", "100", lambda i: 2000 + rng.randint(0, 7000), 25, 0, "c"),
            ("Material de escritório", "Papelaria Ponto Certo", "4.07", "900", lambda i: 900 + rng.randint(0, 700), 14, 0, "b"),
            ("Tarifas e pacote de serviços bancários", "Banco Aurora", "5.01", "900", lambda i: 290 + rng.randint(0, 40), 28, 0, "b"),
        ]
        for idx in range(-1, 15):
            ms = add_months(self.inicio, idx)
            for desc, forn, cat, centro, f, dia, off, via in defs:
                if idx < 0 and off == 0:
                    continue  # mes anterior ao inicio: so a folha paga no 1o mes
                venc = add_months(ms, off).replace(day=dia)
                l = self.mk("P", desc, F[forn], self.cat[cat], self.cc[centro], self.cartao if via == "c" else self.banco,
                            f(idx), venc, ms, recorrente=True)
                if venc > hoje:
                    continue
                # pagamentos atrasados pontuais, com encargos
                atraso = 6 if (forn == "Conecta Telecom" and idx == 4) or (forn == "Papelaria Ponto Certo" and idx == 7) else 0
                # contas propositalmente em aberto e vencidas
                if (forn == "Seguros Escudo" and idx == 11) or (forn == "Agência Pixel Norte" and idx == 11 and venc < hoje):
                    continue
                if atraso:
                    self.baixa_com_encargos(l, venc + timedelta(days=atraso), self.banco, l.valor)
                elif via == "c":
                    self.baixa(l, venc, self.cartao, l.valor)
                    self.cartao_mes[ms] = self.cartao_mes.get(ms, ZERO) + l.valor
                else:
                    self.baixa(l, venc, self.banco, l.valor)
            # impostos sobre a receita do mes (8,5%)
            imp = q2(self.rec_mes.get(ms, ZERO) * D("0.085"))
            if imp:
                venc = add_months(ms, 1).replace(day=20)
                l = self.mk("P", "Impostos sobre serviços (ISS/PIS/COFINS)", F["Fazenda (tributos municipais e federais)"],
                            self.cat["2.01"], self.cc["900"], self.banco, imp, venc, ms, recorrente=True)
                if venc <= hoje:
                    self.baixa(l, venc, self.banco, imp)

    def transferencias(self):
        hoje = self.hoje
        for ms, total in sorted(self.cartao_mes.items()):
            d = add_months(ms, 1).replace(day=10)
            if d <= hoje:
                Transferencia.objects.create(origem=self.banco, destino=self.cartao, valor=total, data=d,
                                             descricao=f"Pagamento da fatura do cartão ({ms:%m/%Y})", usuario=self.fin_u)
        for idx, v in ((3, 40000), (6, 40000), (9, 30000)):
            d = add_months(self.inicio, idx).replace(day=21)
            if d <= hoje:
                Transferencia.objects.create(origem=self.banco, destino=self.reserva, valor=dec(v), data=d,
                                             descricao="Aporte na reserva financeira", usuario=self.marina)

    # -------- caixa
    def caixa_dias(self):
        rng, hoje = self.rng, self.hoje
        gastos = [("Lanche de reunião com cliente", "4.07", "Mercadinho do Bairro"), ("Café e copa", "4.07", "Mercadinho do Bairro"),
                  ("Material de escritório (compra rápida)", "4.07", "Papelaria Ponto Certo"),
                  ("Estacionamento e deslocamento", "4.08", "Rota Certa Viagens"), ("Correios e envio de documentos", "4.07", "Papelaria Ponto Certo")]
        entradas = [("Treinamento avulso no balcão", "1.01"), ("Relatório gerencial avulso", "1.01"), ("Hora técnica avulsa", "1.01")]
        r05 = lambda v: dec(round(float(v) * 20) / 20)
        d = hoje - timedelta(days=60)
        while d <= hoje:
            if d.weekday() >= 5:
                d += timedelta(days=1)
                continue
            s = services.abrir_caixa(self.caixa, d, self.fin_u)
            SessaoCaixa.objects.filter(pk=s.pk).update(aberto_em=self.dt(d, 8), aberto_por=self.user())
            if services.saldo_conta(self.caixa, d) < 1200:
                services.registrar_movimento_caixa(s, "suprimento", D("2500"), "Reforço de troco", self.banco, self.fin_u)
            for _ in range(rng.randint(0, 2)):
                desc, cat, forn = rng.choice(gastos)
                v = r05(rng.uniform(18, 190))
                l = self.mk("P", desc, self.forn[forn], self.cat[cat], self.cc["900"], self.caixa, v, d)
                self.baixa(l, d, self.caixa, v)
            if rng.random() < .55:
                desc, cat = rng.choice(entradas)
                v = r05(rng.uniform(250, 1400))
                cli = self.cli["Balcão (clientes avulsos)"]
                l = self.mk("R", desc, cli, self.cat[cat], self.cc["100"], self.caixa, v, d)
                self.baixa(l, d, self.caixa, v)
            if d == hoje:
                break
            r = services.resumo_sessao(s)
            if r["esperado"] > 4000:
                san = D(int((r["esperado"] - 1500) // 50) * 50)
                services.registrar_movimento_caixa(s, "sangria", san, "Depósito em conta (sangria)", self.banco, self.fin_u)
            esperado = services.resumo_sessao(s)["esperado"]
            dif = rng.choice([0, 0, 0, 0, 0, 0, D("-2.50"), D("5.00"), D("-10.00"), D("0.50")])
            contado = esperado + D(dif)
            contagem, resto = {}, contado
            for den in services.DENOMINACOES:
                q, resto = divmod(resto, den)
                if q:
                    contagem[str(den)] = int(q)
            user = self.user()
            services.fechar_caixa(s, contagem=contagem, observacao=("Diferença de troco conferida" if dif else ""),
                                  usuario=user)
            SessaoCaixa.objects.filter(pk=s.pk).update(fechado_em=self.dt(d, 18), aberto_em=self.dt(d, 8))
            s.refresh_from_db()
            self.log(self.dt(d, 8), s.aberto_por or user, "criou", "SessaoCaixa", s, {"data": ["", str(d)], "valor_abertura": ["", str(s.valor_abertura)]})
            self.log(self.dt(d, 18), user, "alterou", "SessaoCaixa", s,
                     {"status": ["aberto", "fechado"], "valor_esperado": ["", str(s.valor_esperado)],
                      "valor_contado": ["", str(s.valor_contado)], "diferenca": ["", str(s.diferenca)]})
            d += timedelta(days=1)

    # -------- extra: extrato, cobrancas, auditoria
    def extras(self):
        rng, hoje = self.rng, self.hoje
        bx = list(Baixa.objects.filter(conta=self.banco, data__gte=hoje - timedelta(days=25)).select_related("lancamento__parceiro").order_by("data"))
        for i, b in enumerate(bx):
            nome = (b.lancamento.parceiro.nome if b.lancamento.parceiro else "")[:24].upper()
            desc = ("PIX RECEBIDO " if b.valor_assinado > 0 else "PAGTO ") + nome
            data = b.data + timedelta(days=rng.choice([0, 0, 1]))
            h = hashlib.sha1(f"demo|{i}|{b.pk}".encode()).hexdigest()
            it = ItemExtrato.objects.create(conta=self.banco, data=min(data, hoje), descricao=desc, valor=b.valor_assinado, hash=h)
            if i % 4 == 0:
                it.status, it.baixa = "conciliado", b
                it.save()
        for k, (desc, v) in enumerate([("TARIFA PACOTE SERVICOS", D("-89.90")), ("IOF OPERACAO", D("-12.37")),
                                       ("TED RECEBIDA NAO IDENTIFICADA", D("1234.56"))]):
            h = hashlib.sha1(f"demo|extra|{k}".encode()).hexdigest()
            ItemExtrato.objects.create(conta=self.banco, data=hoje - timedelta(days=3 + k), descricao=desc, valor=v, hash=h)
        for l in Lancamento.objects.filter(tipo="R", status__in=("aberta", "parcial"), vencimento__lt=hoje)[:2]:
            services.gerar_cobranca(l, "boleto", self.fin_u)
        l = Lancamento.objects.filter(tipo="R", status="aberta", vencimento__gte=hoje).order_by("vencimento").first()
        if l:
            services.gerar_cobranca(l, "pix", self.fin_u)
        # edicoes auditadas de exemplo (aplicadas de fato)
        abertos = list(Lancamento.objects.filter(tipo="P", status="aberta", vencimento__gt=hoje + timedelta(days=3)).order_by("vencimento")[:4])
        for i, l in enumerate(abertos):
            quando = self.dt(hoje - timedelta(days=i + 1))
            if i % 2 == 0:
                antigo = l.vencimento
                novo = antigo + timedelta(days=3)
                if novo.replace(day=1) == antigo.replace(day=1):
                    Lancamento.objects.filter(pk=l.pk).update(vencimento=novo)
                    self.log(quando, self.marina, "alterou", "Lancamento", l, {"vencimento": [str(antigo), str(novo)]})
            else:
                antigo = l.valor
                novo = q2(antigo * D("1.04"))
                Lancamento.objects.filter(pk=l.pk).update(valor=novo)
                self.log(quando, self.fin_u, "alterou", "Lancamento", l, {"valor": [str(antigo), str(novo)]})
        for l in Lancamento.objects.filter(criado_por=self.fin_u, vencimento__gte=hoje - timedelta(days=20),
                                           vencimento__lte=hoje + timedelta(days=10))[:12]:
            self.log(self.dt(hoje - timedelta(days=rng.randint(1, 20))), self.user(), "criou", "Lancamento", l,
                     {"descricao": ["", l.descricao], "valor": ["", str(l.valor)], "vencimento": ["", str(l.vencimento)]})

    def fechar_periodos(self):
        for idx in range(0, 10):  # tudo exceto os 2 ultimos meses fechados
            ms = add_months(self.inicio, idx)
            p = PeriodoFechado.objects.create(ano=ms.year, mes=ms.month, fechado_por=self.marina,
                                              fechado_em=self.dt(add_months(ms, 1).replace(day=8), 10))
            self.log(p.fechado_em, self.marina, "criou", "PeriodoFechado", p, {"ano": ["", str(ms.year)], "mes": ["", str(ms.month)]})

    def rodar(self):
        self.cadastros()
        self.receitas()
        self.despesas()
        self.transferencias()
        self.caixa_dias()
        self.extras()
        self.fechar_periodos()
        AuditLog.objects.bulk_create(self.logs)
        self.stats()

    def stats(self):
        o = self.out
        o.write(f"Lancamentos: {Lancamento.objects.count()} · baixas: {Baixa.objects.count()} · caixas: {SessaoCaixa.objects.count()}")
        for c in ContaFinanceira.objects.all():
            o.write(f"  {c.nome}: {c.saldo()}")
        menor = min(services.saldo_conta(self.banco, add_months(self.inicio, i).replace(day=min(28, 28)))
                    for i in range(12))
        o.write(f"Menor saldo mensal (conta corrente): {menor}")
