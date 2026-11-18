from datetime import timedelta
from decimal import Decimal

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from . import services
from .models import (Categoria, CentroCusto, ContaFinanceira, Lancamento, MovimentoCaixa, Parceiro, q2)

DATA_FMT = ["%Y-%m-%d", "%d/%m/%Y"]


class BaseForm:
    """Aplica classes, datas HTML5 e decimais no formato brasileiro."""

    def _estilizar(self):
        for nome, f in self.fields.items():
            if isinstance(f, forms.DecimalField):
                f.localize = True
                f.widget = forms.TextInput(attrs={"inputmode": "decimal", "placeholder": "0,00"})
                f.widget.is_localized = True
            elif isinstance(f, forms.DateField):
                f.input_formats = DATA_FMT
                f.widget = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
            elif isinstance(f.widget, forms.CheckboxInput):
                continue
            cls = f.widget.attrs.get("class", "")
            f.widget.attrs["class"] = (cls + " campo").strip()


class F(BaseForm, forms.Form):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._estilizar()


class MF(BaseForm, forms.ModelForm):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._estilizar()


MODOS = [("unico", "Lançamento único"), ("parcelado", "Parcelado (divide o valor total)"),
         ("recorrente", "Recorrente mensal (repete o valor)")]


class LancamentoForm(MF):
    modo = forms.ChoiceField(choices=MODOS, initial="unico", required=False, label="Repetição")
    quantidade = forms.IntegerField(min_value=1, max_value=60, initial=1, required=False,
                                    label="Nº de parcelas / meses")

    class Meta:
        model = Lancamento
        fields = ["descricao", "parceiro", "categoria", "centro_custo", "conta", "valor", "vencimento",
                  "observacao", "anexo"]

    def __init__(self, *a, tipo=None, **kw):
        super().__init__(*a, **kw)
        self.tipo = tipo or (self.instance.tipo if self.instance.pk else Lancamento.RECEBER)
        self.fields["categoria"].queryset = Categoria.objects.filter(tipo=("R" if self.tipo == Lancamento.RECEBER else "D"), ativo=True)
        if self.tipo == Lancamento.RECEBER:
            self.fields["parceiro"].queryset = Parceiro.objects.filter(ativo=True, tipo__in=["cliente", "ambos"])
            self.fields["parceiro"].label = "Cliente"
        else:
            self.fields["parceiro"].queryset = Parceiro.objects.filter(ativo=True, tipo__in=["fornecedor", "ambos"])
            self.fields["parceiro"].label = "Fornecedor"
        self.fields["centro_custo"].queryset = CentroCusto.objects.filter(ativo=True)
        self.fields["conta"].queryset = ContaFinanceira.objects.filter(ativa=True)
        self.fields["conta"].required = False
        if self.instance.pk:
            del self.fields["modo"], self.fields["quantidade"]
            if self.instance.baixas.exists():
                self.fields["valor"].help_text = "Não pode ficar menor que o principal já baixado."

    def clean(self):
        d = super().clean()
        inst = self.instance
        if inst.pk and inst.periodo_fechado:
            raise ValidationError("Período fechado: este lançamento não pode ser editado.")
        if inst.pk and inst.status in (Lancamento.PAGA, Lancamento.CANCELADA):
            raise ValidationError("Lançamentos pagos ou cancelados não podem ser editados.")
        if inst.pk and d.get("valor") is not None and d["valor"] < inst.valor_pago:
            self.add_error("valor", "Valor menor que o principal já baixado.")
        if d.get("vencimento"):
            comp = d["vencimento"].replace(day=1)
            from .models import periodo_fechado
            if (not inst.pk or inst.competencia != comp) and periodo_fechado(comp):
                self.add_error("vencimento", "O mês deste vencimento está fechado.")
        return d

    def clean_anexo(self):
        a = self.cleaned_data.get("anexo")
        if a and hasattr(a, "size") and a.size > settings.LIMITE_ANEXO_BYTES:
            raise ValidationError("Anexo acima de 2 MB.")
        return a


class BaixaForm(F):
    data = forms.DateField(label="Data do pagamento/recebimento")
    conta = forms.ModelChoiceField(ContaFinanceira.objects.none(), label="Conta financeira")
    valor_principal = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), label="Valor principal")
    juros = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False, label="Juros")
    multa = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False, label="Multa")
    desconto = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False, label="Desconto")
    observacao = forms.CharField(max_length=200, required=False, label="Observação")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["conta"].queryset = ContaFinanceira.objects.filter(ativa=True)

    def clean(self):
        d = super().clean()
        for k in ("juros", "multa", "desconto"):
            d[k] = d.get(k) or Decimal("0.00")
        return d


