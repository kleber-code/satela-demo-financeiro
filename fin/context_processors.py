from django.conf import settings

from .models import Perfil, papel_de


def global_ctx(request):
    papel = papel_de(getattr(request, "user", None))
    return {
        "DEMO_MODE": settings.DEMO_MODE,
        "papel": papel,
        "pode_escrever": papel in (Perfil.ADMIN, Perfil.FINANCEIRO),
        "eh_admin": papel == Perfil.ADMIN,
        "papel_label": dict(Perfil.PAPEIS).get(papel, ""),
    }
