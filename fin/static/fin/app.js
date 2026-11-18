(function () {
  var r = document.documentElement;
  function setTema(t) { r.setAttribute('data-theme', t); try { localStorage.setItem('fin-tema', t); } catch (e) {} }
  try { var t = localStorage.getItem('fin-tema'); if (t) r.setAttribute('data-theme', t); } catch (e) {}
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-tema]');
    if (b) {
      var atual = r.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
      setTema(atual === 'dark' ? 'light' : 'dark');
    }
    if (e.target.closest('[data-menu]')) document.body.classList.toggle('menu-aberto');
    var p = e.target.closest('[data-imprimir]');
    if (p) window.print();
    var c = e.target.closest('[data-confirma]');
    if (c && !confirm(c.getAttribute('data-confirma'))) e.preventDefault();
  });
  // aplica tema salvo antes do render quando o script e carregado no head
})();
