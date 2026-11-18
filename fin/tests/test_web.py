from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse

from fin import services
from fin.models import ItemExtrato, Lancamento, Baixa, Perfil
from .base import Base, D


class AcessoTests(Base):
    def setUp(self):
        cache.clear()

    def test_saude_sem_login(self):
        r = self.client.get("/saude/")
        self.assertEqual((r.status_code, r.content), (200, b"ok"))

    def test_login_obrigatorio(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/entrar/", r["Location"])

    @override_settings(DEMO_MODE=True)
    def test_demo_um_clique(self):
        call_command("seed_demo", "--se-vazio", verbosity=0) if False else None
        from django.contrib.auth import get_user_model
        u = get_user_model().objects.create_user("financeiro")
        Perfil.objects.create(usuario=u, papel="financeiro")
        r = self.client.post("/entrar-demo/")
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        page = self.client.get("/")
        self.assertContains(page, "Ambiente de demonstração — dados fictícios")

    @override_settings(DEMO_MODE=False)
    def test_demo_desligado(self):
        self.assertEqual(self.client.post("/entrar-demo/").status_code, 404)
        self.client.force_login(self.fin)
        self.assertNotContains(self.client.get("/"), "Ambiente de demonstração")

    def test_rate_limit_de_login(self):
        for _ in range(5):
            self.client.post("/entrar/", {"usuario": "fin", "senha": "errada"})
        r = self.client.post("/entrar/", {"usuario": "fin", "senha": "senha-forte-123"})
        self.assertContains(r, "Muitas tentativas")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_normal(self):
        r = self.client.post("/entrar/", {"usuario": "fin", "senha": "senha-forte-123"})
        self.assertEqual(r.status_code, 302)


class PermissaoTests(Base):
    def test_leitura_nao_escreve(self):
        l = self.novo()
        self.client.force_login(self.leitor)
        for url in ["/", "/receber/", "/pagar/", "/caixa/", "/relatorios/dre/", "/conciliacao/", "/transferencias/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(self.client.get(reverse("lancamento_novo", args=["R"])).status_code, 403)
        self.assertEqual(self.client.get(reverse("lancamento_baixar", args=[l.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("lancamento_cancelar", args=[l.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("periodo_fechar"), {"ano": 2020, "mes": 1}).status_code, 403)
        self.assertEqual(self.client.post("/transferencias/", {"origem": self.banco.pk, "destino": self.caixa.pk,
                                                               "valor": "1,00", "data": self.hoje}).status_code, 403)
        self.assertEqual(self.client.get("/auditoria/").status_code, 403)
        self.assertNotContains(self.client.get(reverse("lancamento", args=[l.pk])), "Registrar recebimento")
        l.refresh_from_db()
        self.assertEqual(l.status, "aberta")

    def test_financeiro_cria_e_baixa(self):
        self.client.force_login(self.fin)
        r = self.client.post(reverse("lancamento_novo", args=["R"]), {
            "descricao": "Contrato X", "parceiro": self.cliente.pk, "categoria": self.c_rec.pk, "valor": "1.234,56".replace(".", ""),
            "vencimento": self.hoje.isoformat(), "modo": "parcelado", "quantidade": 3})
        self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:300])
        ls = Lancamento.objects.filter(descricao="Contrato X")
        self.assertEqual(ls.count(), 3)
        self.assertEqual(sum(l.valor for l in ls), D("1234.56"))
        l = ls.first()
        r = self.client.post(reverse("lancamento_baixar", args=[l.pk]), {
            "data": self.hoje.isoformat(), "conta": self.banco.pk, "valor_principal": "100,00", "juros": "1,50", "multa": "", "desconto": ""})
        self.assertEqual(r.status_code, 302)
        b = Baixa.objects.get(lancamento=l)
        self.assertEqual(b.total, D("101.50"))

    def test_financeiro_nao_reabre_periodo(self):
        self.client.force_login(self.fin)
        self.assertEqual(self.client.post(reverse("periodo_reabrir"), {"ano": 2020, "mes": 1}).status_code, 403)
        self.client.force_login(self.adm)
        self.assertEqual(self.client.post(reverse("periodo_reabrir"), {"ano": 2020, "mes": 1}).status_code, 302)

    def test_edicao_em_periodo_fechado_mostra_erro(self):
        from datetime import date
        from fin.models import PeriodoFechado
        l = self.novo(venc=date(self.hoje.year - 1, 2, 10))
        PeriodoFechado.objects.create(ano=l.competencia.year, mes=l.competencia.month)
        self.client.force_login(self.fin)
        r = self.client.post(reverse("lancamento_editar", args=[l.pk]), {
            "descricao": "novo nome", "categoria": self.c_rec.pk, "valor": "5,00", "vencimento": l.vencimento.isoformat()})
        self.assertContains(r, "Período fechado")
        l.refresh_from_db()
        self.assertEqual(l.descricao, "Teste")


class TelasTests(Base):
    def test_telas_principais_com_dados(self):
        call_command("seed_demo", "--reset", verbosity=0)
        self.client.force_login(self.fin)
        urls = ["/", "/receber/", "/pagar/", "/caixa/", "/contas/", "/parceiros/", "/plano-de-contas/", "/conciliacao/",
                "/relatorios/", "/relatorios/fluxo-de-caixa/", "/relatorios/dre/", "/relatorios/aging/",
                "/relatorios/inadimplencia/", "/relatorios/ranking/", "/relatorios/comparativo/", "/periodos/", "/auditoria/",
                "/transferencias/", "/receber/?formato=csv", "/relatorios/dre/?formato=csv", "/relatorios/aging/?formato=csv"]
        for u in urls:
            self.assertEqual(self.client.get(u).status_code, 200, u)
        csv = self.client.get("/relatorios/dre/?formato=csv").content.decode("utf-8-sig")
        self.assertIn("Receita operacional bruta;", csv)
        self.assertContains(self.client.get("/"), "R$ ")

    def test_seed_idempotente_e_reset(self):
        call_command("seed_demo", "--reset", verbosity=0)
        n = Lancamento.objects.count()
        self.assertGreater(n, 300)
        call_command("seed_demo", verbosity=0)
        call_command("seed_demo", "--se-vazio", verbosity=0)
        self.assertEqual(Lancamento.objects.count(), n)
        call_command("seed_demo", "--reset", verbosity=0)
        self.assertEqual(Lancamento.objects.count(), n)

    def test_saldos_do_seed_nunca_absurdos(self):
        call_command("seed_demo", "--reset", verbosity=0)
        from fin.models import ContaFinanceira
        from datetime import timedelta
        banco = ContaFinanceira.objects.get(tipo="banco", nome__icontains="Corrente")
        for i in range(0, 360, 7):
            s = services.saldo_conta(banco, self.hoje - timedelta(days=i))
            self.assertGreater(s, D("20000"), i)
            self.assertLess(s, D("1500000"), i)
        caixa = ContaFinanceira.objects.get(tipo="caixa")
        self.assertGreaterEqual(caixa.saldo(), 0)


class ConciliacaoTests(Base):
    def test_parse_csv_e_pareamento(self):
        csv = "data;descricao;valor\n05/03/2026;PIX CLIENTE TESTE;1.234,56\n06/03/2026;PAGTO FORNECEDOR;-250,00\n06/03/2026;PAGTO FORNECEDOR;-250,00\n"
        self.assertEqual(services.parse_csv(csv), [
            (__import__("datetime").date(2026, 3, 5), "PIX CLIENTE TESTE", D("1234.56")),
            (__import__("datetime").date(2026, 3, 6), "PAGTO FORNECEDOR", D("-250.00")),
            (__import__("datetime").date(2026, 3, 6), "PAGTO FORNECEDOR", D("-250.00"))])
        with self.assertRaises(ValueError):
            services.parse_csv("foo;bar\n1;2\n")
        l = self.novo("R", valor="1234.56", venc=__import__("datetime").date(2026, 3, 4))
        Baixa.objects.create(lancamento=l, data=__import__("datetime").date(2026, 3, 5), conta=self.banco,
                             valor_principal=D("1234.56"))
        novos, dup = services.importar_extrato(self.banco, "ext.csv", csv.encode())
        self.assertEqual((novos, dup), (3, 0))
        self.assertEqual(services.importar_extrato(self.banco, "ext.csv", csv.encode()), (0, 3))  # sem duplicar
        it = ItemExtrato.objects.get(valor=D("1234.56"))
        sug = services.sugerir_pareamento(it)
        self.assertEqual(len(sug), 1)
        self.assertGreaterEqual(sug[0]["score"], 100)
        services.conciliar(it, sug[0]["baixa"])
        it.refresh_from_db()
        self.assertEqual(it.status, "conciliado")
        self.assertEqual(services.sugerir_pareamento(it), [])  # baixa ja usada

    def test_ofx(self):
        ofx = "<OFX><BANKTRANLIST><STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260310<TRNAMT>150.75<MEMO>DEPOSITO</STMTTRN></BANKTRANLIST></OFX>"
        self.assertEqual(services.parse_ofx(ofx)[0][2], D("150.75"))


class CobrancaTests(Base):
    def test_cobranca_simulada_marcada(self):
        l = self.novo("R", valor="100.00")
        self.client.force_login(self.fin)
        r = self.client.post(reverse("cobranca_emitir", args=[l.pk]), {"tipo": "pix"})
        self.assertEqual(r.status_code, 302)
        page = self.client.get(r["Location"])
        self.assertContains(page, "SIMULAÇÃO")
        self.assertContains(page, "não funcional")


class AnexoTests(Base):
    def _post(self, arquivo):
        return self.client.post(reverse("lancamento_novo", args=["P"]), {
            "descricao": "Com anexo", "parceiro": self.forn.pk, "categoria": self.c_dop.pk, "valor": "10,00",
            "vencimento": self.hoje.isoformat(), "modo": "unico", "quantidade": 1, "anexo": arquivo})

    def test_limites_de_upload(self):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=tmp):
            self.client.force_login(self.fin)
            ok = self._post(SimpleUploadedFile("nota.pdf", b"%PDF-1.4 teste", content_type="application/pdf"))
            self.assertEqual(ok.status_code, 302, ok.context["form"].errors if ok.context else "")
            l = Lancamento.objects.get(descricao="Com anexo")
            self.assertEqual(self.client.get(reverse("anexo", args=[l.pk])).status_code, 200)
            grande = self._post(SimpleUploadedFile("grande.pdf", b"x" * (2 * 1024 * 1024 + 1)))
            self.assertContains(grande, "2 MB")
            exe = self._post(SimpleUploadedFile("virus.exe", b"MZ"))
            self.assertEqual(exe.status_code, 200)
            self.assertEqual(Lancamento.objects.filter(descricao="Com anexo").count(), 1)


class FluxosHttpTests(Base):
    def test_caixa_transferencia_conciliacao_e_periodo_pela_web(self):
        from fin.models import SessaoCaixa, Transferencia, PeriodoFechado
        c = self.client
        c.force_login(self.fin)
        r = c.post("/caixa/", {"conta": self.caixa.pk, "data": self.hoje.isoformat()})
        s = SessaoCaixa.objects.get()
        self.assertRedirects(r, reverse("caixa", args=[s.pk]))
        r = c.post(reverse("caixa", args=[s.pk]), {"tipo": "suprimento", "valor": "150,00", "motivo": "troco", "contraparte": self.banco.pk})
        self.assertEqual(r.status_code, 302)
        r = c.post(reverse("caixa_fechar", args=[s.pk]), {"den_100": 6, "den_50": 1, "observacao": ""})
        self.assertEqual(r.status_code, 302, r.content[:200])
        s.refresh_from_db()
        self.assertEqual((s.status, s.valor_esperado, s.valor_contado, s.diferenca), ("fechado", D("650.00"), D("650.00"), D("0.00")))
        self.assertContains(c.get(reverse("caixa", args=[s.pk])), "Conferência de valores")
        r = c.post("/transferencias/", {"origem": self.banco.pk, "destino": self.caixa.pk, "valor": "10,00", "data": (self.hoje - timedelta(days=1)).isoformat()})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Transferencia.objects.filter(descricao="").count(), 1)
        # conciliacao via upload
        from django.core.files.uploadedfile import SimpleUploadedFile
        l = self.novo("R", valor="321.00", venc=self.hoje)
        services.baixar(l, data=self.hoje, conta=self.banco, valor_principal=D("321.00"))
        csv = f"data;descricao;valor\n{self.hoje:%d/%m/%Y};PIX CLIENTE TESTE;321,00\n".encode()
        r = c.post("/conciliacao/", {"conta": self.banco.pk, "arquivo": SimpleUploadedFile("e.csv", csv)})
        self.assertEqual(r.status_code, 302)
        self.assertContains(c.get("/conciliacao/"), "sugestão")
        c.post("/conciliacao/auto/")
        self.assertEqual(ItemExtrato.objects.get().status, "conciliado")
        self.assertEqual(c.get("/conciliacao/?status=conciliado").status_code, 200)
        # estorno e periodo
        b = Baixa.objects.get(lancamento=l)
        self.assertEqual(c.post(reverse("baixa_estornar", args=[b.pk])).status_code, 302)
        l.refresh_from_db()
        self.assertEqual(l.status, "aberta")
        mp = self.hoje.replace(day=1) - timedelta(days=1)
        c.post("/periodos/fechar/", {"ano": mp.year, "mes": mp.month})
        self.assertTrue(PeriodoFechado.objects.filter(ano=mp.year, mes=mp.month).exists())
        self.assertContains(c.get("/periodos/"), "Fechado")
        self.assertEqual(c.get("/auditoria/?modelo=Baixa").status_code, 200)
