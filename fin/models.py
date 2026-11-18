import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def q2(valor):
    """Arredonda para centavos (ROUND_HALF_UP)."""
    from decimal import ROUND_HALF_UP
    return Decimal(valor).quantize(CENT, rounding=ROUND_HALF_UP)


class PeriodoFechadoError(ValidationError):
    pass


# ---------------------------------------------------------------- auditoria
class AuditavelMixin:
    """Guarda o estado anterior para a trilha de auditoria (ver signals)."""
    AUDIT_FIELDS = ()

    def _snapshot(self):
        d = {}
        for f in self.AUDIT_FIELDS:
            v = getattr(self, f)
            d[f] = "" if v is None else str(v)
        return d

    def _capturar_antes(self):
        self._antes = None
        if self.pk:
            antigo = type(self).objects.filter(pk=self.pk).first()
            if antigo:
                self._antes = antigo._snapshot()


def periodo_fechado(d):
    if d is None:
        return False
    return PeriodoFechado.objects.filter(ano=d.year, mes=d.month).exists()


# ---------------------------------------------------------------- perfis
class Perfil(models.Model):
    ADMIN, FINANCEIRO, LEITURA = "admin", "financeiro", "leitura"
    PAPEIS = [(ADMIN, "Administrador"), (FINANCEIRO, "Financeiro"), (LEITURA, "Somente leitura")]
    usuario = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="perfil")
    papel = models.CharField(max_length=12, choices=PAPEIS, default=LEITURA)

    def __str__(self):
        return f"{self.usuario} ({self.get_papel_display()})"


def papel_de(user):
    if not user or not user.is_authenticated:
        return None
    if user.is_superuser:
        return Perfil.ADMIN
    p = getattr(user, "perfil", None)
    return p.papel if p else Perfil.LEITURA


# ---------------------------------------------------------------- cadastros
class CentroCusto(models.Model):
    codigo = models.CharField("código", max_length=10, unique=True)
    nome = models.CharField(max_length=80)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ["codigo"]
        verbose_name = "centro de custo"
        verbose_name_plural = "centros de custo"

    def __str__(self):
        return f"{self.codigo} · {self.nome}"


class Categoria(models.Model):
    RECEITA, DESPESA = "R", "D"
    TIPOS = [(RECEITA, "Receita"), (DESPESA, "Despesa")]
    GRUPOS = [
        ("receita_operacional", "Receita operacional"),
        ("receita_financeira", "Receita financeira"),
        ("deducao", "Deduções da receita (impostos)"),
        ("custo", "Custos dos serviços"),
        ("despesa_operacional", "Despesa operacional"),
        ("despesa_financeira", "Despesa financeira"),
    ]
    codigo = models.CharField("código", max_length=10, unique=True)
    nome = models.CharField(max_length=80)
    tipo = models.CharField(max_length=1, choices=TIPOS)
    grupo = models.CharField(max_length=24, choices=GRUPOS)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ["codigo"]

    def __str__(self):
        return f"{self.codigo} · {self.nome}"

    def clean(self):
        if self.grupo and self.tipo:
            ok = (self.tipo == self.RECEITA) == self.grupo.startswith("receita")
            if not ok:
                raise ValidationError("O grupo não é compatível com o tipo (receita/despesa).")


class ContaFinanceira(models.Model):
    CAIXA, BANCO, CARTAO = "caixa", "banco", "cartao"
    TIPOS = [(CAIXA, "Caixa"), (BANCO, "Conta bancária"), (CARTAO, "Cartão")]
    nome = models.CharField(max_length=80)
    tipo = models.CharField(max_length=8, choices=TIPOS)
    instituicao = models.CharField("instituição", max_length=60, blank=True)
    saldo_inicial = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    data_saldo_inicial = models.DateField("data do saldo inicial", default=date.today)
    ativa = models.BooleanField(default=True)

    class Meta:
        ordering = ["tipo", "nome"]
        verbose_name = "conta financeira"
        verbose_name_plural = "contas financeiras"

    def __str__(self):
        return self.nome

    def saldo(self, ate=None):
        from .services import saldo_conta
        return saldo_conta(self, ate)


class Parceiro(models.Model):
    CLIENTE, FORNECEDOR, AMBOS = "cliente", "fornecedor", "ambos"
    TIPOS = [(CLIENTE, "Cliente"), (FORNECEDOR, "Fornecedor"), (AMBOS, "Cliente e fornecedor")]
    nome = models.CharField(max_length=120)
    tipo = models.CharField(max_length=10, choices=TIPOS, default=CLIENTE)
    documento = models.CharField("CNPJ/CPF", max_length=20, blank=True)
    email = models.EmailField("e-mail", blank=True)
    telefone = models.CharField(max_length=20, blank=True)
    ativo = models.BooleanField(default=True)

    class Meta:
        ordering = ["nome"]
        verbose_name = "cliente/fornecedor"
        verbose_name_plural = "clientes e fornecedores"

    def __str__(self):
        return self.nome


