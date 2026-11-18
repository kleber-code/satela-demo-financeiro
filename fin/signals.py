from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .middleware import usuario_atual
from .models import (AuditLog, Baixa, Lancamento, MovimentoCaixa, PeriodoFechado, SessaoCaixa,
                     Transferencia)

pausado = False  # usado pelo seed_demo para nao poluir a trilha
AUDITADOS = (Lancamento, Baixa, Transferencia, SessaoCaixa, MovimentoCaixa, PeriodoFechado)


def _registrar(acao, instance, mudancas):
    u = usuario_atual()
    if u is not None and not getattr(u, "is_authenticated", False):
        u = None
    AuditLog.objects.create(
        usuario=u, usuario_nome=(u.get_username() if u else "sistema"), acao=acao,
        modelo=instance.__class__.__name__, objeto_id=str(instance.pk),
        objeto_repr=str(instance)[:200], mudancas=mudancas)


@receiver(post_save)
def auditar_save(sender, instance, created, **kw):
    if pausado or sender not in AUDITADOS:
        return
    novo = instance._snapshot()
    if created:
        _registrar("criou", instance, {k: ["", v] for k, v in novo.items() if v != ""})
        return
    antes = getattr(instance, "_antes", None) or {}
    dif = {k: [antes.get(k, ""), v] for k, v in novo.items() if antes.get(k, "") != v}
    if dif:
        _registrar("alterou", instance, dif)


@receiver(post_delete)
def auditar_delete(sender, instance, **kw):
    if not pausado and sender in AUDITADOS:
        _registrar("excluiu", instance, instance._snapshot())
