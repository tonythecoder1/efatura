# API de faturas PDF → CSV

API em Python/FastAPI que recebe uma fatura em PDF, analisa as páginas com a API da
OpenAI e acrescenta os dados comerciais a uma linha do CSV. Preparada para faturas
em português e inglês, incluindo PDFs digitalizados e faturas com várias páginas.

Não depende de um layout fixo. A precisão depende da legibilidade e da análise da
IA: não é possível garantir a extração correta de **qualquer** fatura. Campos ausentes
ou ambíguos ficam a `null`; avisos e `needs_review` indicam problemas detetados.
A ausência de avisos não garante que todos os valores estejam corretos.

## Arrancar

Requer Python 3.11+ e uma chave da API da OpenAI com acesso ao modelo configurado.

```bash
cd '/Users/antonyferreira/Documents/Invoice API'
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock -e '.[dev]'
cp .env.example .env
```

Preenche `OPENAI_API_KEY` no `.env` usando o teu editor. Não publiques este ficheiro.
Depois:

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Abre [a documentação interativa](http://127.0.0.1:8000/docs). Em
`POST /v1/invoices`, escolhe **Try it out**, seleciona o PDF e executa.

## Interface web

O frontend React/Vite está em `frontend/` e usa a API real. Num segundo terminal:

```bash
cd '/Users/antonyferreira/Documents/Invoice API/frontend'
npm install
cp .env.example .env
npm run dev
```

`VITE_API_URL` define a URL pública da API e, por defeito, aponta para
`http://127.0.0.1:8000`. Com PostgreSQL configurado, o frontend apresenta login,
categorias, centros de custo, uso do período gratuito e os planos Stripe. Permite
importar um CSV já criado, selecionar até 10 PDFs e escolher entre um CSV combinado
ou um CSV por transação. A chave da OpenAI permanece no backend.

As dependências e o ambiente `.venv` foram instalados durante a implementação.
`requirements.lock` regista as versões verificadas; `pyproject.toml` define os
intervalos suportados para futuras atualizações.

## Usar por HTTP

```bash
# Analisar e guardar uma fatura
curl --fail-with-body -X POST http://127.0.0.1:8000/v1/invoices \
  -F 'file=@/caminho/para/fatura.pdf;type=application/pdf'

# Descarregar o CSV acumulado
curl --fail-with-body http://127.0.0.1:8000/v1/invoices.csv -o faturas.csv

# Importar um CSV criado pela aplicação (linhas repetidas são ignoradas)
curl --fail-with-body -X POST http://127.0.0.1:8000/v1/invoices/import-csv \
  -F 'csv_file=@faturas.csv;type=text/csv'

# Processar até 10 PDFs num lote
curl --fail-with-body -X POST http://127.0.0.1:8000/v1/invoices/batch \
  -F 'files=@/caminho/fatura-1.pdf;type=application/pdf' \
  -F 'files=@/caminho/fatura-2.pdf;type=application/pdf' \
  -F 'mode=combined'

# Estado do serviço e presença de configuração de IA
curl http://127.0.0.1:8000/health

# Criar conta e iniciar sessão
curl --fail-with-body -X POST http://127.0.0.1:8000/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"tu@example.com","password":"uma-password-segura"}'
```

Se definires `API_KEY`, acrescenta `-H 'X-API-Key: a-tua-chave-local'` aos pedidos de
faturas ou usa **Authorize** no Swagger. Esta chave protege a tua API; é diferente
de `OPENAI_API_KEY`. O endpoint de saúde e a documentação continuam públicos.

| Endpoint | Resultado |
|---|---|
| `POST /v1/invoices` | `201` + dados extraídos e guardados; `200` se o PDF já existe |
| `POST /v1/invoices/import-csv` | Importa o CSV da aplicação e devolve linhas adicionadas e repetidas |
| `POST /v1/invoices/batch` | Processa 1–10 PDFs em modo `combined` ou `separate` |
| `GET /v1/invoices.csv` | CSV com todos os registos; só cabeçalho quando vazio |
| `POST /v1/auth/register` / `POST /v1/auth/login` | Criar conta e obter sessão |
| `GET /v1/billing/status` | Uso gratuito e plano ativo |
| `POST /v1/billing/checkout` | Criar checkout semanal ou mensal Stripe |
| `POST /v1/billing/webhook` | Atualizar a subscrição a partir do Stripe |
| `GET /health` | Estado, formatos aceites, limite do PDF e indicação de chave configurada |

A resposta do upload contém `duplicate` e `record`, incluindo avisos para revisão.
Esses metadados são devolvidos pela API, mas não são escritos no CSV.

## Conteúdo do CSV

Por defeito, o ficheiro é criado em `data/faturas.csv` no primeiro upload bem-sucedido.
Usa UTF-8 com BOM, separador `;` e cabeçalho único. Cada linha representa uma fatura
ou recibo e contém apenas os campos comerciais habituais:

- Tipo, número, datas de emissão e vencimento.
- Nome, NIF/VAT, morada e email do fornecedor e cliente.
- Moeda, subtotal, descontos, impostos, total e montante em dívida.
- Método de pagamento, IBAN, encomenda e observações.
- `categoria` e `centro_custo` para organização e contabilidade interna.
- `impostos`: discriminação dos impostos em texto legível.
- `outros_detalhes`: ATCUD, referências e outros dados comerciais identificados.

Ao importar o mesmo CSV ou uma linha já existente, a API compara todos os campos
comerciais e ignora a linha repetida. No modo de lote `combined`, os novos registos
ficam no CSV acumulado. No modo `separate`, a resposta inclui um CSV independente
para cada PDF processado.

O cabeçalho usa nomes em português: `tipo_documento`, `numero_fatura`, `data_emissao`,
`data_vencimento`, `nome_fornecedor`, `nif_fornecedor`, `morada_fornecedor`,
`email_fornecedor`, `nome_cliente`, `nif_cliente`, `morada_cliente`, `email_cliente`,
`moeda`, `subtotal`, `desconto_total`, `total_impostos`, `total`, `valor_em_divida`,
`metodo_pagamento`, `iban`, `ordem_compra`, `observacoes`, `impostos` e
`outros_detalhes`. Os artigos individuais não são exportados para o CSV.

Campos simples ausentes ficam vazios. Os valores decimais usam ponto, sem separador
de milhares: `1.234,56` / `1,234.56` → `1234.56`. Datas inequívocas usam `YYYY-MM-DD`.

O CSV não inclui ID interno, hash do PDF, nome do ficheiro, data de processamento,
nome do modelo, avisos de análise ou JSON técnico. Para evitar duplicados, a API
mantém um índice interno em `data/faturas.csv.index.json`; esse ficheiro não é
disponibilizado pelo endpoint de download.

Exemplo de leitura com Python:

```python
import csv
with open("data/faturas.csv", encoding="utf-8-sig", newline="") as file:
    for row in csv.DictReader(file, delimiter=";"):
        print(row["numero_fatura"], row["nome_fornecedor"], row["total"])
```

Para abrir num programa de folhas de cálculo, importa NIFs e referências como
**texto**, preservando zeros iniciais. Células que poderiam ser interpretadas como
fórmulas recebem um apóstrofo, incluindo montantes negativos.

## Configuração

| Variável | Valor inicial | Função |
|---|---|---|
| `OPENAI_API_KEY` | vazio | Chave obrigatória para análise real |
| `OPENAI_MODEL` | `gpt-6.1-sol` | Modelo com entrada PDF/visão e Structured Outputs |
| `API_KEY` | vazio | Autenticação opcional da API local |
| `CSV_PATH` | `data/faturas.csv` | Caminho do CSV, relativo ao diretório de execução |
| `MAX_UPLOAD_MB` | `20` | Limite do PDF (1 a 40 MiB) |
| `MAX_PAGES` | `30` | Número máximo de páginas por PDF |
| `OPENAI_TIMEOUT_SECONDS` | `120` | Timeout configurado no cliente HTTP do fornecedor |
| `FRONTEND_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Origens permitidas pelo CORS, separadas por vírgulas |
| `DATABASE_URL` | vazio | PostgreSQL persistente; injetado pelo Blueprint do Render |
| `AUTH_SECRET` | vazio | Segredo para assinar sessões de login |
| `FREE_INVOICE_LIMIT` | `10` | Faturas gratuitas por conta |
| `STRIPE_SECRET_KEY` | vazio | Chave privada do Stripe |
| `STRIPE_WEBHOOK_SECRET` | vazio | Assinatura do webhook Stripe |
| `STRIPE_MONTHLY_PRICE_ID` | vazio | Preço Stripe do plano mensal |
| `STRIPE_WEEKLY_PRICE_ID` | vazio | Preço Stripe do plano semanal |
| `APP_BASE_URL` | `http://localhost:5173` | URL para retorno do checkout |

O corpo multipart completo também é limitado a `MAX_UPLOAD_MB` + 1 MiB. O upload
é temporariamente colocado em memória/disco e eliminado no final do pedido.

## Comportamento e limites

- Uma transação por PDF; várias páginas e vários cartões da mesma reserva são aceites.
  Os valores dos cartões relacionados são somados. PDFs com transações independentes
  são rejeitados para evitar juntar dados diferentes.
- PDFs protegidos por palavra-passe, danificados, vazios ou acima dos limites são
  rejeitados antes da chamada à IA. A extensão e a estrutura do ficheiro são verificadas.
- PDFs com o mesmo conteúdo binário não duplicam o CSV, mesmo com outro nome.
  Cópias reexportadas com bytes diferentes também são deduplicadas quando mantêm
  o número da fatura e o fornecedor; documentos sem número usam uma combinação
  mais estrita de fornecedor, data, moeda, total e cliente.
- Os lotes aceitam entre 1 e 10 PDFs; cada PDF representa uma transação, podendo
  agrupar cartões de fatura da mesma reserva.
- A importação aceita apenas CSVs com o cabeçalho gerado pela aplicação e ignora linhas
  comerciais que já existam no ficheiro atual.
- CSVs criados por uma versão anterior são migrados automaticamente na próxima
  inicialização: as colunas técnicas são removidas e o índice interno é criado.
- Gravações usam bloqueio entre processos e substituição atómica do CSV. Chamadas
  simultâneas do mesmo PDF podem gerar duas análises pagas, mas apenas um registo.
- Falhas da IA não gravam linhas parciais. Extrações incompletas com campos `null`
  podem ser guardadas com `needs_review=true` para revisão posterior.
- A soma subtotal + impostos e a soma dos impostos discriminados são verificadas
  com `Decimal`. Diferenças acima de 0,02 geram avisos; não corrigimos a fatura.
- Com `DATABASE_URL`, os utilizadores, subscrições, categorias e faturas ficam numa
  base PostgreSQL persistente. Sem ela, a aplicação mantém o modo CSV local para
  desenvolvimento e testes.
- O serviço arranca sem chave, mas o upload devolve `503` até a chave ser configurada.
  Testes não fazem chamadas pagas. Não existe um modo de extração local nesta versão.

O PDF é enviado à OpenAI para análise e pode gerar custos na tua conta. A chamada
usa `store=False` e não cria um ficheiro persistente pela Files API; isso não é uma
garantia de retenção zero pelo fornecedor. O serviço conserva os dados extraídos
no CSV local. Para disponibilizar fora do computador, configurar autenticação,
HTTPS e limites de utilização no serviço de publicação.

Erros: `401` chave local inválida; `413` upload excessivo; `415` extensão não aceite;
`422` PDF/documento inválido; `502` análise recusada/incompleta/inválida; `503`
configuração, quota ou armazenamento indisponível; `504` timeout da IA.

## Verificação

```bash
python -m pytest -q
ruff check app tests
cd frontend && npm run build
```

Os testes verificam uploads, autenticação, CSV, Unicode, decimais, avisos, duplicados,
concorrência, falhas de escrita e de fornecedor. Usam PDFs sintéticos e respostas de
IA simuladas; um teste exercita o SDK real através de HTTP simulado. **Não medem a
precisão de extração em faturas reais**, que deve ser avaliada com documentos de
exemplo e uma chave configurada.

Referências da implementação: [entrada de PDFs](https://developers.openai.com/api/docs/guides/file-inputs),
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs) e
[modelo configurado](https://developers.openai.com/api/docs/models/gpt-6.1-sol).