# ---------------------------------------------------------------- lançamentos
def validar_anexo(arq):
    if arq.size > settings.LIMITE_ANEXO_BYTES:
        raise ValidationError("Anexo acima do limite de 2 MB.")


class Lancamento(AuditavelMixin, models.Model):
    RECEBER, PAGAR = "R", "P"
    TIPOS = [(RECEBER, "A receber"), (PAGAR, "A pagar")]
    ABERTA, PARCIAL, PAGA, CANCELADA = "aberta", "parcial", "paga", "cancelada"
    STATUS = [(ABERTA, "Aberta"), (PARCIAL, "Parcial"), (PAGA, "Paga"), (CANCELADA, "Cancelada")]
    ABERTOS = (ABERTA, PARCIAL)
    AUDIT_FIELDS = ("tipo", "descricao", "parceiro_id", "categoria_id", "centro_custo_id", "conta_id",
                    "valor", "vencimento", "competencia", "status", "valor_pago", "observacao")
    # campos que podem mudar mesmo em periodo fechado (efeito de baixas)
    CAMPOS_LIVRES = {"valor_pago", "status", "data_pagamento", "atualizado_em"}

    tipo = models.CharField(max_length=1, choices=TIPOS)
    descricao = models.CharField("descrição", max_length=160)
    parceiro = models.ForeignKey(Parceiro, null=True, blank=True, on_delete=models.PROTECT, related_name="lancamentos")
    categoria = models.ForeignKey(Categoria, on_delete=models.PROTECT, related_name="lancamentos")
    centro_custo = models.ForeignKey(CentroCusto, null=True, blank=True, on_delete=models.PROTECT)
    conta = models.ForeignKey(ContaFinanceira, null=True, blank=True, on_delete=models.PROTECT,
                              verbose_name="conta prevista", related_name="lancamentos")
    valor = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(CENT)])
    vencimento = models.DateField()
    competencia = models.DateField("competência", help_text="Primeiro dia do mês de competência")
    parcela = models.PositiveSmallIntegerField(default=1)
    total_parcelas = models.PositiveSmallIntegerField(default=1)
    grupo = models.UUIDField("grupo de parcelas/recorrência", null=True, blank=True, db_index=True)
    recorrente = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=STATUS, default=ABERTA, db_index=True)
    valor_pago = models.DecimalField("principal pago", max_digits=14, decimal_places=2, default=ZERO)
    data_pagamento = models.DateField(null=True, blank=True)
    observacao = models.CharField("observação", max_length=255, blank=True)
    anexo = models.FileField(upload_to="anexos/%Y/%m", blank=True, validators=[
        FileExtensionValidator(["pdf", "png", "jpg", "jpeg", "txt", "csv"]), validar_anexo])
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["vencimento", "id"]
        indexes = [models.Index(fields=["tipo", "status", "vencimento"])]

    def __str__(self):
        sufixo = f" ({self.parcela}/{self.total_parcelas})" if self.total_parcelas > 1 else ""
        return f"{self.descricao}{sufixo}"

    # ----- propriedades
    @property
    def saldo_aberto(self):
        if self.status == self.CANCELADA:
            return ZERO
        return q2(self.valor - self.valor_pago)

    @property
    def vencida(self):
        return self.status in self.ABERTOS and self.vencimento < timezone.localdate()

    @property
    def situacao(self):
        return "vencida" if self.vencida else self.status

    @property
    def situacao_label(self):
        return {"vencida": "Vencida", "aberta": "Aberta", "parcial": "Parcial",
                "paga": "Paga", "cancelada": "Cancelada"}[self.situacao]

    @property
    def dias_atraso(self):
        return max((timezone.localdate() - self.vencimento).days, 0) if self.status in self.ABERTOS else 0

    @property
    def periodo_fechado(self):
        return periodo_fechado(self.competencia)

    # ----- proteção
    def save(self, *args, **kw):
        if not self.competencia:
            self.competencia = self.vencimento.replace(day=1)
        self._capturar_antes()
        if periodo_fechado(self.competencia):
            if self._state.adding:
                raise PeriodoFechadoError("O período de competência está fechado.")
            antigo = Lancamento.objects.get(pk=self.pk)
            for f in self.AUDIT_FIELDS:
                if f in self.CAMPOS_LIVRES:
                    continue
                if getattr(antigo, f) != getattr(self, f):
                    raise PeriodoFechadoError("Período fechado: o lançamento não pode ser alterado.")
        elif not self._state.adding:
            antigo = Lancamento.objects.filter(pk=self.pk).first()
            if antigo and periodo_fechado(antigo.competencia) and antigo.competencia != self.competencia:
                raise PeriodoFechadoError("Período fechado: o lançamento não pode ser movido.")
        super().save(*args, **kw)

    def delete(self, *args, **kw):
        if periodo_fechado(self.competencia):
            raise PeriodoFechadoError("Período fechado: o lançamento não pode ser excluído.")
        if self.baixas.exists():
            raise PeriodoFechadoError("Há baixas registradas; estorne-as antes de excluir.")
        return super().delete(*args, **kw)


