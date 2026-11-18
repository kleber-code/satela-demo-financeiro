from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError

from fin import services
from fin.models import (Baixa, Lancamento, MovimentoCaixa, PeriodoFechado, PeriodoFechadoError, SessaoCaixa,
                        Transferencia, AuditLog)
from .base import Base, D


class ParcelamentoTests(Base):
    def test_soma_das_parcelas_igual_ao_total(self):
        for total, n in [("100.00", 3), ("0.10", 3), ("1000.01", 7), ("99999.99", 12), ("10.00", 1), ("0.05", 5)]:
            parcelas = services.parcelar(D(total), n)
            self.assertEqual(len(parcelas), n)
            self.assertEqual(sum(parcelas), D(total), (total, n))
            self.assertTrue(max(parcelas) - min(parcelas) <= D("0.01"))
            self.assertTrue(all(p == p.quantize(D("0.01")) for p in parcelas))

    def test_resto_vai_para_as_primeiras(self):
        self.assertEqual(services.parcelar(D("100.00"), 3), [D("33.34"), D("33.33"), D("33.33")])

    def test_criar_parcelado_e_recorrente(self):
        ls = services.criar_lancamentos(tipo="R", descricao="Projeto", categoria=self.c_rec, valor=D("100.00"),
                                        vencimento=date(2026, 1, 31), modo="parcelado", quantidade=3)
        self.assertEqual(sum(l.valor for l in ls), D("100.00"))
        self.assertEqual([l.vencimento for l in ls], [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)])
        self.assertEqual({l.grupo for l in ls}.__len__(), 1)
        self.assertEqual([l.parcela for l in ls], [1, 2, 3])
        rec = services.criar_lancamentos(tipo="P", descricao="Aluguel", categoria=self.c_dop, valor=D("500.00"),
                                         vencimento=date(2026, 5, 10), modo="recorrente", quantidade=6)
        self.assertEqual(len(rec), 6)
        self.assertTrue(all(r.valor == D("500.00") and r.recorrente for r in rec))
        self.assertEqual(rec[-1].vencimento, date(2026, 10, 10))


class BaixaTests(Base):
    def test_encargos_multa_e_juros_pro_rata(self):
        l = self.novo(valor="1000.00", venc=self.hoje - timedelta(days=10))
        juros, multa, dias = services.calcular_encargos(l, self.hoje)
        self.assertEqual((dias, multa, juros), (10, D("20.00"), D("3.33")))
        self.assertEqual(services.calcular_encargos(l, self.hoje - timedelta(days=10))[:2], (D("0.00"), D("0.00")))

    def test_baixa_parcial_com_juros_e_multa(self):
        l = self.novo(valor="1000.00", venc=self.hoje - timedelta(days=10))
        b = services.baixar(l, data=self.hoje, conta=self.banco, valor_principal=D("400.00"), juros=D("3.33"),
                            multa=D("20.00"), desconto=D("1.00"), usuario=self.fin)
        l.refresh_from_db()
        self.assertEqual(b.total, D("422.33"))
        self.assertEqual((l.status, l.valor_pago, l.saldo_aberto), ("parcial", D("400.00"), D("600.00")))
        self.assertEqual(self.banco.saldo(), D("10000.00") + D("422.33"))
        services.baixar(l, data=self.hoje, conta=self.banco, valor_principal=D("600.00"))
        l.refresh_from_db()
        self.assertEqual((l.status, l.saldo_aberto, l.data_pagamento), ("paga", D("0.00"), self.hoje))

    def test_validacoes_da_baixa(self):
        l = self.novo(valor="100.00")
        with self.assertRaises(ValidationError):
            services.baixar(l, data=self.hoje, conta=self.banco, valor_principal=D("100.01"))
        with self.assertRaises(ValidationError):
            services.baixar(l, data=self.hoje + timedelta(days=1), conta=self.banco, valor_principal=D("10"))
        with self.assertRaises(ValidationError):
            services.baixar(l, data=self.hoje, conta=self.banco, valor_principal=D("0"))

    def test_pagamento_reduz_saldo_e_estorno_restaura(self):
        l = self.novo("P", valor="250.00")
        b = services.baixar(l, data=self.hoje, conta=self.banco, valor_principal=D("250.00"))
        self.assertEqual(self.banco.saldo(), D("9750.00"))
        services.estornar_baixa(b)
        l.refresh_from_db()
        self.assertEqual((self.banco.saldo(), l.status, l.valor_pago), (D("10000.00"), "aberta", D("0.00")))

    def test_status_vencida(self):
        self.assertTrue(self.novo(venc=self.hoje - timedelta(days=1)).vencida)
        self.assertFalse(self.novo(venc=self.hoje).vencida)


