# SB Farma Negociação - v12.8 Streamlit Cloud

## Como publicar no Streamlit Cloud

1. Suba a pasta do projeto para um repositório GitHub.
2. No Streamlit Cloud, selecione o arquivo principal:

```text
app.py
```

3. Em **Settings > Secrets**, cadastre:

```toml
[app]
mode = "cloud"
cloud = true
allow_scheduler = false

[postgres]
host = "SEU_HOST"
port = "5432"
database = "SEU_BANCO"
user = "SEU_USUARIO"
password = "SUA_SENHA"
```

4. O arquivo `requirements.txt` já contém as dependências necessárias.

## Pontos importantes

- No Streamlit Cloud não use `.bat`.
- Rotina automática às 04h/hora em hora deve ser feita por job externo ou botão manual no app.
- O cache local do Streamlit Cloud pode ser temporário. Para produção, mantenha dados críticos em banco externo.
- Comprovantes/anexos em pasta local podem ser apagados em reinicializações do Cloud. Para uso corporativo, o ideal é salvar em storage externo ou banco.

## Modo local

O mesmo projeto continua funcionando localmente. Para usar localmente, execute:

```bash
pip install -r requirements.txt
streamlit run app.py
```

Ou use os `.bat` no Windows, se preferir.


## Correção obrigatória no Streamlit Cloud

No painel do Streamlit Cloud, abra **Settings > Python version** e selecione **Python 3.11** ou **Python 3.12**.

O Streamlit Cloud pode ignorar `runtime.txt` e iniciar automaticamente em Python 3.14. Quando isso acontece, dependências como pandas/psycopg podem falhar durante a instalação.

Configuração recomendada:

- Main file path: `app.py`
- Python version: `3.11`
- Após alterar: clique em **Reboot app**
