# Projeto Negociação SB Farma - v14.9 Streamlit Cloud

Correções desta versão:
- bloqueio de consultas SQL longas no Streamlit Cloud para não derrubar o health check;
- timeout de banco reduzido para estabilidade;
- dependências Python 3.11 travadas;
- carga completa deve ser feita local/servidor dedicado ou por job externo.

No Streamlit Cloud use Python 3.11 e main file `app.py`.