class CaixaTests(Base):
    def test_abertura_movimentos_e_fechamento_com_diferenca(self):
        s = services.abrir_caixa(self.caixa, self.hoje, self.fin)
        self.assertEqual(s.valor_abertura, D("500.00"))
        r = self.novo("R", valor="300.00", venc=self.hoje)
        services.baixar(r, data=self.hoje, conta=self.caixa, valor_principal=D("300.00"))
        p = self.novo("P", valor="80.50", venc=self.hoje)
        services.baixar(p, data=self.hoje, conta=self.caixa, valor_principal=D("80.50"))
        services.registrar_movimento_caixa(s, "suprimento", D("100.00"), "troco", self.banco, self.fin)
        services.registrar_movimento_caixa(s, "sangria", D("200.00"), "deposito", self.banco, self.fin)
        resumo = services.resumo_sessao(s)
        self.assertEqual(resumo["esperado"], D("500.00") + D("300.00") - D("80.50") + D("100.00") - D("200.00"))
        # sangria/suprimento com contraparte movimentam o banco
        self.assertEqual(self.banco.saldo(), D("10000.00") - D("100.00") + D("200.00"))
        self.assertEqual(self.caixa.saldo(), resumo["esperado"])
        with self.assertRaises(ValidationError):  # diferenca exige observacao
            services.fechar_caixa(s, valor_contado=D("619.00"), usuario=self.fin)
        f = services.fechar_caixa(s, contagem={"100": 6, "10": 1, "5": 1, "2": 2, "0.50": 1}, observacao="falta de troco",
                                  usuario=self.fin)
        self.assertEqual((f.valor_contado, f.valor_esperado, f.diferenca), (D("619.50"), D("619.50"), D("0.00")))

    def test_diferenca_negativa_e_imutabilidade(self):
        s = services.abrir_caixa(self.caixa, self.hoje, self.fin)
        f = services.fechar_caixa(s, valor_contado=D("495.00"), observacao="quebra de caixa", usuario=self.fin)
        self.assertEqual(f.diferenca, D("-5.00"))
        self.assertEqual(f.status, "fechado")
        f.observacao = "alterado"
        with self.assertRaises(PeriodoFechadoError):
            f.save()
        with self.assertRaises(ValidationError):
            services.registrar_movimento_caixa(f, "suprimento", D("10"), usuario=self.fin)
        with self.assertRaises(ValidationError):  # baixa em caixa ja fechado
            services.baixar(self.novo("P", valor="10.00"), data=self.hoje, conta=self.caixa, valor_principal=D("10.00"))
        self.assertTrue(AuditLog.objects.filter(modelo="SessaoCaixa", acao="alterou").exists())

    def test_so_um_caixa_aberto_e_sangria_maior_que_saldo(self):
        s = services.abrir_caixa(self.caixa, self.hoje, self.fin)
        with self.assertRaises(ValidationError):
            services.abrir_caixa(self.caixa, self.hoje - timedelta(days=1), self.fin)
        with self.assertRaises(ValidationError):
            services.registrar_movimento_caixa(s, "sangria", D("9999.00"), usuario=self.fin)
        with self.assertRaises(ValidationError):
            services.abrir_caixa(self.banco, self.hoje)

    def test_transferencia(self):
        t = services.transferir(origem=self.banco, destino=self.caixa, valor=D("700.00"), data=self.hoje,
                                usuario=self.fin)
        self.assertEqual((self.banco.saldo(), self.caixa.saldo()), (D("9300.00"), D("1200.00")))
        with self.assertRaises(ValidationError):
            services.transferir(origem=self.caixa, destino=self.banco, valor=D("5000"), data=self.hoje)
        with self.assertRaises(ValidationError):
            services.transferir(origem=self.banco, destino=self.banco, valor=D("1"), data=self.hoje)