class FiltroLancamentos(F):
    q = forms.CharField(required=False, label="Buscar", widget=forms.TextInput(attrs={"placeholder": "descrição ou nome"}))
    situacao = forms.ChoiceField(required=False, label="Situação", choices=[
        ("", "Todas"), ("aberta", "Abertas (inclui vencidas)"), ("vencida", "Vencidas"), ("parcial", "Parciais"),
        ("paga", "Pagas"), ("cancelada", "Canceladas")])
    categoria = forms.ModelChoiceField(Categoria.objects.all(), required=False, empty_label="Todas")
    conta = forms.ModelChoiceField(ContaFinanceira.objects.all(), required=False, empty_label="Todas")
    de = forms.DateField(required=False, label="Vencimento de")
    ate = forms.DateField(required=False, label="até")


class TransferenciaForm(F):
    origem = forms.ModelChoiceField(ContaFinanceira.objects.none())
    destino = forms.ModelChoiceField(ContaFinanceira.objects.none())
    valor = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    data = forms.DateField(initial=timezone.localdate)
    descricao = forms.CharField(max_length=160, required=False, label="Descrição")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        qs = ContaFinanceira.objects.filter(ativa=True)
        self.fields["origem"].queryset = self.fields["destino"].queryset = qs


class AbrirCaixaForm(F):
    conta = forms.ModelChoiceField(ContaFinanceira.objects.none(), label="Caixa")
    data = forms.DateField(initial=timezone.localdate)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["conta"].queryset = ContaFinanceira.objects.filter(tipo="caixa", ativa=True)


class MovimentoCaixaForm(F):
    tipo = forms.ChoiceField(choices=MovimentoCaixa.TIPOS)
    valor = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    motivo = forms.CharField(max_length=160, required=False)
    contraparte = forms.ModelChoiceField(ContaFinanceira.objects.none(), required=False,
                                         label="Conta de destino/origem (opcional)")

    def __init__(self, *a, conta=None, **kw):
        super().__init__(*a, **kw)
        self.fields["contraparte"].queryset = ContaFinanceira.objects.filter(ativa=True).exclude(pk=getattr(conta, "pk", 0))


class FecharCaixaForm(F):
    valor_contado = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False,
                                       label="Outros valores / total contado manual")
    observacao = forms.CharField(max_length=255, required=False, label="Observação (obrigatória se houver diferença)")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.denoms = []
        for d in services.DENOMINACOES:
            nome = "den_" + str(d).replace(".", "_")
            self.fields[nome] = forms.IntegerField(min_value=0, max_value=100000, required=False,
                                                   label=f"R$ {str(d).replace('.', ',')}",
                                                   widget=forms.NumberInput(attrs={"class": "campo mini", "min": 0}))
            self.denoms.append((d, self.fields[nome], nome))

    def contagem(self):
        c = {}
        for d, _, nome in self.denoms:
            v = self.cleaned_data.get(nome)
            if v:
                c[str(d)] = v
        return c

    def campos_denominacao(self):
        return [self[nome] for _, _, nome in self.denoms]


class ImportarExtratoForm(F):
    conta = forms.ModelChoiceField(ContaFinanceira.objects.none())
    arquivo = forms.FileField(help_text="CSV (data;descricao;valor) ou OFX. Máx. 1 MB.")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["conta"].queryset = ContaFinanceira.objects.filter(ativa=True).exclude(tipo="cartao")

    def clean_arquivo(self):
        a = self.cleaned_data["arquivo"]
        if a.size > settings.LIMITE_EXTRATO_BYTES:
            raise ValidationError("Arquivo acima de 1 MB.")
        if not a.name.lower().endswith((".csv", ".ofx", ".txt")):
            raise ValidationError("Use arquivo .csv ou .ofx.")
        return a


class ContaForm(MF):
    class Meta:
        model = ContaFinanceira
        fields = ["nome", "tipo", "instituicao", "saldo_inicial", "data_saldo_inicial", "ativa"]


class ParceiroForm(MF):
    class Meta:
        model = Parceiro
        fields = ["nome", "tipo", "documento", "email", "telefone", "ativo"]


class CategoriaForm(MF):
    class Meta:
        model = Categoria
        fields = ["codigo", "nome", "tipo", "grupo", "ativo"]


class CentroForm(MF):
    class Meta:
        model = CentroCusto
        fields = ["codigo", "nome", "ativo"]


class PeriodoForm(F):
    ano = forms.IntegerField(min_value=2000, max_value=2100)
    mes = forms.IntegerField(min_value=1, max_value=12)


class PeriodoRelatorioForm(F):
    """Filtros dos relatorios/dashboard."""
    de = forms.DateField(required=False, label="De")
    ate = forms.DateField(required=False, label="Até")
    conta = forms.ModelChoiceField(ContaFinanceira.objects.filter(ativa=True), required=False, empty_label="Todas as contas")
    categoria = forms.ModelChoiceField(Categoria.objects.all(), required=False, empty_label="Todas as categorias")