class Baixa(AuditavelMixin, models.Model):
    AUDIT_FIELDS = ("lancamento_id", "data", "conta_id", "valor_principal", "juros", "multa", "desconto")
    lancamento = models.ForeignKey(Lancamento, on_delete=models.CASCADE, related_name="baixas")
    data = models.DateField()
    conta = models.ForeignKey(ContaFinanceira, on_delete=models.PROTECT, related_name="baixas")
    valor_principal = models.DecimalField(max_digits=14, decimal_places=2)
    juros = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    multa = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    desconto = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    observacao = models.CharField(max_length=200, blank=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+")
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-data", "-id"]

    @property
    def total(self):
        """Valor efetivamente movimentado na conta."""
        return q2(self.valor_principal + self.juros + self.multa - self.desconto)

    @property
    def sinal(self):
        return 1 if self.lancamento.tipo == Lancamento.RECEBER else -1

    @property
    def valor_assinado(self):
        return self.total * self.sinal

    def __str__(self):
        return f"Baixa {self.total} em {self.data}"

    def save(self, *args, **kw):
        self._capturar_antes()
        if periodo_fechado(self.data):
            raise PeriodoFechadoError("O período da data da baixa está fechado.")
        super().save(*args, **kw)

    def delete(self, *args, **kw):
        if periodo_fechado(self.data):
            raise PeriodoFechadoError("Período fechado: a baixa não pode ser estornada.")
        return super().delete(*args, **kw)


class Transferencia(AuditavelMixin, models.Model):
    AUDIT_FIELDS = ("origem_id", "destino_id", "valor", "data", "descricao")
    origem = models.ForeignKey(ContaFinanceira, on_delete=models.PROTECT, related_name="saidas")
    destino = models.ForeignKey(ContaFinanceira, on_delete=models.PROTECT, related_name="entradas")
    valor = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(CENT)])
    data = models.DateField()
    descricao = models.CharField("descrição", max_length=160, blank=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+")
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-data", "-id"]

    def __str__(self):
        return f"{self.origem} → {self.destino}: {self.valor}"

    def clean(self):
        if self.origem_id and self.origem_id == self.destino_id:
            raise ValidationError("Origem e destino devem ser contas diferentes.")

    def save(self, *args, **kw):
        self._capturar_antes()
        if periodo_fechado(self.data):
            raise PeriodoFechadoError("O período da transferência está fechado.")
        super().save(*args, **kw)

    def delete(self, *args, **kw):
        if periodo_fechado(self.data):
            raise PeriodoFechadoError("Período fechado: a transferência não pode ser excluída.")
        return super().delete(*args, **kw)


# ---------------------------------------------------------------- caixa
class SessaoCaixa(AuditavelMixin, models.Model):
    ABERTO, FECHADO = "aberto", "fechado"
    AUDIT_FIELDS = ("conta_id", "data", "status", "valor_abertura", "valor_esperado", "valor_contado",
                    "diferenca", "observacao")
    conta = models.ForeignKey(ContaFinanceira, on_delete=models.PROTECT, related_name="sessoes")
    data = models.DateField()
    status = models.CharField(max_length=8, default=ABERTO, choices=[(ABERTO, "Aberto"), (FECHADO, "Fechado")])
    valor_abertura = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    aberto_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    aberto_em = models.DateTimeField(default=timezone.now)
    fechado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    fechado_em = models.DateTimeField(null=True, blank=True)
    valor_esperado = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    valor_contado = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    diferenca = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    contagem = models.JSONField("contagem por cédula/moeda", default=dict, blank=True)
    observacao = models.CharField("observação", max_length=255, blank=True)

    class Meta:
        ordering = ["-data", "-id"]
        verbose_name = "sessão de caixa"
        verbose_name_plural = "sessões de caixa"
        constraints = [models.UniqueConstraint(fields=["conta", "data"], name="uniq_sessao_conta_dia")]

    def __str__(self):
        return f"Caixa {self.conta} {self.data:%d/%m/%Y}"

    def save(self, *args, **kw):
        self._capturar_antes()
        if self.pk and self._antes and self._antes.get("status") == self.FECHADO:
            raise PeriodoFechadoError("Caixa já fechado: não pode ser alterado.")
        if periodo_fechado(self.data) and self._state.adding:
            raise PeriodoFechadoError("O período está fechado.")
        super().save(*args, **kw)


class MovimentoCaixa(AuditavelMixin, models.Model):
    SANGRIA, SUPRIMENTO = "sangria", "suprimento"
    TIPOS = [(SANGRIA, "Sangria (retirada)"), (SUPRIMENTO, "Suprimento (reforço)")]
    AUDIT_FIELDS = ("sessao_id", "tipo", "valor", "motivo")
    sessao = models.ForeignKey(SessaoCaixa, on_delete=models.CASCADE, related_name="movimentos")
    tipo = models.CharField(max_length=10, choices=TIPOS)
    valor = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(CENT)])
    motivo = models.CharField(max_length=160, blank=True)
    contraparte = models.ForeignKey(ContaFinanceira, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="+", verbose_name="conta de destino/origem (opcional)")
    transferencia = models.OneToOneField(Transferencia, null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name="movimento_caixa")
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+")
    criado_em = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["criado_em", "id"]

    def save(self, *args, **kw):
        self._capturar_antes()
        if self.sessao.status == SessaoCaixa.FECHADO:
            raise PeriodoFechadoError("Caixa fechado: não aceita movimentos.")
        super().save(*args, **kw)


