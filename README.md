# SB Farma Enterprise V2

Versão profissional do projeto de Gestão de Negociações, preparada para virar produto comercial.

## Arquitetura

- **Backend:** FastAPI
- **Banco do app:** SQLite local por padrão ou PostgreSQL via `DATABASE_URL`
- **Frontend:** React + Bootstrap, servido pelo próprio FastAPI
- **Importação:** conexão PostgreSQL do ERP configurável pela tela de Configurações
- **Sem Streamlit:** elimina o erro `removeChild` e dependência do frontend do Streamlit Cloud.

## Como executar localmente

### Windows

1. Extraia o ZIP.
2. Execute:

```bat
executar_windows.bat
```

3. Acesse:

```text
http://127.0.0.1:8000
```

### Manual

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate # Linux/Mac
pip install -r requirements.txt
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
```

## Publicação em servidor

Defina as variáveis:

```env
DATABASE_URL=postgresql+psycopg://usuario:senha@host:5432/banco
APP_SECRET_KEY=troque-esta-chave
```

Se `DATABASE_URL` não for informado, o sistema usa SQLite em `data/sbfarma_enterprise.db`.

## Funcionalidades incluídas

- Dashboard executivo
- Cadastro de negociações
- Tipos de negociação abrangentes
- Produtos por negociação
- Faixa progressiva não cumulativa
- Apuração por cache/resumo
- Extrato financeiro estilo bancário
- Conta corrente por fabricante/fornecedor
- Lançamentos avulsos como negociação financeira
- Configuração do banco ERP com teste de conexão
- Importação de Entradas e Vendas via SQL
- Relatórios básicos em JSON/CSV
- Auditoria de ações

## Observação importante

Esta versão é uma base Enterprise pronta para executar e evoluir. O motor de importação aceita os scripts `sql/ENTRADAS_SB.sql` e `sql/VENDAS_SB.sql`, copiados do projeto Streamlit quando disponíveis.
