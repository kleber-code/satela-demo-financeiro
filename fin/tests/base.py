from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from fin import services
from fin.models import Categoria, CentroCusto, ContaFinanceira, Parceiro, Perfil

D = Decimal
User = get_user_model()


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.hoje = timezone.localdate()
        cls.banco = ContaFinanceira.objects.create(nome="Banco", tipo="banco", saldo_inicial=D("10000.00"),
                                                   data_saldo_inicial=cls.hoje - timedelta(days=400))
        cls.caixa = ContaFinanceira.objects.create(nome="Caixa", tipo="caixa", saldo_inicial=D("500.00"),
                                                   data_saldo_inicial=cls.hoje - timedelta(days=400))
        mk = lambda cod, nome, t, g: Categoria.objects.create(codigo=cod, nome=nome, tipo=t, grupo=g)
        cls.c_rec = mk("1", "Servicos", "R", "receita_operacional")
        cls.c_fin = mk("1.9", "Rendimentos", "R", "receita_financeira")
        cls.c_imp = mk("2", "Impostos", "D", "deducao")
        cls.c_cus = mk("3", "Equipe", "D", "custo")
        cls.c_dop = mk("4", "Aluguel", "D", "despesa_operacional")
        cls.c_dfin = mk("5", "Tarifas", "D", "despesa_financeira")
        cls.centro = CentroCusto.objects.create(codigo="1", nome="Geral")
        cls.cliente = Parceiro.objects.create(nome="Cliente Teste", tipo="cliente")
        cls.forn = Parceiro.objects.create(nome="Fornecedor Teste", tipo="fornecedor")
        cls.fin = cls._user("fin", Perfil.FINANCEIRO)
        cls.leitor = cls._user("leitor", Perfil.LEITURA)
        cls.adm = cls._user("adm", Perfil.ADMIN)

    @staticmethod
    def _user(nome, papel):
        u = User.objects.create_user(nome, password="senha-forte-123")
        Perfil.objects.create(usuario=u, papel=papel)
        return u

    def novo(self, tipo="R", valor="1000.00", venc=None, cat=None, **kw):
        venc = venc or self.hoje
        cat = cat or (self.c_rec if tipo == "R" else self.c_dop)
        return services.criar_lancamentos(
            tipo=tipo, descricao=kw.pop("descricao", "Teste"), categoria=cat, valor=D(valor), vencimento=venc,
            parceiro=self.cliente if tipo == "R" else self.forn, conta=self.banco, **kw)[0]