class PeriodoFechado(AuditavelMixin, models.Model):
    AUDIT_FIELDS = ("ano", "mes")
    ano = models.PositiveSmallIntegerField()
    mes = models.PositiveSmallIntegerField()
    fechado_por = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    fechado_em = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-ano", "-mes"]
        constraints = [models.UniqueConstraint(fields=["ano", "mes"], name="uniq_periodo")]
        verbose_name = "período fechado"
        verbose_name_plural = "períodos fechados"

    def __str__(self):
        return f"{self.mes:02d}/{self.ano}"


# ---------------------------------------------------------------- cobrança simulada
class Cobranca(models.Model):
    BOLETO, PIX = "boleto", "pix"
    lancamento = models.ForeignKey(Lancamento, on_delete=models.CASCADE, related_name="cobrancas")
    tipo = models.CharField(max_length=6, choices=[(BOLETO, "Boleto"), (PIX, "Pix")])
    codigo = models.CharField(max_length=120)
    valor = models.DecimalField(max_digits=14, decimal_places=2)
    emitida_em = models.DateTimeField(default=timezone.now)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+")

    class Meta:
        ordering = ["-emitida_em"]


# ---------------------------------------------------------------- conciliação
class ItemExtrato(models.Model):
    PENDENTE, CONCILIADO, IGNORADO = "pendente", "conciliado", "ignorado"
    conta = models.ForeignKey(ContaFinanceira, on_delete=models.CASCADE, related_name="extrato")
    data = models.DateField()
    descricao = models.CharField(max_length=200)
    valor = models.DecimalField(max_digits=14, decimal_places=2, help_text="Positivo = crédito; negativo = débito")
    hash = models.CharField(max_length=40, db_index=True)
    status = models.CharField(max_length=10, default=PENDENTE,
                              choices=[(PENDENTE, "Pendente"), (CONCILIADO, "Conciliado"), (IGNORADO, "Ignorado")])
    baixa = models.ForeignKey(Baixa, null=True, blank=True, on_delete=models.SET_NULL, related_name="extratos")
    importado_em = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-data", "-id"]
        constraints = [models.UniqueConstraint(fields=["conta", "hash"], name="uniq_item_extrato")]


# ---------------------------------------------------------------- trilha
class AuditLog(models.Model):
    quando = models.DateTimeField(default=timezone.now, db_index=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+")
    usuario_nome = models.CharField(max_length=150, blank=True)
    acao = models.CharField(max_length=20)
    modelo = models.CharField(max_length=40, db_index=True)
    objeto_id = models.CharField(max_length=20, db_index=True)
    objeto_repr = models.CharField(max_length=200, blank=True)
    mudancas = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-quando", "-id"]
