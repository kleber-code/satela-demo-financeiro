# Caixa & Finanças — financeiro e caixa da empresa (demonstração)

Sistema web de **financeiro e caixa** para uma empresa de serviços de TI (empresa **fictícia**, "Nimbus Soluções Digitais"), feito em Django 5 para servir como vitrine de portfólio. Tudo em português do Brasil, valores em R$ e **`Decimal` em todo cálculo monetário**. Não há nenhuma integração financeira real: boleto e Pix são **simulações** claramente marcadas.

## O que tem

- **Cadastros**: plano de contas (categorias de receita/despesa agrupadas para a DRE), centros de custo, contas financeiras (caixa, banco, cartão), clientes e fornecedores.
- **Contas a receber e a pagar**: vencimento, parcelamento (a soma das parcelas é sempre igual ao total, centavos sobrantes vão para as primeiras), recorrência mensal, baixa total ou parcial com **juros (1% a.m. pro rata) e multa (2%)** sugeridos e editáveis, desconto, estorno, status (aberta, parcial, paga, vencida, cancelada) e anexo opcional (PDF/imagem até 2 MB).
- **Caixa do dia**: abertura (saldo contábil do dia anterior), sangria e suprimento (opcionalmente movimentando outra conta), **conferência por cédulas/moedas**, cálculo da diferença (justificativa obrigatória) e fechamento imutável. Transferências entre contas.
- **Conciliação bancária** por importação de CSV (`data;descricao;valor`, também crédito/débito) ou OFX, sem duplicar linhas reimportadas, com **sugestão de pareamento** (mesmo valor, mesma conta, data até 5 dias) e conciliação automática das sugestões de alta confiança.
- **Relatórios** (todos com CSV e versão para impressão/PDF via CSS print): fluxo de caixa realizado e projetado (90 dias), DRE por período, aging a receber/pagar (a vencer, 1–30, 31–60, 60+), inadimplência, top clientes/fornecedores, comparativo mês a mês.
- **Dashboard** com KPIs e gráficos SVG/CSS próprios (sem CDN), filtros por período, conta e categoria.
- **Cobrança simulada** (boleto e Pix **FAKE**; o "QR" é um desenho de exemplo não funcional).
- **Papéis**: administrador, financeiro, somente leitura. **Trilha de auditoria** (quem, o quê, quando, valor antes/depois) em lançamentos, baixas, transferências, caixas e fechamentos. **Períodos fechados** ficam imutáveis (lançamentos, baixas e transferências); só o administrador reabre.
- UI própria, responsiva, tema claro/escuro, acessível (skip link, foco visível, contraste), destaque verde `#1b7a55`.

## Como rodar localmente

```bash
python3 -m venv /tmp/financeiro-venv && . /tmp/financeiro-venv/bin/activate
pip install -r requirements.txt
export DEBUG=1                      # usa SQLite local (db.sqlite3) e chave de desenvolvimento
python manage.py migrate
python manage.py seed_demo          # idempotente; --reset recria; --se-vazio só popula se vazio
python manage.py runserver
python manage.py test               # 37 testes
```

Abra http://127.0.0.1:8000/ e clique em **Entrar na demonstração**.

`seed_demo` cria 12 meses de movimentos coerentes (~560 lançamentos, ~440 baixas, 43 caixas diários, fatura de cartão paga todo mês, aportes na reserva, inadimplentes e títulos vencidos em todas as faixas, extrato para conciliar, meses antigos fechados). Os saldos nunca ficam negativos nem absurdos (teste automatizado verifica).

### Docker / produção (Satela)

- `Dockerfile` (python:3.12-slim, usuário não root, `collectstatic` no build, escuta `$PORT`, padrão 8000).
- `Procfile`: `release` roda `migrate` + `seed_demo --se-vazio`; `web` roda gunicorn com 2 workers.
- `ENV` traz `HEALTHCHECK_PATH=/saude/` (responde `200 ok` sem login).

## Variáveis de ambiente

