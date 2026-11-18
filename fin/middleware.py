import threading

from django.conf import settings
from django.shortcuts import redirect

_local = threading.local()


def usuario_atual():
    return getattr(_local, "user", None)


class UsuarioAtualMiddleware:
    """Disponibiliza o usuario da requisicao para a trilha de auditoria."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _local.user = getattr(request, "user", None)
        try:
            return self.get_response(request)
        finally:
            _local.user = None


class LoginObrigatorioMiddleware:
    """Tudo exige login, exceto saude, entrada, estaticos e admin (que tem login proprio)."""
    LIVRES = ("/saude/", "/entrar/", "/entrar-demo/", "/sair/", "/static/", "/admin/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and not request.path.startswith(self.LIVRES):
            return redirect(f"{settings.LOGIN_URL}?next={request.path}")
        return self.get_response(request)