class RelatoriosTests(Base):
    def test_fluxo_projetado(self):
        h = self.hoje
        self.novo("R", valor="1000.00", venc=h + timedelta(days=2))      # semana 1
        self.novo("P", valor="400.00", venc=h + timedelta(days=9))       # semana 2
        self.novo("R", valor="250.00", venc=h + timedelta(days=30))      # semana 5
        self.novo("P", valor="100.00", venc=h - timedelta(days=5))       # vencida: semana 1
        self.novo("R", valor="777.00", venc=h - timedelta(days=5))       # inadimplente (fora por padrao)
        self.novo("R", valor="999.00", venc=h + timedelta(days=120))     # fora do horizonte
        p = services.fluxo_projetado(90, hoje=h)
        s = p["semanas"]
        self.assertEqual(len(s), 13)
        self.assertEqual((s[0]["entradas"], s[0]["saidas"]), (D("1000.00"), D("100.00")))
        self.assertEqual(s[1]["saidas"], D("400.00"))
        self.assertEqual(s[4]["entradas"], D("250.00"))
        self.assertEqual(p["saldo_inicial"], D("10500.00"))
        self.assertEqual(p["saldo_final"], D("10500.00") + D("1250.00") - D("500.00"))
        self.assertEqual(s[1]["saldo"], D("10500.00") + D("900.00") - D("400.00"))
        p2 = services.fluxo_projetado(90, hoje=h, incluir_inadimplentes=True)
        self.assertEqual(p2["semanas"][0]["entradas"], D("1777.00"))

    def test_dre(self):
        h = self.hoje.replace(day=10)
        self.novo("R", valor="10000.00", venc=h)
        self.novo("P", valor="800.00", venc=h, cat=self.c_imp)
        self.novo("P", valor="3000.00", venc=h, cat=self.c_cus)
        self.novo("P", valor="1500.00", venc=h, cat=self.c_dop)
        self.novo("P", valor="50.00", venc=h, cat=self.c_dfin)
        cancel = self.novo("R", valor="5555.00", venc=h)
        services.cancelar_lancamento(cancel)
        # juros/multa recebidos entram como receita financeira
        r = self.novo("R", valor="200.00", venc=h)
        services.baixar(r, data=self.hoje, conta=self.banco, valor_principal=D("200.00"), juros=D("5.00"), multa=D("4.00"))
        d = services.dre(h.replace(day=1), self.hoje)
        v = {l["rotulo"]: l["valor"] for l in d["linhas"]}
        self.assertEqual(d["receita_bruta"], D("10200.00"))
        self.assertEqual(v["(−) Deduções e impostos sobre a receita"], D("-800.00"))
        self.assertEqual(v["(=) Receita líquida"], D("9400.00"))
        self.assertEqual(d["lucro_bruto"], D("6400.00"))
        self.assertEqual(d["resultado_operacional"], D("4900.00"))
        self.assertEqual(v["(+) Receitas financeiras"], D("9.00"))
        self.assertEqual(d["resultado_liquido"], D("4900.00") + D("9.00") - D("50.00"))

    def test_aging(self):
        h = self.hoje
        for dias, val in [(-5, "100.00"), (3, "200.00"), (30, "300.00"), (31, "400.00"), (60, "500.00"), (61, "600.00"), (200, "700.00")]:
            self.novo("R", valor=val, venc=h - timedelta(days=dias))
        parcial = self.novo("R", valor="1000.00", venc=h - timedelta(days=45))
        services.baixar(parcial, data=h, conta=self.banco, valor_principal=D("400.00"))
        pago = self.novo("R", valor="9999.00", venc=h - timedelta(days=45))
        services.baixar(pago, data=h, conta=self.banco, valor_principal=D("9999.00"))
        a = {f["chave"]: f for f in services.aging("R", h)["faixas"]}
        self.assertEqual(a["a_vencer"]["valor"], D("100.00"))
        self.assertEqual(a["1_30"]["valor"], D("500.00"))
        self.assertEqual(a["31_60"]["valor"], D("900.00") + D("600.00"))
        self.assertEqual(a["60_mais"]["valor"], D("1300.00"))
        ag = services.aging("R", h)
        self.assertEqual(ag["total"], D("3400.00"))
        self.assertEqual(ag["vencido"], D("3300.00"))
        ina = services.inadimplencia(h)
        self.assertEqual(ina["vencido"], D("3300.00"))

    def test_comparativo_e_ranking(self):
        self.novo("R", valor="100.00", venc=self.hoje)
        lin = services.comparativo_mensal(self.hoje)
        self.assertEqual(len(lin), 12)
        self.assertEqual(lin[-1]["receitas"], D("100.00"))
        top, total = services.ranking("R", self.hoje.replace(day=1), self.hoje)
        self.assertEqual((top[0]["nome"], total), ("Cliente Teste", D("100.00")))


