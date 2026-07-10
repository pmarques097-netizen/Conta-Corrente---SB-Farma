# Projeto Negociação SB Farma - Streamlit Cloud

## Arquivo principal
Use exatamente:

```text
app.py
```

## O que subir para o GitHub
Suba os arquivos desta pasta, com `app.py` na raiz do repositório.
Não suba ZIP dentro do GitHub.

## Secrets do Streamlit Cloud
Copie o conteúdo de `.streamlit/secrets.toml.example` para:

Streamlit Cloud > App > Settings > Secrets

Depois troque host, database, user e password pelos dados reais.

## Publicação
1. Crie/reutilize o repositório no GitHub.
2. Envie todos os arquivos desta pasta para a raiz do repositório.
3. No Streamlit Cloud, selecione o repositório.
4. Main file path: `app.py`.
5. Configure os Secrets.
6. Clique em Reboot app e Clear cache.

## Observação importante
Os arquivos antigos de backup, `.pyc`, `.bat` e READMEs antigos foram removidos desta versão limpa para evitar erro, vazamento de senha antiga e confusão no deploy.
