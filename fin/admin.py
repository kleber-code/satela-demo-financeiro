from django.contrib import admin

from . import models

for m in (models.Perfil, models.CentroCusto, models.Categoria, models.ContaFinanceira, models.Parceiro,
          models.Lancamento, models.Baixa, models.Transferencia, models.SessaoCaixa, models.MovimentoCaixa,
          models.PeriodoFechado, models.Cobranca, models.ItemExtrato, models.AuditLog):
    admin.site.register(m)