class PeriodoFechadoTests(Base):
    def test_periodo_fechado_e_imutavel(self):
        venc = date(self.hoje.year - 1, 3, 10)
        l = self.novo("R", valor="500.00", venc=venc)
        # baixa antiga (antes de fechar)
        Baixa.objects.create(lancamento=l, data=venc, conta=self.banco, valor_principal=D("100.00"))
        PeriodoFechado.objects.create(ano=venc.year, mes=3)
        l.refresh_from_db()
        l.valor = D("999.00")
        with self.assertRaises(PeriodoFechadoError):
            l.save()
        l.refresh_from_db()
        l.descricao = "outra"
        with self.assertRaises(PeriodoFechadoError):
            l.save()
        with self.assertRaises(PeriodoFechadoError):
            l.delete()
        with self.assertRaises(PeriodoFechadoError):
            self.novo("R", venc=date(venc.year, 3, 20))              # novo lancamento no periodo
        with self.assertRaises(ValidationError):                       # baixa/transferencia datadas no periodo
            services.baixar(l, data=venc, conta=self.banco, valor_principal=D("10.00"))
        with self.assertRaises(ValidationError):
            services.transferir(origem=self.banco, destino=self.caixa, valor=D("1"), data=venc)
        with self.assertRaises(PeriodoFechadoError):
            Baixa.objects.get(lancamento=l).delete()
        # mas a liquidacao hoje (periodo aberto) e permitida: apenas status/valor pago mudam
        services.baixar(l, data=self.hoje, conta=self.banco, valor_principal=D("500.00"))
        l.refresh_from_db()
        self.assertEqual(l.status, "paga")

    def test_fechar_periodo_regras(self):
        with self.assertRaises(ValidationError):
            services.fechar_periodo(self.hoje.year, self.hoje.month, self.fin)  # mes corrente
        mes_passado = (self.hoje.replace(day=1) - timedelta(days=1))
        p = services.fechar_periodo(mes_passado.year, mes_passado.month, self.fin)
        self.assertTrue(PeriodoFechado.objects.filter(pk=p.pk).exists())
        with self.assertRaises(ValidationError):
            services.fechar_periodo(mes_passado.year, mes_passado.month, self.fin)


class AuditoriaTests(Base):
    def test_edicao_gera_trilha_com_antes_e_depois(self):
        l = self.novo(valor="100.00")
        AuditLog.objects.all().delete()
        l = Lancamento.objects.get(pk=l.pk)
        l.valor = D("120.00")
        l.save()
        g = AuditLog.objects.get(modelo="Lancamento", acao="alterou")
        self.assertEqual(g.mudancas["valor"], ["100.00", "120.00"])
        self.assertEqual(g.objeto_id, str(l.pk))