| Variável | Padrão | Função |
|---|---|---|
| `DJANGO_SECRET_KEY` | efêmera se vazia | Chave secreta (defina em produção) |
| `DEBUG` | `0` | `1` ativa debug, cookies sem Secure |
| `DJANGO_ALLOWED_HOSTS` | `.satela.com.br,localhost,127.0.0.1` | Hosts aceitos (vírgula) |
| `CSRF_TRUSTED_ORIGINS` | derivado dos hosts (`https://*.satela.com.br`) | Origens confiáveis |
| `DATABASE_URL` | SQLite local | Postgres em produção (`postgres://...`) |
| `DEMO_MODE` | `1` | Botão "Entrar na demonstração" (usuário `financeiro`), faixa de aviso |
| `DEMO_ADMIN_PASSWORD` | vazio | Se definida, cria/atualiza o superusuário `admin` (vazia = não cria) |
| `SECURE_SSL_REDIRECT` | `0` | Redirecionar para HTTPS (o proxy da plataforma já termina TLS) |
| `SECURE_HSTS_SECONDS` | `31536000` | HSTS fora do debug |

Atrás de proxy, `SECURE_PROXY_SSL_HEADER` usa `X-Forwarded-Proto`. Cookies de sessão e CSRF são `Secure` fora do debug. Uploads vão para `/data/media` se `/data` existir, senão `./media`, e só são servidos por rota autenticada. Nenhum e-mail é enviado (backend `dummy`). Nenhum segredo está no repositório.

## Rotas principais

`/` dashboard · `/receber/` · `/pagar/` · `/lancamentos/<id>/` · `/caixa/` · `/transferencias/` · `/conciliacao/` · `/relatorios/` (`fluxo-de-caixa`, `dre`, `aging`, `inadimplencia`, `ranking`, `comparativo`; `?formato=csv`) · `/contas/` · `/parceiros/` · `/plano-de-contas/` · `/periodos/` · `/auditoria/` · `/saude/` · `/admin/` (só superusuário).

## Roteiro de demonstração (2 minutos)

1. **Entrar** com um clique (perfil Financeiro). Mostre a faixa "Ambiente de demonstração" e o **dashboard**: saldo, inadimplência, projeção de 90 dias, gráficos (0:00–0:30).
2. **Contas a receber** → filtre "Vencidas" → abra um título de 90+ dias → **gerar boleto/Pix simulado** (note a marca SIMULAÇÃO) → **Registrar recebimento**: veja multa e juros calculados; faça uma baixa parcial (0:30–1:00).
3. **Caixa do dia**: o caixa de hoje está aberto; registre uma sangria, clique em **Conferir e fechar**, informe a contagem de cédulas e veja a diferença (1:00–1:25).
4. **Conciliação**: veja o extrato com sugestões e clique em "Conciliar sugestões automáticas" (1:25–1:40).
5. **Relatórios**: DRE e aging; exporte CSV e use "Imprimir / PDF" (1:40–1:50).
6. **Períodos / Auditoria**: abra um lançamento de mês fechado e tente editar (bloqueado); mostre a trilha com valor antes/depois. Volte à tela de entrada e use "Ver como somente leitura" para mostrar os papéis (1:50–2:00).

## Limitações honestas

- Boleto, Pix e QR são só simulação; não há integração bancária, emissão fiscal nem Open Finance.
- Conciliação simples: casa por valor/data/conta (sem divisão de lançamentos, sem regras aprendidas); OFX aceita apenas o essencial (`STMTTRN`).
- Rate limit de login usa cache em memória por processo (cada worker conta separado; reinicia ao reiniciar).
- Modo demonstração permite escrita ao perfil Financeiro: se a instância for pública, qualquer visitante altera os dados (use `--reset` para restaurar). Nesse caso considere `DEMO_MODE=0`.
- Juros/multa seguem uma regra fixa (2% + 1% a.m. pro rata); não há tabela por contrato.
- Regime de DRE: competência para receitas/despesas e caixa para juros/multas; sem rateio entre centros de custo nem impostos reais.
- Sem multiempresa, sem moedas, sem backup/restauração de anexos além do volume `/data`.
