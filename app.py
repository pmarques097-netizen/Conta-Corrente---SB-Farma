import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import sqlite3
import os
import re
import tempfile
from contextlib import contextmanager
import io
import base64
import zipfile
import shutil
from pathlib import Path
from datetime import date, datetime, time, timedelta
import threading
import time as time_module
import hashlib
import inspect


def _import_postgres_driver():
    """Importa driver PostgreSQL compatível com Streamlit Cloud.
    Preferência: psycopg2 quando disponível; fallback: psycopg v3.
    """
    try:
        import psycopg2  # type: ignore
        return psycopg2
    except Exception:
        try:
            import psycopg  # type: ignore
            return psycopg
        except Exception as exc:
            raise RuntimeError(
                "Driver PostgreSQL não instalado. No Streamlit Cloud, use Python 3.11/3.12 e requirements.txt atualizado."
            ) from exc

APP_DIR = Path(__file__).parent


def _secrets_section(nome):
    """Lê uma seção de st.secrets sem quebrar execução local."""
    try:
        sec = st.secrets.get(nome, {})
        return dict(sec) if sec else {}
    except Exception:
        return {}


def _truthy(v):
    return str(v).strip().lower() in {'1', 'true', 'sim', 'yes', 'on', 'cloud'}


APP_SECRETS = _secrets_section('app')
POSTGRES_SECRETS = _secrets_section('postgres')
def _is_streamlit_cloud_env():
    cwd = str(Path.cwd()).replace('\\','/')
    home = str(Path.home()).replace('\\','/')
    return (
        _truthy(os.environ.get('SB_FARMA_CLOUD'))
        or _truthy(APP_SECRETS.get('cloud'))
        or str(APP_SECRETS.get('mode', '')).strip().lower() == 'cloud'
        or cwd.startswith('/mount/src')
        or home.startswith('/home/appuser')
        or bool(os.environ.get('STREAMLIT_SERVER_PORT'))
    )

CLOUD_MODE = _is_streamlit_cloud_env()


def _config_val(section, key, env_name, default=''):
    if section.get(key) not in (None, ''):
        return str(section.get(key))
    return str(os.environ.get(env_name, default) or default)


# Dados persistentes fora da pasta do código.
# Local: mantém em pasta fixa do usuário.
# Streamlit Cloud: usa uma pasta do próprio app, evitando perda a cada reload da página.
# Observação: no Streamlit Community Cloud, arquivos locais ainda podem ser limpos em redeploy/reboot;
# para persistência definitiva use um storage/banco externo.
def _default_data_dir():
    env_dir = os.environ.get('SB_FARMA_DATA_DIR') or os.environ.get('SB_FARMA_DATA') or APP_SECRETS.get('data_dir')
    if env_dir:
        return Path(str(env_dir)).expanduser()
    if CLOUD_MODE:
        # NÃO usar /tmp no Cloud: /tmp é removido com facilidade entre reinicializações.
        # Usamos a pasta do app para manter os dados durante o ciclo de vida do container.
        return APP_DIR / '.sb_farma_dados'
    if os.name == 'nt':
        return Path.home() / 'SB_Farma_Negociacao_Dados'
    return Path.home() / 'sb_farma_negociacao_dados'

DATA_DIR = _default_data_dir()
CACHE_DIR = DATA_DIR / 'cache'
DB_DIR = DATA_DIR / 'db'
BACKUP_DIR = DATA_DIR / 'backups'
for _d in (DATA_DIR, CACHE_DIR, DB_DIR, BACKUP_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = DB_DIR / 'negociacao_investimentos.db'
CACHE_PARQUET = CACHE_DIR / 'entradas_cache.parquet'
CACHE_PICKLE = CACHE_DIR / 'entradas_cache.pkl'
SQL_PATH = APP_DIR / 'ENTRADAS_SB.sql'
SQL_VENDAS_PATH = APP_DIR / 'VENDAS_SB.sql'
CACHE_VENDAS_PARQUET = CACHE_DIR / 'vendas_cache.parquet'
CACHE_VENDAS_PICKLE = CACHE_DIR / 'vendas_cache.pkl'

PERF_LOG_PATH = CACHE_DIR / 'performance_telas.log'
PERF_WARN_SECONDS = float(os.environ.get('SB_FARMA_PERF_WARN_SECONDS', '0.8'))


def registrar_performance(etapa, segundos, detalhe=''):
    """Registra tempos de tela sem impactar a experiência do usuário.

    A navegação deve ser leve; quando alguma tela passar do limite configurado,
    deixamos evidência em cache/performance_telas.log para auditoria técnica.
    """
    try:
        PERF_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        linha = f"{datetime.now():%Y-%m-%d %H:%M:%S};{etapa};{segundos:.3f};{str(detalhe).replace(chr(10),' ')}\n"
        with open(PERF_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(linha)
    except Exception:
        pass


@contextmanager
def medir_performance(etapa, detalhe=''):
    inicio = time_module.perf_counter()
    try:
        yield
    finally:
        segundos = time_module.perf_counter() - inicio
        if segundos >= PERF_WARN_SECONDS:
            registrar_performance(etapa, segundos, detalhe)

def migrar_dados_locais_para_pasta_fixa():
    """Migra dados das versões antigas para pasta persistente sem apagar a origem."""
    import shutil
    candidatos = [
        (APP_DIR / 'negociacao_investimentos.db', DB_PATH),
        (APP_DIR / 'database.db', DB_PATH),
        (APP_DIR / 'projeto_negociacao.db', DB_PATH),
        (APP_DIR / 'cache' / 'entradas_cache.parquet', CACHE_PARQUET),
        (APP_DIR / 'cache' / 'entradas_cache.pkl', CACHE_PICKLE),
        (APP_DIR / 'cache_entradas.parquet', CACHE_PARQUET),
        (APP_DIR / 'cache_entradas.pkl', CACHE_PICKLE),
    ]
    for origem, destino in candidatos:
        try:
            if origem.exists() and not destino.exists():
                destino.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origem, destino)
        except Exception:
            pass

migrar_dados_locais_para_pasta_fixa()

DEFAULT_DB_CONFIG = {
    'host': _config_val(POSTGRES_SECRETS, 'host', 'POSTGRES_HOST', ''),
    'port': _config_val(POSTGRES_SECRETS, 'port', 'POSTGRES_PORT', '5432'),
    'database': _config_val(POSTGRES_SECRETS, 'database', 'POSTGRES_DATABASE', ''),
    'user': _config_val(POSTGRES_SECRETS, 'user', 'POSTGRES_USER', ''),
    'password': _config_val(POSTGRES_SECRETS, 'password', 'POSTGRES_PASSWORD', ''),
}


# -----------------------------------------------------------------------------
# Persistência real para Streamlit Cloud
# -----------------------------------------------------------------------------
# No Streamlit Cloud, arquivos locais podem ser perdidos em reboot/redeploy.
# Para evitar perda das negociações/financeiro/configurações, o app salva o
# SQLite local (e comprovantes) em uma tabela PostgreSQL externa.
# Ative com st.secrets:
# [app]
# cloud = true
# state_sync = true
#
# A mesma conexão [postgres] já usada para ler o banco SB Farma é reutilizada.
# Se desejar separar o banco de persistência, use [app_state_postgres].
APP_STATE_SECRETS = _secrets_section('app_state_postgres') or POSTGRES_SECRETS
APP_STATE_SYNC = CLOUD_MODE and _truthy(APP_SECRETS.get('state_sync', 'false'))
APP_STATE_TABLE = str(APP_SECRETS.get('state_table', 'sb_farma_app_state'))
APP_STATE_KEY = str(APP_SECRETS.get('state_key', 'projeto_negociacao_investimento'))
APP_STATE_MAX_MB = float(APP_SECRETS.get('state_max_mb', 80))
APP_STATE_MIN_INTERVAL = int(APP_SECRETS.get('state_sync_interval_seconds', 20))
APP_STATE_MARKER = DATA_DIR / '.cloud_state_marker.txt'
APP_STATE_SYNC_LOG = CACHE_DIR / 'cloud_state_sync.log'
DB_CONFIG_FILE = DATA_DIR / 'db_config_salva.json'


def _read_saved_db_config_file():
    try:
        import json
        if DB_CONFIG_FILE.exists():
            return json.loads(DB_CONFIG_FILE.read_text(encoding='utf-8')) or {}
    except Exception:
        pass
    return {}


def _write_saved_db_config_file(host, port, database, user, password):
    try:
        import json
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {'host': str(host or '').strip(), 'port': str(port or '5432').strip(), 'database': str(database or '').strip(), 'user': str(user or '').strip(), 'password': str(password or '')}
        DB_CONFIG_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass



def _state_pg_config():
    saved = _read_saved_db_config_file()
    return {
        'host': _config_val(APP_STATE_SECRETS, 'host', 'APP_STATE_POSTGRES_HOST', DEFAULT_DB_CONFIG.get('host', '') or saved.get('host', '')),
        'port': _config_val(APP_STATE_SECRETS, 'port', 'APP_STATE_POSTGRES_PORT', str(DEFAULT_DB_CONFIG.get('port', '5432') or saved.get('port', '5432'))),
        'database': _config_val(APP_STATE_SECRETS, 'database', 'APP_STATE_POSTGRES_DATABASE', DEFAULT_DB_CONFIG.get('database', '') or saved.get('database', '')),
        'user': _config_val(APP_STATE_SECRETS, 'user', 'APP_STATE_POSTGRES_USER', DEFAULT_DB_CONFIG.get('user', '') or saved.get('user', '')),
        'password': _config_val(APP_STATE_SECRETS, 'password', 'APP_STATE_POSTGRES_PASSWORD', DEFAULT_DB_CONFIG.get('password', '') or saved.get('password', '')),
    }


def _state_pg_available():
    cfg = _state_pg_config()
    return bool(cfg.get('host') and cfg.get('database') and cfg.get('user') and cfg.get('password'))


@contextmanager
def cloud_state_pg_conn():
    psycopg2 = _import_postgres_driver()
    cfg = _state_pg_config()
    conn = psycopg2.connect(
        host=cfg['host'],
        port=cfg.get('port', '5432'),
        dbname=cfg['database'],
        user=cfg['user'],
        password=cfg['password'],
        connect_timeout=15,
    )
    try:
        yield conn
    finally:
        conn.close()


def _log_cloud_state(msg):
    try:
        APP_STATE_SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(APP_STATE_SYNC_LOG, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}\n")
    except Exception:
        pass


def _ensure_state_table(cur):
    safe_table = re.sub(r'[^a-zA-Z0-9_]', '', APP_STATE_TABLE) or 'sb_farma_app_state'
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {safe_table} (
            state_key TEXT PRIMARY KEY,
            payload BYTEA NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            size_bytes BIGINT NOT NULL DEFAULT 0,
            app_version TEXT
        )
    """)
    return safe_table


def _zip_local_state_bytes():
    """Compacta somente o estado do app: SQLite + comprovantes/documentos.

    Não inclui caches grandes de compras/vendas, pois esses podem ser recriados
    pela atualização automática/manual a partir do PostgreSQL da SB Farma.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        if DB_PATH.exists():
            z.write(DB_PATH, arcname='db/negociacao_investimentos.db')
        if DB_CONFIG_FILE.exists():
            z.write(DB_CONFIG_FILE, arcname='db_config_salva.json')
        for folder_name in ['documentos', 'documentos_financeiros']:
            folder = DATA_DIR / folder_name
            if folder.exists():
                for f in folder.rglob('*'):
                    if f.is_file():
                        z.write(f, arcname=str(f.relative_to(DATA_DIR)))
    buffer.seek(0)
    return buffer.getvalue()


def restaurar_estado_cloud_se_necessario(force=False):
    """Restaura estado persistido no PostgreSQL quando o container Cloud reinicia."""
    if not APP_STATE_SYNC or not _state_pg_available():
        return False
    if (not force) and DB_PATH.exists() and DB_PATH.stat().st_size > 0:
        return False
    try:
        with cloud_state_pg_conn() as conn:
            with conn.cursor() as cur:
                table = _ensure_state_table(cur)
                cur.execute(f"SELECT payload, updated_at FROM {table} WHERE state_key=%s", (APP_STATE_KEY,))
                row = cur.fetchone()
                conn.commit()
        if not row:
            return False
        payload, updated_at = row
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(bytes(payload)), 'r') as z:
            z.extractall(DATA_DIR)
        try:
            APP_STATE_MARKER.write_text(str(updated_at), encoding='utf-8')
        except Exception:
            pass
        _log_cloud_state(f'estado restaurado do PostgreSQL em {DATA_DIR}')
        return True
    except Exception as e:
        _log_cloud_state(f'falha ao restaurar estado: {e}')
        return False


def sincronizar_estado_cloud(force=False):
    """Envia o estado local do app ao PostgreSQL, protegendo contra perda no Cloud."""
    if not APP_STATE_SYNC or not _state_pg_available():
        return False
    try:
        now_ts = time_module.time()
        last = float(st.session_state.get('_cloud_state_last_sync_ts', 0) or 0)
        if not force and (now_ts - last) < APP_STATE_MIN_INTERVAL:
            return False
        if not DB_PATH.exists():
            return False
        mtime = DB_PATH.stat().st_mtime_ns
        last_mtime = st.session_state.get('_cloud_state_last_db_mtime')
        if not force and last_mtime == mtime:
            return False
        payload = _zip_local_state_bytes()
        size_mb = len(payload) / (1024 * 1024)
        if size_mb > APP_STATE_MAX_MB:
            _log_cloud_state(f'estado não sincronizado: {size_mb:.1f}MB maior que limite {APP_STATE_MAX_MB}MB')
            return False
        with cloud_state_pg_conn() as conn:
            with conn.cursor() as cur:
                table = _ensure_state_table(cur)
                cur.execute(f"""
                    INSERT INTO {table} (state_key, payload, updated_at, size_bytes, app_version)
                    VALUES (%s, %s, CURRENT_TIMESTAMP, %s, %s)
                    ON CONFLICT (state_key) DO UPDATE SET
                        payload = EXCLUDED.payload,
                        updated_at = CURRENT_TIMESTAMP,
                        size_bytes = EXCLUDED.size_bytes,
                        app_version = EXCLUDED.app_version
                """, (APP_STATE_KEY, payload, len(payload), 'v14.3 Cloud Definitivo'))
                conn.commit()
        st.session_state['_cloud_state_last_sync_ts'] = now_ts
        st.session_state['_cloud_state_last_db_mtime'] = mtime
        _log_cloud_state(f'estado sincronizado: {size_mb:.2f}MB')
        return True
    except Exception as e:
        _log_cloud_state(f'falha ao sincronizar estado: {e}')
        return False
SCHEDULE_HOUR = 4
SCHEDULE_MINUTE = 0
FULL_SYNC_START_DATE = date(2025, 1, 1)
HOURLY_SYNC_MINUTE = 5  # roda uma vez por hora, próximo ao minuto 05

st.set_page_config(page_title='SB Farma | Projeto Negociação v14.5 Streamlit Cloud', page_icon='💊', layout='wide')

CSS = '''
<style>
:root{
  --sb-bg:#070B12;
  --sb-panel:#0D1421;
  --sb-panel-2:#111B2C;
  --sb-blue:#123A56;
  --sb-blue-2:#0E2F48;
  --sb-cream:#EDE3D4;
  --sb-text:#F8FAFC;
  --sb-muted:#94A3B8;
  --sb-green:#00A86B;
  --sb-red:#FF4B4B;
  --sb-border:rgba(237,227,212,.16);
}
html, body, .stApp, [data-testid="stAppViewContainer"] { translate: no; }
.stApp {background: radial-gradient(circle at top left, rgba(18,58,86,.22), transparent 34%), var(--sb-bg); color: var(--sb-text);}
.block-container {padding-top: 1.15rem; padding-bottom: 2rem; max-width: 1500px;}
section[data-testid="stSidebar"] {background: linear-gradient(180deg,#08111F 0%,#0D1421 100%); border-right:1px solid var(--sb-border);}
section[data-testid="stSidebar"] .stRadio label {font-weight:700;}
.sb-header{background:linear-gradient(135deg,rgba(18,58,86,.95),rgba(8,17,31,.98)); border:1px solid var(--sb-border); border-radius:22px; padding:22px 26px; margin-bottom:22px; box-shadow:0 12px 34px rgba(0,0,0,.28);}
.sb-logo{max-width:310px; width:32%; min-width:210px; display:block; margin-bottom:14px;}
.main-title {font-size: 2.0rem; font-weight: 900; margin-bottom: .15rem; letter-spacing:.2px; color:var(--sb-cream);}
.subtitle {color: var(--sb-muted); margin-bottom: .5rem; font-size:1rem;}
.sb-chip{display:inline-flex; gap:8px; align-items:center; background:rgba(237,227,212,.08); color:var(--sb-cream); border:1px solid var(--sb-border); padding:8px 12px; border-radius:999px; font-size:.86rem; margin-top:6px;}
.card {background: linear-gradient(180deg,rgba(17,27,44,.94),rgba(13,20,33,.98)); border: 1px solid var(--sb-border); border-radius: 20px; padding: 18px; box-shadow: 0 10px 26px rgba(0,0,0,.22);}
.metric-card {background: linear-gradient(135deg, #102C45, #0B1728); color: white; border:1px solid rgba(237,227,212,.14); border-radius: 22px; padding: 22px; box-shadow: 0 12px 32px rgba(0,0,0,.25);}
.metric-label {font-size: .85rem; opacity: .78; color:var(--sb-cream);}
.metric-value {font-size: 1.72rem; font-weight: 900; color:#fff;}
.stButton > button, .stDownloadButton > button {border-radius:12px !important; border:1px solid rgba(237,227,212,.24) !important; background:linear-gradient(135deg,#123A56,#0E2F48) !important; color:#fff !important; font-weight:800 !important;}
.stButton > button:hover, .stDownloadButton > button:hover {border-color:var(--sb-cream) !important; filter:brightness(1.08);}
div[data-testid="stAlert"] {border-radius:14px; border:1px solid var(--sb-border);}
[data-testid="stDataFrame"] {border-radius:16px; overflow:hidden; border:1px solid var(--sb-border);}
hr {border-color:var(--sb-border);}
.sb-footer{color:var(--sb-muted); text-align:center; margin-top:26px; padding:12px; border-top:1px solid var(--sb-border); font-size:.85rem;}
</style>
'''
st.markdown(CSS, unsafe_allow_html=True)


def instalar_guard_dom_cloud():
    """Guard Cloud desativado na v14.5.

    A versão anterior injetava JavaScript com components.html para mexer no
    documento pai. No Streamlit Cloud atual isso gera avisos e pode provocar
    a tela branca/OH NO no frontend. Mantemos somente CSS/HTML estável abaixo.
    """
    return

instalar_guard_dom_cloud()


# -----------------------------------------------------------------------------
# V13.7 - Cloud notranslate guard
# -----------------------------------------------------------------------------
# O erro de frontend "removeChild" no Streamlit Cloud é frequentemente disparado
# quando o Chrome/Google Translate ou extensões alteram o DOM gerenciado pelo React.
# Este bloco marca a aplicação como "notranslate" no documento pai para impedir
# que tradutores modifiquem os nós da interface durante os reruns do Streamlit.
def aplicar_guard_notranslate_cloud():
    """Guarda leve contra tradução sem injetar JS no DOM pai.

    Versões anteriores usavam components.html para alterar o document.parent.
    No Streamlit Cloud isso pode contribuir para o erro frontend removeChild.
    Agora mantemos apenas CSS/HTML estável renderizado pelo próprio Streamlit.
    """
    try:
        st.markdown("""<div class='notranslate' translate='no' style='display:none'></div>""", unsafe_allow_html=True)
    except Exception:
        pass

aplicar_guard_notranslate_cloud()


# -----------------------------------------------------------------------------
# V13.8 Cloud Update Stability Guard
# -----------------------------------------------------------------------------
# No Streamlit Cloud, o erro de frontend "removeChild: node is not a child"
# aparece com frequência quando o app recria componentes grandes durante reruns.
# Nas versões anteriores havia um monkey patch para adicionar keys automáticas a
# st.dataframe/st.plotly_chart. Em alguns ambientes Cloud isso pode piorar o erro
# porque a key muda quando o shape/colunas do dataframe mudam.
#
# Nesta versão, a estabilidade é tratada de forma conservadora:
# - não fazemos monkey patch dos componentes nativos do Streamlit;
# - mantemos apenas o nome da página atual em session_state;
# - evitamos recriar contadores/keys dinâmicas entre reruns.

def reset_render_stability_state(page_name: str):
    """Registra a tela ativa sem modificar a árvore de componentes."""
    st.session_state['_active_page_name'] = str(page_name)


def safe_rerun():
    """Rerun centralizado.

    No Streamlit Cloud, reruns imediatos após operações pesadas podem causar
    o erro de frontend removeChild. Em Cloud, evitamos o rerun automático e
    deixamos a tela estabilizar; o usuário pode navegar/atualizar se precisar.
    Localmente mantemos o comportamento normal.
    """
    if CLOUD_MODE:
        st.session_state['_pending_soft_refresh'] = True
        return
    st.rerun()


def finalizar_atualizacao_cloud(mensagem, detalhe=None):
    """Encerra a renderização após rotinas pesadas no Cloud.

    Não chama st.rerun. Isso evita reconstrução parcial de componentes e reduz
    o risco do erro frontend removeChild durante importação/atualização.
    """
    if CLOUD_MODE:
        st.success(mensagem)
        if detalhe:
            st.info(str(detalhe))
        st.caption('Atualização concluída. Para ver os novos dados, troque de tela ou pressione Ctrl+F5.')
        st.stop()

LOGO_BRANCO = APP_DIR / 'assets' / 'sb_farma_logo_branco.png'


def usuario_atual():
    usuario = str(st.session_state.get('usuario_operacao', '')).strip()
    return usuario if usuario else 'Paulo'


def img_to_base64(path):
    try:
        return base64.b64encode(Path(path).read_bytes()).decode('utf-8')
    except Exception:
        return ''

def render_sb_header():
    logo64 = img_to_base64(LOGO_BRANCO)
    logo_html = f'<img class="sb-logo" src="data:image/png;base64,{logo64}" />' if logo64 else '<div class="main-title">SB Farma</div>'
    st.markdown(f'''
    <div class="sb-header">
      {logo_html}
      <div class="main-title">Gestão de Negociações</div>
      <div class="subtitle">Negociações • Apuração • Financeiro</div>
      <div class="sb-chip">SB Farma • v14.0 Cloud Stable Import</div>
    </div>
    ''', unsafe_allow_html=True)


def money(v):
    try:
        return f"R$ {float(v):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
    except Exception:
        return 'R$ 0,00'


def pct(v):
    try:
        return f"{float(v):.2f}%".replace('.', ',')
    except Exception:
        return '0,00%'


def to_numero(valor):
    """Converte valores do banco/Excel para número, aceitando formato brasileiro."""
    if isinstance(valor, pd.Series):
        if pd.api.types.is_numeric_dtype(valor):
            return valor
        return (
            valor.astype(str)
            .str.replace('R$', '', regex=False)
            .str.replace('.', '', regex=False)
            .str.replace(',', '.', regex=False)
            .pipe(pd.to_numeric, errors='coerce')
        )
    return pd.to_numeric(valor, errors='coerce')


def connect():
    # Timeout e WAL reduzem bloqueio nas tabelas pequenas de configuração/histórico.
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA busy_timeout=30000')
    except Exception:
        pass
    return con


def get_conn():
    """Compatibilidade para telas novas: usa a conexão padrão do projeto."""
    return connect()


UPDATE_LOCK = threading.Lock()


def limpar_cache_telas():
    """Limpa caches de leitura usados para acelerar a navegação.

    Deve ser chamado somente após importações, atualizações ou gravações relevantes.
    A troca de telas continua leve porque as leituras pesadas ficam em cache.
    """
    try:
        st.cache_data.clear()
    except Exception:
        pass


def salvar_cache_compras(df):
    """Salva a base de entradas fora do SQLite para evitar database is locked."""
    df = df.copy()
    tmp = None
    try:
        tmp = CACHE_PARQUET.with_suffix('.parquet.tmp')
        df.to_parquet(tmp, index=False)
        os.replace(tmp, CACHE_PARQUET)
        if CACHE_PICKLE.exists():
            try:
                CACHE_PICKLE.unlink()
            except Exception:
                pass
        limpar_cache_telas()
        return 'parquet'
    except Exception:
        if tmp and Path(tmp).exists():
            try:
                Path(tmp).unlink()
            except Exception:
                pass
        tmp = CACHE_PICKLE.with_suffix('.pkl.tmp')
        df.to_pickle(tmp)
        os.replace(tmp, CACHE_PICKLE)
        limpar_cache_telas()
        return 'pickle'


@st.cache_data(ttl=3600, show_spinner=False)
def carregar_cache_compras():
    # Cache persistente em pasta persistente/cache.
    # Migra automaticamente arquivos antigos salvos na raiz do projeto.
    old_parquet = APP_DIR / 'cache_entradas.parquet'
    old_pickle = APP_DIR / 'cache_entradas.pkl'
    old_cache_parquet = APP_DIR / 'cache' / 'entradas_cache.parquet'
    old_cache_pickle = APP_DIR / 'cache' / 'entradas_cache.pkl'
    try:
        import shutil
        for origem, destino in [(old_parquet, CACHE_PARQUET), (old_pickle, CACHE_PICKLE), (old_cache_parquet, CACHE_PARQUET), (old_cache_pickle, CACHE_PICKLE)]:
            if origem.exists() and not destino.exists():
                shutil.copy2(origem, destino)
    except Exception:
        pass
    if CACHE_PARQUET.exists():
        try:
            return pd.read_parquet(CACHE_PARQUET)
        except Exception:
            pass
    if CACHE_PICKLE.exists():
        try:
            return pd.read_pickle(CACHE_PICKLE)
        except Exception:
            pass
    # Migração: se existir compra antiga dentro do SQLite, carrega uma vez e grava no cache externo.
    try:
        with connect() as con:
            df = pd.read_sql_query('SELECT * FROM compras', con)
        if not df.empty:
            salvar_cache_compras(df)
            return df
    except Exception:
        pass
    return pd.DataFrame(columns=['data_compra','fabricante','fornecedor','produto','ean','codigo_interno','qtd_compra','valor_compra','fonte_valor','origem_arquivo'])


def salvar_cache_vendas(df):
    """Salva a base de vendas em cache persistente fora da pasta do projeto."""
    df = df.copy()
    tmp = None
    try:
        tmp = CACHE_VENDAS_PARQUET.with_suffix('.parquet.tmp')
        df.to_parquet(tmp, index=False)
        os.replace(tmp, CACHE_VENDAS_PARQUET)
        if CACHE_VENDAS_PICKLE.exists():
            try:
                CACHE_VENDAS_PICKLE.unlink()
            except Exception:
                pass
        limpar_cache_telas()
        return 'parquet'
    except Exception:
        if tmp and Path(tmp).exists():
            try:
                Path(tmp).unlink()
            except Exception:
                pass
        tmp = CACHE_VENDAS_PICKLE.with_suffix('.pkl.tmp')
        df.to_pickle(tmp)
        os.replace(tmp, CACHE_VENDAS_PICKLE)
        limpar_cache_telas()
        return 'pickle'


@st.cache_data(ttl=3600, show_spinner=False)
def carregar_cache_vendas():
    if CACHE_VENDAS_PARQUET.exists():
        try:
            return pd.read_parquet(CACHE_VENDAS_PARQUET)
        except Exception:
            pass
    if CACHE_VENDAS_PICKLE.exists():
        try:
            return pd.read_pickle(CACHE_VENDAS_PICKLE)
        except Exception:
            pass
    return pd.DataFrame(columns=['data_venda','loja','usuario_orcamento','vendaid','fabricante','fornecedor','produto','ean','cod_interno','classificacao','qtd_venda','valor_venda','valor_unitario','custo','lucro','lucro_perc','programa_pbm','pec','coo','media_venda_dia','media_venda_mes','origem_arquivo'])


def init_db():
    with connect() as con:
        con.execute('''
            CREATE TABLE IF NOT EXISTS negociacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo TEXT NOT NULL,
                nome TEXT NOT NULL,
                percentual REAL NOT NULL,
                data_inicio TEXT NOT NULL,
                data_fim TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Ativo',
                observacao TEXT,
                criado_em TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Migração para negociações criadas em versões anteriores
        for col, ddl in [
            ('codigo_negociacao', 'ALTER TABLE negociacoes ADD COLUMN codigo_negociacao TEXT'),
            ('meta_compra', 'ALTER TABLE negociacoes ADD COLUMN meta_compra REAL DEFAULT 0'),
            ('codigo_curto', 'ALTER TABLE negociacoes ADD COLUMN codigo_curto TEXT'),
            ('tipo_negociacao', "ALTER TABLE negociacoes ADD COLUMN tipo_negociacao TEXT DEFAULT 'Desconto percentual'"),
            ('bonificacao', 'ALTER TABLE negociacoes ADD COLUMN bonificacao REAL DEFAULT 0'),
            ('verba_comercial', 'ALTER TABLE negociacoes ADD COLUMN verba_comercial REAL DEFAULT 0'),
            ('atualizado_em', 'ALTER TABLE negociacoes ADD COLUMN atualizado_em TEXT'),
            ('excluido_em', 'ALTER TABLE negociacoes ADD COLUMN excluido_em TEXT'),
            ('excluido_por', 'ALTER TABLE negociacoes ADD COLUMN excluido_por TEXT'),
            ('criado_por', 'ALTER TABLE negociacoes ADD COLUMN criado_por TEXT'),
            ('atualizado_por', 'ALTER TABLE negociacoes ADD COLUMN atualizado_por TEXT'),
            ('tipo_investimento', "ALTER TABLE negociacoes ADD COLUMN tipo_investimento TEXT DEFAULT 'Sell Out'"),
        ]:
            try:
                con.execute(ddl)
            except Exception:
                pass

        con.execute('''
            CREATE TABLE IF NOT EXISTS negociacao_faixas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                negociacao_id INTEGER NOT NULL,
                faixa INTEGER NOT NULL,
                meta_valor REAL DEFAULT 0,
                percentual REAL DEFAULT 0,
                bonificacao REAL DEFAULT 0,
                verba_comercial REAL DEFAULT 0,
                observacao TEXT,
                criado_em TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (negociacao_id) REFERENCES negociacoes(id)
            )
        ''')
        con.execute('''
            CREATE TABLE IF NOT EXISTS negociacao_produtos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                negociacao_id INTEGER NOT NULL,
                produto TEXT,
                ean TEXT,
                meta_qtd REAL DEFAULT 0,
                valor_unitario REAL DEFAULT 0,
                percentual REAL DEFAULT 0,
                observacao TEXT,
                criado_em TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (negociacao_id) REFERENCES negociacoes(id)
            )
        ''')

        # Migração v37: separação de investimento Sell In (compra) e Sell Out (venda) por produto
        for _col, _ddl in {
            'codigo_interno': 'TEXT',
            'tipo_investimento': 'TEXT DEFAULT "Sell Out"',
            'meta_compra_qtd': 'REAL DEFAULT 0',
            'meta_venda_qtd': 'REAL DEFAULT 0',
            'meta_venda_valor': 'REAL DEFAULT 0',
            'valor_unitario_sellin': 'REAL DEFAULT 0',
            'valor_unitario_sellout': 'REAL DEFAULT 0',
            'percentual_sellin': 'REAL DEFAULT 0',
            'percentual_sellout': 'REAL DEFAULT 0'
        }.items():
            try:
                con.execute(f'ALTER TABLE negociacao_produtos ADD COLUMN {_col} {_ddl}')
            except Exception:
                pass

        con.execute('''
            CREATE TABLE IF NOT EXISTS historico_negociacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                negociacao_id INTEGER,
                codigo_negociacao TEXT,
                data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
                usuario TEXT,
                campo TEXT,
                valor_anterior TEXT,
                valor_novo TEXT,
                observacao TEXT
            )
        ''')

        con.execute('''
            CREATE TABLE IF NOT EXISTS compras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_compra TEXT NOT NULL,
                fabricante TEXT,
                fornecedor TEXT,
                produto TEXT,
                ean TEXT,
                qtd_compra REAL DEFAULT 0,
                valor_compra REAL NOT NULL,
                fonte_valor TEXT,
                origem_arquivo TEXT,
                criado_em TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Migração para bancos locais já criados em versões anteriores
        try:
            con.execute('ALTER TABLE compras ADD COLUMN fonte_valor TEXT')
        except Exception:
            pass
        try:
            con.execute('ALTER TABLE compras ADD COLUMN qtd_compra REAL DEFAULT 0')
        except Exception:
            pass

        con.execute('''
            CREATE TABLE IF NOT EXISTS configuracoes (
                chave TEXT PRIMARY KEY,
                valor TEXT
            )
        ''')
        con.execute('''
            CREATE TABLE IF NOT EXISTS atualizacoes_entrada (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
                tipo TEXT,
                status TEXT,
                registros INTEGER DEFAULT 0,
                mensagem TEXT
            )
        ''')

        # V8.0 Enterprise: workflow, documentos, recebimentos, backup e permissões.
        con.execute('''
            CREATE TABLE IF NOT EXISTS workflow_negociacao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                negociacao_id INTEGER NOT NULL,
                etapa TEXT NOT NULL,
                status TEXT DEFAULT 'Pendente',
                responsavel TEXT,
                data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
                observacao TEXT,
                FOREIGN KEY (negociacao_id) REFERENCES negociacoes(id)
            )
        ''')
        con.execute('''
            CREATE TABLE IF NOT EXISTS documentos_negociacao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                negociacao_id INTEGER NOT NULL,
                tipo_documento TEXT,
                nome_arquivo TEXT,
                caminho_arquivo TEXT,
                usuario TEXT,
                data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
                observacao TEXT,
                FOREIGN KEY (negociacao_id) REFERENCES negociacoes(id)
            )
        ''')
        con.execute('''
            CREATE TABLE IF NOT EXISTS recebimentos_negociacao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                negociacao_id INTEGER NOT NULL,
                data_recebimento TEXT NOT NULL,
                valor_recebido REAL DEFAULT 0,
                forma_recebimento TEXT,
                usuario TEXT,
                observacao TEXT,
                criado_em TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (negociacao_id) REFERENCES negociacoes(id)
            )
        ''')
        # V11.4: permite excluir logicamente recebimentos antigos que aparecem no extrato.
        for col_def in [
            "status_lancamento TEXT DEFAULT 'Ativo'",
            'excluido_em TEXT',
            'excluido_por TEXT',
            'motivo_exclusao TEXT'
        ]:
            try:
                con.execute(f'ALTER TABLE recebimentos_negociacao ADD COLUMN {col_def}')
            except Exception:
                pass
        # V10.1: lançamentos financeiros do conta corrente (débitos, glosas, abatimentos e ajustes).
        con.execute('''
            CREATE TABLE IF NOT EXISTS financeiro_lancamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                negociacao_id INTEGER NOT NULL,
                data_lancamento TEXT NOT NULL,
                competencia TEXT,
                tipo_movimento TEXT NOT NULL,
                natureza TEXT NOT NULL DEFAULT 'Débito',
                valor REAL DEFAULT 0,
                documento TEXT,
                usuario TEXT,
                observacao TEXT,
                criado_em TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (negociacao_id) REFERENCES negociacoes(id)
            )
        ''')
        try:
            con.execute('ALTER TABLE financeiro_lancamentos ADD COLUMN competencia TEXT')
        except Exception:
            pass
        # V10.6: lançamentos financeiros avulsos sem negociação vinculada.
        # Esses campos permitem lançar verbas, bonificações e ajustes diretamente
        # na conta corrente do fabricante/fornecedor.
        for col_def in [
            "origem_lancamento TEXT DEFAULT \'Negociação\'",
            'entidade_tipo TEXT',
            'entidade_nome TEXT',
            'parent_credito_id INTEGER DEFAULT 0',
            "status_credito TEXT DEFAULT 'Em Aberto'",
            'data_vencimento TEXT',
            'forma_pagamento TEXT',
            "status_lancamento TEXT DEFAULT 'Ativo'",
            'alterado_em TEXT',
            'alterado_por TEXT',
            'excluido_em TEXT',
            'excluido_por TEXT',
            'motivo_exclusao TEXT'
        ]:
            try:
                con.execute(f'ALTER TABLE financeiro_lancamentos ADD COLUMN {col_def}')
            except Exception:
                pass
        con.execute('''
            CREATE TABLE IF NOT EXISTS financeiro_comprovantes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lancamento_id INTEGER NOT NULL,
                negociacao_id INTEGER NOT NULL,
                nome_arquivo TEXT,
                caminho_arquivo TEXT,
                usuario TEXT,
                data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
                competencia TEXT,
                entidade_nome TEXT,
                FOREIGN KEY (lancamento_id) REFERENCES financeiro_lancamentos(id),
                FOREIGN KEY (negociacao_id) REFERENCES negociacoes(id)
            )
        ''')
        for col_def in [
            'competencia TEXT',
            'entidade_nome TEXT'
        ]:
            try:
                con.execute(f'ALTER TABLE financeiro_comprovantes ADD COLUMN {col_def}')
            except Exception:
                pass

        # V10.8: cadastro parametrizável de tipos de crédito financeiro.
        con.execute('''
            CREATE TABLE IF NOT EXISTS financeiro_tipos_credito (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL UNIQUE,
                ativo INTEGER DEFAULT 1,
                usuario TEXT,
                criado_em TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        for tipo_padrao in [
            'Verba fixa',
            'Verba comercial',
            'Bonificação financeira',
            'Incentivo',
            'Ressarcimento',
            'Sell In manual',
            'Sell Out manual',
            'Ajuste a favor',
            'Outros créditos'
        ]:
            try:
                con.execute('INSERT OR IGNORE INTO financeiro_tipos_credito (nome, ativo, usuario) VALUES (?, 1, ?)', (tipo_padrao, 'Sistema'))
            except Exception:
                pass


        # V12.5: tipos de negociação parametrizáveis.
        con.execute('''
            CREATE TABLE IF NOT EXISTS tipos_negociacao_parametros (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL UNIQUE,
                ativo INTEGER DEFAULT 1,
                controla_produtos INTEGER DEFAULT 0,
                faz_apuracao INTEGER DEFAULT 0,
                gera_financeiro INTEGER DEFAULT 1,
                utiliza_faixas INTEGER DEFAULT 0,
                utiliza_metas INTEGER DEFAULT 0,
                permite_comprovantes INTEGER DEFAULT 1,
                gera_cobranca INTEGER DEFAULT 1,
                observacao TEXT,
                usuario TEXT,
                criado_em TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        tipos_negociacao_padrao = [
            ('Desconto percentual', 0, 1, 1, 0, 1), ('Preço unitário fixo', 0, 1, 1, 0, 1),
            ('Bonificação', 0, 1, 1, 0, 1), ('Faixa de meta', 0, 1, 1, 1, 1),
            ('Híbrida', 1, 1, 1, 1, 1), ('Verba comercial / Sell-out', 0, 1, 1, 0, 1),
            ('Trade Marketing', 0, 0, 1, 0, 0), ('Verba Comercial', 0, 0, 1, 0, 0),
            ('Cashback', 0, 0, 1, 0, 0), ('Bonificação Financeira', 0, 0, 1, 0, 0),
            ('Incentivo', 0, 0, 1, 0, 0), ('Acordo Comercial', 0, 0, 1, 0, 0),
            ('Campanha', 0, 0, 1, 0, 0), ('Patrocínio', 0, 0, 1, 0, 0),
            ('Evento', 0, 0, 1, 0, 0), ('Exposição', 0, 0, 1, 0, 0),
            ('Mídia', 0, 0, 1, 0, 0), ('Digital', 0, 0, 1, 0, 0),
            ('Fidelidade', 0, 0, 1, 0, 0), ('Contrato', 0, 0, 1, 0, 0), ('Outros', 0, 0, 1, 0, 0)
        ]
        for nome_tipo, controla_produtos, faz_apuracao, gera_financeiro, utiliza_faixas, utiliza_metas in tipos_negociacao_padrao:
            try:
                con.execute('''
                    INSERT OR IGNORE INTO tipos_negociacao_parametros
                    (nome, ativo, controla_produtos, faz_apuracao, gera_financeiro, utiliza_faixas, utiliza_metas, permite_comprovantes, gera_cobranca, usuario)
                    VALUES (?, 1, ?, ?, ?, ?, ?, 1, 1, ?)
                ''', (nome_tipo, controla_produtos, faz_apuracao, gera_financeiro, utiliza_faixas, utiliza_metas, 'Sistema'))
            except Exception:
                pass

        con.execute('''
            CREATE TABLE IF NOT EXISTS usuarios_sistema (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario TEXT UNIQUE,
                nome TEXT,
                perfil TEXT DEFAULT 'Compras',
                ativo INTEGER DEFAULT 1,
                criado_em TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        con.execute("INSERT OR IGNORE INTO usuarios_sistema (usuario, nome, perfil, ativo) VALUES ('paulo', 'Paulo Marques', 'Administrador', 1)")
        for idx_name, ddl in [
            ('idx_workflow_negociacao', 'CREATE INDEX IF NOT EXISTS idx_workflow_negociacao ON workflow_negociacao(negociacao_id)'),
            ('idx_documentos_negociacao', 'CREATE INDEX IF NOT EXISTS idx_documentos_negociacao ON documentos_negociacao(negociacao_id)'),
            ('idx_recebimentos_negociacao', 'CREATE INDEX IF NOT EXISTS idx_recebimentos_negociacao ON recebimentos_negociacao(negociacao_id)'),
            ('idx_financeiro_lancamentos_negociacao', 'CREATE INDEX IF NOT EXISTS idx_financeiro_lancamentos_negociacao ON financeiro_lancamentos(negociacao_id)'),
            ('idx_financeiro_lancamentos_entidade', 'CREATE INDEX IF NOT EXISTS idx_financeiro_lancamentos_entidade ON financeiro_lancamentos(entidade_tipo, entidade_nome)'),
            ('idx_financeiro_lancamentos_competencia', 'CREATE INDEX IF NOT EXISTS idx_financeiro_lancamentos_competencia ON financeiro_lancamentos(competencia)'),
            ('idx_financeiro_lancamentos_parent', 'CREATE INDEX IF NOT EXISTS idx_financeiro_lancamentos_parent ON financeiro_lancamentos(parent_credito_id)'),
            ('idx_financeiro_comprovantes_lancamento', 'CREATE INDEX IF NOT EXISTS idx_financeiro_comprovantes_lancamento ON financeiro_comprovantes(lancamento_id)'),
            ('idx_financeiro_tipos_credito_nome', 'CREATE INDEX IF NOT EXISTS idx_financeiro_tipos_credito_nome ON financeiro_tipos_credito(nome)'),
            ('idx_produtos_negociacao_id', 'CREATE INDEX IF NOT EXISTS idx_produtos_negociacao_id ON negociacao_produtos(negociacao_id)'),
            ('idx_historico_negociacao_id', 'CREATE INDEX IF NOT EXISTS idx_historico_negociacao_id ON historico_negociacoes(negociacao_id)'),
        ]:
            try:
                con.execute(ddl)
            except Exception:
                pass

        # Preenche código e meta para negociações antigas, se existirem.
        try:
            rows = con.execute("SELECT id, nome FROM negociacoes WHERE codigo_negociacao IS NULL OR codigo_negociacao = '' ORDER BY id").fetchall()
            for _id, nome in rows:
                codigo = proximo_codigo_negociacao(con) if 'proximo_codigo_negociacao' in globals() else f'NEG-{datetime.now().year}-{int(_id):06d}'
                codigo_curto = gerar_codigo_curto(nome, int(codigo.split('-')[-1])) if 'gerar_codigo_curto' in globals() else f'NEG-{int(_id):04d}'
                con.execute('UPDATE negociacoes SET codigo_negociacao=?, codigo_curto=?, meta_compra=COALESCE(meta_compra,0) WHERE id=?', (codigo, codigo_curto, _id))
        except Exception:
            pass
        con.commit()


def garantir_config_padrao():
    """Configura conexão PostgreSQL com suporte local e Streamlit Cloud.

    Regra da v13.1:
    - Se já existir configuração salva no app, ela não será apagada por valores vazios.
    - Se houver st.secrets/env preenchido, ele só preenche campos ainda vazios.
    - A senha só é alterada quando o usuário/secrets informar uma senha não vazia.
    """
    with connect() as con:
        existe = con.execute("SELECT COUNT(*) FROM configuracoes WHERE chave='auto_host'").fetchone()[0]

    defaults = {
        'auto_host': DEFAULT_DB_CONFIG.get('host', ''),
        'auto_port': DEFAULT_DB_CONFIG.get('port', '5432'),
        'auto_database': DEFAULT_DB_CONFIG.get('database', ''),
        'auto_user': DEFAULT_DB_CONFIG.get('user', ''),
        'auto_password': DEFAULT_DB_CONFIG.get('password', ''),
        'auto_enabled': '1' if (_truthy(APP_SECRETS.get('auto_enabled')) and not CLOUD_MODE) else '0',
        'auto_hour': str(SCHEDULE_HOUR),
        'auto_minute': str(SCHEDULE_MINUTE),
    }

    if not existe:
        for chave, valor in defaults.items():
            set_config(chave, valor or '')
        return

    # Completa somente campos vazios. Isso evita perder a senha salva ao publicar no Cloud.
    for chave, valor in defaults.items():
        atual = get_config(chave, '')
        if (atual in (None, '')) and (valor not in (None, '')):
            set_config(chave, valor)



def _db_mtime_sig():
    try:
        return DB_PATH.stat().st_mtime_ns
    except Exception:
        return 0


@st.cache_data(ttl=3600, show_spinner=False)
def _read_sql_table_cached(db_path_str, table_name, filtrar_ativos, db_sig):
    """Leitura rápida das tabelas pequenas do SQLite.

    O Streamlit reexecuta o app inteiro a cada clique. Antes, várias telas
    releram as mesmas tabelas de apoio em cada transição. Esse cache é
    invalidado automaticamente quando o arquivo do banco muda ou quando o
    app chama limpar_cache_telas() após gravações/atualizações.
    """
    with sqlite3.connect(db_path_str, timeout=30) as con:
        if table_name == 'negociacoes' and filtrar_ativos:
            try:
                return pd.read_sql_query("SELECT * FROM negociacoes WHERE excluido_em IS NULL OR excluido_em = ''", con)
            except Exception:
                return pd.read_sql_query('SELECT * FROM negociacoes', con)
        return pd.read_sql_query(f'SELECT * FROM "{table_name}"', con)


def load_table(name):
    if name == 'compras':
        return carregar_cache_compras()
    if name == 'vendas':
        return carregar_cache_vendas()
    return _read_sql_table_cached(str(DB_PATH), str(name), name == 'negociacoes', _db_mtime_sig())


def load_negociacoes_todas():
    with connect() as con:
        try:
            return pd.read_sql_query('SELECT * FROM negociacoes', con)
        except Exception:
            return pd.DataFrame()



def load_table_safe(name):
    try:
        return load_table(name)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def listar_tipos_negociacao_parametros():
    padrao = pd.DataFrame([
        {'nome':'Desconto percentual','ativo':1,'controla_produtos':0,'faz_apuracao':1,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':1},
        {'nome':'Preço unitário fixo','ativo':1,'controla_produtos':0,'faz_apuracao':1,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':1},
        {'nome':'Bonificação','ativo':1,'controla_produtos':0,'faz_apuracao':1,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':1},
        {'nome':'Faixa de meta','ativo':1,'controla_produtos':0,'faz_apuracao':1,'gera_financeiro':1,'utiliza_faixas':1,'utiliza_metas':1},
        {'nome':'Híbrida','ativo':1,'controla_produtos':1,'faz_apuracao':1,'gera_financeiro':1,'utiliza_faixas':1,'utiliza_metas':1},
        {'nome':'Verba comercial / Sell-out','ativo':1,'controla_produtos':0,'faz_apuracao':1,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':1},
        {'nome':'Trade Marketing','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Verba Comercial','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Cashback','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Bonificação Financeira','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Incentivo','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Acordo Comercial','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Campanha','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Patrocínio','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Evento','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Exposição','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Mídia','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Digital','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Fidelidade','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Contrato','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
        {'nome':'Outros','ativo':1,'controla_produtos':0,'faz_apuracao':0,'gera_financeiro':1,'utiliza_faixas':0,'utiliza_metas':0},
    ])
    try:
        df = load_table_safe('tipos_negociacao_parametros')
        if df.empty or 'nome' not in df.columns:
            return padrao
        for col in ['ativo','controla_produtos','faz_apuracao','gera_financeiro','utiliza_faixas','utiliza_metas','permite_comprovantes','gera_cobranca']:
            if col not in df.columns:
                df[col] = 1 if col in ['ativo','gera_financeiro'] else 0
        return df.sort_values(['ativo','nome'], ascending=[False, True])
    except Exception:
        return padrao


def listar_tipos_negociacao_ativos():
    df = listar_tipos_negociacao_parametros()
    if df.empty or 'nome' not in df.columns:
        return ['Desconto percentual','Preço unitário fixo','Bonificação','Faixa de meta','Híbrida','Verba comercial / Sell-out']
    ativos = df[df['ativo'].fillna(1).astype(int).eq(1)] if 'ativo' in df.columns else df
    nomes = ativos['nome'].dropna().astype(str).str.strip().replace('', pd.NA).dropna().tolist()
    base = ['Desconto percentual','Preço unitário fixo','Bonificação','Faixa de meta','Híbrida','Verba comercial / Sell-out']
    return [x for x in base if x in nomes] + [x for x in nomes if x not in base]


def obter_parametro_tipo_negociacao(nome):
    nome = str(nome or '').strip()
    df = listar_tipos_negociacao_parametros()
    if df.empty or 'nome' not in df.columns:
        return {'nome': nome, 'faz_apuracao': 1, 'gera_financeiro': 1, 'controla_produtos': 0, 'utiliza_faixas': 0, 'utiliza_metas': 1}
    achou = df[df['nome'].astype(str).str.strip().str.lower().eq(nome.lower())]
    if achou.empty:
        return {'nome': nome, 'faz_apuracao': 0, 'gera_financeiro': 1, 'controla_produtos': 0, 'utiliza_faixas': 0, 'utiliza_metas': 0}
    r = achou.iloc[0].to_dict()
    out = {}
    for k, v in r.items():
        if k in ['ativo','controla_produtos','faz_apuracao','gera_financeiro','utiliza_faixas','utiliza_metas','permite_comprovantes','gera_cobranca']:
            try:
                out[k] = int(v)
            except Exception:
                out[k] = 0
        else:
            out[k] = v
    return out


def tipo_negociacao_faz_apuracao(nome):
    return int(obter_parametro_tipo_negociacao(nome).get('faz_apuracao', 0) or 0) == 1


def salvar_tipo_negociacao_parametro(nome, controla_produtos=0, faz_apuracao=0, gera_financeiro=1, utiliza_faixas=0, utiliza_metas=0, ativo=1, observacao=''):
    nome = str(nome or '').strip()
    if not nome:
        return False, 'Informe o nome do tipo de negociação.'
    try:
        with connect() as con:
            con.execute('''
                INSERT INTO tipos_negociacao_parametros
                (nome, ativo, controla_produtos, faz_apuracao, gera_financeiro, utiliza_faixas, utiliza_metas, permite_comprovantes, gera_cobranca, observacao, usuario)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
                ON CONFLICT(nome) DO UPDATE SET
                    ativo=excluded.ativo,
                    controla_produtos=excluded.controla_produtos,
                    faz_apuracao=excluded.faz_apuracao,
                    gera_financeiro=excluded.gera_financeiro,
                    utiliza_faixas=excluded.utiliza_faixas,
                    utiliza_metas=excluded.utiliza_metas,
                    observacao=excluded.observacao,
                    usuario=excluded.usuario
            ''', (nome, int(ativo), int(controla_produtos), int(faz_apuracao), int(gera_financeiro), int(utiliza_faixas), int(utiliza_metas), observacao, usuario_atual()))
            con.commit()
        try:
            listar_tipos_negociacao_parametros.clear()
        except Exception:
            pass
        return True, f'Tipo de negociação "{nome}" salvo.'
    except Exception as e:
        return False, f'Não foi possível salvar o tipo de negociação: {e}'


def inativar_tipo_negociacao_parametro(nome):
    nome = str(nome or '').strip()
    if not nome:
        return False, 'Selecione o tipo de negociação.'
    try:
        with connect() as con:
            con.execute('UPDATE tipos_negociacao_parametros SET ativo=0, usuario=? WHERE nome=?', (usuario_atual(), nome))
            con.commit()
        try:
            listar_tipos_negociacao_parametros.clear()
        except Exception:
            pass
        return True, f'Tipo de negociação "{nome}" inativado.'
    except Exception as e:
        return False, f'Não foi possível inativar o tipo de negociação: {e}'


def criar_credito_financeiro_negociacao_manual(con, negociacao_id, codigo, tipo_entidade, entidade_nome, tipo_negociacao, valor, data_inicio, competencia, usuario, observacao=''):
    # Cria/atualiza crédito a receber para negociações que não dependem de apuração.
    valor = float(valor or 0)
    if valor <= 0:
        return None
    competencia = str(competencia or data_inicio or '')[:7]
    agora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    row = con.execute('''
        SELECT id FROM financeiro_lancamentos
        WHERE negociacao_id=?
          AND origem_lancamento='Negociação sem apuração'
          AND natureza='Crédito'
          AND (status_lancamento IS NULL OR status_lancamento <> 'Excluído')
        ORDER BY id DESC LIMIT 1
    ''', (int(negociacao_id),)).fetchone()
    if row:
        lanc_id = int(row[0])
        con.execute('''
            UPDATE financeiro_lancamentos
            SET data_lancamento=?, competencia=?, tipo_movimento=?, valor=?, documento=?, usuario=?, observacao=?, entidade_tipo=?, entidade_nome=?, status_credito='Em Aberto', data_vencimento=?, alterado_em=?, alterado_por=?
            WHERE id=?
        ''', (str(data_inicio), competencia, str(tipo_negociacao), valor, codigo, usuario, observacao, tipo_entidade, entidade_nome, str(data_inicio), agora, usuario, lanc_id))
        return lanc_id
    con.execute('''
        INSERT INTO financeiro_lancamentos
        (negociacao_id, data_lancamento, competencia, tipo_movimento, natureza, valor, documento, usuario, observacao, origem_lancamento, entidade_tipo, entidade_nome, parent_credito_id, status_credito, data_vencimento, forma_pagamento)
        VALUES (?, ?, ?, ?, 'Crédito', ?, ?, ?, ?, 'Negociação sem apuração', ?, ?, 0, 'Em Aberto', ?, '')
    ''', (int(negociacao_id), str(data_inicio), competencia, str(tipo_negociacao), valor, codigo, usuario, observacao, tipo_entidade, entidade_nome, str(data_inicio)))
    return con.execute('SELECT last_insert_rowid()').fetchone()[0]

@st.cache_data(ttl=3600, show_spinner=False)
def listar_tipos_credito_ativos():
    """Retorna os tipos de crédito cadastrados pelo usuário, mantendo padrões se a tabela ainda não existir."""
    padrao = ['Verba fixa','Verba comercial','Bonificação financeira','Incentivo','Ressarcimento','Sell In manual','Sell Out manual','Ajuste a favor','Outros créditos']
    try:
        df = load_table_safe('financeiro_tipos_credito')
        if df.empty or 'nome' not in df.columns:
            return padrao
        if 'ativo' in df.columns:
            df = df[df['ativo'].fillna(1).astype(int).eq(1)]
        tipos = sorted(df['nome'].dropna().astype(str).str.strip().replace('', pd.NA).dropna().unique().tolist())
        return tipos or padrao
    except Exception:
        return padrao


def salvar_tipo_credito(nome):
    nome = str(nome or '').strip()
    if not nome:
        return False, 'Informe o nome do tipo de crédito.'
    try:
        with connect() as con:
            con.execute('INSERT OR IGNORE INTO financeiro_tipos_credito (nome, ativo, usuario) VALUES (?, 1, ?)', (nome, usuario_atual()))
            con.execute('UPDATE financeiro_tipos_credito SET ativo=1, usuario=? WHERE nome=?', (usuario_atual(), nome))
            con.commit()
        return True, f'Tipo de crédito "{nome}" cadastrado/reativado.'
    except Exception as e:
        return False, f'Não foi possível salvar o tipo de crédito: {e}'


def excluir_tipo_credito(nome):
    nome = str(nome or '').strip()
    if not nome:
        return False, 'Selecione o tipo de crédito.'
    try:
        with connect() as con:
            con.execute('UPDATE financeiro_tipos_credito SET ativo=0 WHERE nome=?', (nome,))
            con.commit()
        return True, f'Tipo de crédito "{nome}" excluído da lista de seleção.'
    except Exception as e:
        return False, f'Não foi possível excluir o tipo de crédito: {e}'



def listar_tipos_credito_todos():
    """Lista todos os tipos de crédito, ativos e inativos, para manutenção cadastral."""
    try:
        df = load_table_safe('financeiro_tipos_credito')
        if df.empty:
            return pd.DataFrame(columns=['id','nome','ativo','usuario','criado_em'])
        for col in ['id','nome','ativo','usuario','criado_em']:
            if col not in df.columns:
                df[col] = ''
        df['Status'] = df['ativo'].fillna(1).astype(int).map({1:'Ativo',0:'Inativo'}).fillna('Ativo')
        return df[['id','nome','Status','usuario','criado_em','ativo']].sort_values(['Status','nome'], ascending=[True, True])
    except Exception:
        return pd.DataFrame(columns=['id','nome','Status','usuario','criado_em','ativo'])


def atualizar_tipo_credito_por_id(tipo_id, novo_nome, ativo=1):
    novo_nome = str(novo_nome or '').strip()
    if not novo_nome:
        return False, 'Informe a descrição do tipo de crédito.'
    try:
        with connect() as con:
            con.execute('UPDATE financeiro_tipos_credito SET nome=?, ativo=?, usuario=? WHERE id=?', (novo_nome, int(ativo), usuario_atual(), int(tipo_id)))
            con.commit()
        return True, 'Tipo de crédito atualizado com sucesso.'
    except Exception as e:
        return False, f'Não foi possível atualizar o tipo de crédito: {e}'


def inativar_tipo_credito_por_id(tipo_id):
    try:
        with connect() as con:
            nome = con.execute('SELECT nome FROM financeiro_tipos_credito WHERE id=?', (int(tipo_id),)).fetchone()
            con.execute('UPDATE financeiro_tipos_credito SET ativo=0, usuario=? WHERE id=?', (usuario_atual(), int(tipo_id)))
            con.commit()
        return True, f'Tipo de crédito inativado: {nome[0] if nome else tipo_id}.'
    except Exception as e:
        return False, f'Não foi possível inativar o tipo de crédito: {e}'


def reativar_tipo_credito_por_id(tipo_id):
    try:
        with connect() as con:
            con.execute('UPDATE financeiro_tipos_credito SET ativo=1, usuario=? WHERE id=?', (usuario_atual(), int(tipo_id)))
            con.commit()
        return True, 'Tipo de crédito reativado com sucesso.'
    except Exception as e:
        return False, f'Não foi possível reativar o tipo de crédito: {e}'

def listar_negociacoes_label():
    neg = load_table('negociacoes')
    if neg.empty:
        return [], {}
    neg = neg.copy()
    neg['data_inicio_dt'] = pd.to_datetime(neg.get('data_inicio'), errors='coerce').dt.date
    neg['data_fim_dt'] = pd.to_datetime(neg.get('data_fim'), errors='coerce').dt.date
    opcoes, mapa = [], {}
    for _, row in neg.sort_values(['data_inicio_dt','nome'], ascending=[False, True]).iterrows():
        label = f"{row.get('codigo_curto') or row.get('codigo_negociacao') or row.get('id')} | {row.get('nome','')} | {row.get('tipo_investimento','')} | {row.get('data_inicio_dt')} a {row.get('data_fim_dt')}"
        opcoes.append(label)
        mapa[label] = row
    return opcoes, mapa


def total_recebido_negociacao(negociacao_id=None):
    rec = load_table_safe('recebimentos_negociacao')
    if rec.empty:
        return 0.0 if negociacao_id is not None else pd.DataFrame(columns=['negociacao_id','Recebido'])
    rec['valor_recebido'] = to_numero(rec.get('valor_recebido')).fillna(0)
    if negociacao_id is not None:
        return float(rec[rec['negociacao_id'].astype(str)==str(negociacao_id)]['valor_recebido'].sum())
    return rec.groupby('negociacao_id', as_index=False)['valor_recebido'].sum().rename(columns={'valor_recebido':'Recebido'})


def criar_backup_enterprise():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    out = BACKUP_DIR / f'backup_sb_farma_negociacao_{stamp}.zip'
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in [DB_PATH, CACHE_PARQUET, CACHE_PICKLE, CACHE_VENDAS_PARQUET, CACHE_VENDAS_PICKLE]:
            if Path(f).exists():
                z.write(f, arcname=str(Path(f).relative_to(DATA_DIR)))
        docs_dir = DATA_DIR / 'documentos'
        if docs_dir.exists():
            for f in docs_dir.rglob('*'):
                if f.is_file():
                    z.write(f, arcname=str(f.relative_to(DATA_DIR)))
    return out


def salvar_documento_negociacao(negociacao_id, uploaded_file, tipo_documento, observacao=''):
    docs_dir = DATA_DIR / 'documentos' / str(int(negociacao_id))
    docs_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^A-Za-z0-9._ -]+', '_', uploaded_file.name)
    destino = docs_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"
    with open(destino, 'wb') as f:
        f.write(uploaded_file.getbuffer())
    with connect() as con:
        con.execute('''
            INSERT INTO documentos_negociacao (negociacao_id, tipo_documento, nome_arquivo, caminho_arquivo, usuario, observacao)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (int(negociacao_id), tipo_documento, uploaded_file.name, str(destino), usuario_atual(), observacao))
        registrar_historico(con, int(negociacao_id), '', 'Documento anexado', '', uploaded_file.name, usuario=usuario_atual(), observacao=tipo_documento)
        con.commit()
    return destino


def salvar_comprovantes_financeiros(lancamento_id, negociacao_id, arquivos, competencia='', entidade_nome=''):
    """Salva comprovantes financeiros vinculados a um lançamento.

    Compatível com lançamentos vinculados à negociação e lançamentos avulsos.
    Organiza os arquivos por entidade, competência, negociação e lançamento.
    """
    if not arquivos:
        return []

    def _safe_dir(valor, padrao):
        valor = str(valor or '').strip()
        if not valor:
            valor = padrao
        return re.sub(r'[^A-Za-z0-9._ -]+', '_', valor).strip(' ._') or padrao

    comp_dir = _safe_dir(competencia, 'sem_competencia')
    entidade_dir = _safe_dir(entidade_nome, 'sem_entidade')
    neg_dir = f"NEG_{int(negociacao_id or 0)}" if int(negociacao_id or 0) else 'avulso'
    docs_dir = DATA_DIR / 'documentos_financeiros' / entidade_dir / comp_dir / neg_dir / f"LANC_{int(lancamento_id)}"
    docs_dir.mkdir(parents=True, exist_ok=True)

    salvos = []
    with connect() as con:
        # Garante compatibilidade com bancos criados em versões anteriores.
        for col_def in ['competencia TEXT', 'entidade_nome TEXT']:
            try:
                con.execute(f'ALTER TABLE financeiro_comprovantes ADD COLUMN {col_def}')
            except Exception:
                pass

        for uploaded_file in arquivos:
            if uploaded_file is None:
                continue
            safe_name = re.sub(r'[^A-Za-z0-9._ -]+', '_', uploaded_file.name)
            destino = docs_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{safe_name}"
            with open(destino, 'wb') as f:
                f.write(uploaded_file.getbuffer())
            con.execute('''
                INSERT INTO financeiro_comprovantes
                (lancamento_id, negociacao_id, nome_arquivo, caminho_arquivo, usuario, competencia, entidade_nome)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (int(lancamento_id), int(negociacao_id or 0), uploaded_file.name, str(destino), usuario_atual(), str(competencia or ''), str(entidade_nome or '')))
            salvos.append(destino)
        con.commit()
    return salvos


def contar_comprovantes_financeiros():
    comp = load_table_safe('financeiro_comprovantes')
    if comp is None or comp.empty or 'lancamento_id' not in comp.columns:
        return {}
    return comp.groupby(comp['lancamento_id'].astype(str)).size().to_dict()


def competencia_from_date(valor):
    dt = pd.to_datetime(valor, errors='coerce')
    if pd.isna(dt):
        return ''
    return dt.strftime('%m/%Y')


def registrar_workflow(negociacao_id, etapa, status, responsavel='', observacao=''):
    with connect() as con:
        con.execute('''
            INSERT INTO workflow_negociacao (negociacao_id, etapa, status, responsavel, observacao)
            VALUES (?, ?, ?, ?, ?)
        ''', (int(negociacao_id), etapa, status, responsavel or usuario_atual(), observacao))
        registrar_historico(con, int(negociacao_id), '', f'Workflow - {etapa}', '', status, usuario=usuario_atual(), observacao=observacao)
        con.commit()


def calcular_saldo_por_negociacao():
    ap = load_table_safe('apuracao')
    ap_prod = load_table_safe('apuracao_produtos')
    neg = load_table_safe('negociacoes')
    if neg.empty:
        return pd.DataFrame()
    base = neg[[c for c in ['id','codigo_curto','codigo_negociacao','nome','tipo','tipo_investimento','status','data_inicio','data_fim'] if c in neg.columns]].copy()
    base['Investimento Geral'] = 0.0
    base['Investimento Produtos'] = 0.0
    if not ap.empty and 'negociacao_id' in ap.columns:
        tmp = ap.copy(); tmp['Valor Investimento a Receber'] = to_numero(tmp.get('Valor Investimento a Receber')).fillna(0)
        g = tmp.groupby('negociacao_id', as_index=False)['Valor Investimento a Receber'].sum()
        base = base.merge(g.rename(columns={'Valor Investimento a Receber':'Investimento Geral Apurado'}), left_on='id', right_on='negociacao_id', how='left')
        base['Investimento Geral'] = to_numero(base.get('Investimento Geral Apurado')).fillna(0)
        base = base.drop(columns=[c for c in ['negociacao_id','Investimento Geral Apurado'] if c in base.columns])
    if not ap_prod.empty and 'negociacao_id' in ap_prod.columns:
        tmp = ap_prod.copy(); tmp['Investimento'] = to_numero(tmp.get('Investimento')).fillna(0)
        g = tmp.groupby('negociacao_id', as_index=False)['Investimento'].sum()
        base = base.merge(g.rename(columns={'Investimento':'Investimento Produtos Apurado'}), left_on='id', right_on='negociacao_id', how='left')
        base['Investimento Produtos'] = to_numero(base.get('Investimento Produtos Apurado')).fillna(0)
        base = base.drop(columns=[c for c in ['negociacao_id','Investimento Produtos Apurado'] if c in base.columns])
    rec = total_recebido_negociacao()
    if not rec.empty:
        base = base.merge(rec, left_on='id', right_on='negociacao_id', how='left').drop(columns=['negociacao_id'])
    else:
        base['Recebido'] = 0.0
    base['Recebido'] = to_numero(base.get('Recebido')).fillna(0)
    base['Investimento Total'] = to_numero(base.get('Investimento Geral')).fillna(0) + to_numero(base.get('Investimento Produtos')).fillna(0)
    base['Saldo a Receber'] = base['Investimento Total'] - base['Recebido']
    return base

@st.cache_data(ttl=120, show_spinner=False)
def get_config(chave, default=''):
    with connect() as con:
        row = con.execute('SELECT valor FROM configuracoes WHERE chave = ?', (chave,)).fetchone()
    return row[0] if row else default


def set_config(chave, valor):
    with connect() as con:
        con.execute('INSERT OR REPLACE INTO configuracoes (chave, valor) VALUES (?, ?)', (chave, str(valor)))
        con.commit()
    # Mantém a navegação leve: configurações são cacheadas, mas invalidadas quando mudam.
    try:
        get_config.clear()
    except Exception:
        pass
    try:
        if 'ultima_atualizacao_cache' in globals():
            ultima_atualizacao_cache.clear()
    except Exception:
        pass


def salvar_configuracao_sql(host, port, database, user, password, habilitado, hora=SCHEDULE_HOUR, minuto=SCHEDULE_MINUTE):
    set_config('auto_host', host)
    set_config('auto_port', port)
    set_config('auto_database', database)
    set_config('auto_user', user)
    senha_atual = get_config('auto_password', '')
    senha_final = password if password else senha_atual
    if password:
        set_config('auto_password', password)
    set_config('auto_enabled', '1' if habilitado else '0')
    set_config('auto_hour', str(int(hora)))
    set_config('auto_minute', str(int(minuto)))
    _write_saved_db_config_file(host, port, database, user, senha_final)
    # No Cloud, força sincronização do SQLite/configurações logo após salvar.
    try:
        sincronizar_estado_cloud(force=True)
    except Exception:
        pass


def registrar_atualizacao(tipo, status, registros=0, mensagem=''):
    with connect() as con:
        con.execute("""
            INSERT INTO atualizacoes_entrada (tipo, status, registros, mensagem)
            VALUES (?, ?, ?, ?)
        """, (tipo, status, int(registros or 0), str(mensagem)[:1000]))
        con.commit()
    try:
        ultima_atualizacao_cache.clear()
    except Exception:
        pass


@st.cache_data(ttl=60, show_spinner=False)
def ultima_atualizacao_cache():
    with connect() as con:
        row = con.execute("""
            SELECT data_hora, status, registros, mensagem
            FROM atualizacoes_entrada
            WHERE tipo IN ('AUTO', 'MANUAL_SQL')
            ORDER BY id DESC
            LIMIT 1
        """).fetchone()
    return row


def proxima_atualizacao_texto():
    h = int(get_config('auto_hour', str(SCHEDULE_HOUR)) or SCHEDULE_HOUR)
    m = int(get_config('auto_minute', str(SCHEDULE_MINUTE)) or SCHEDULE_MINUTE)
    return f'{h:02d}:{m:02d}'


def mostrar_status_conexao():
    ultimo = ultima_atualizacao_cache()
    status = 'Ativado' if get_config('auto_enabled', '0') == '1' else 'Desativado'
    if ultimo:
        msg = f"Atualização: {ultimo[0]} | Status: {ultimo[1]} | Registros: {ultimo[2]}"
    else:
        msg = f"Atualização automática: {status} | Sem atualização registrada"
    st.caption(msg)


def proximo_codigo_negociacao(con):
    ano = datetime.now().year
    prefixo = f'NEG-{ano}-'
    row = con.execute(
        "SELECT codigo_negociacao FROM negociacoes WHERE codigo_negociacao LIKE ? ORDER BY codigo_negociacao DESC LIMIT 1",
        (prefixo + '%',)
    ).fetchone()
    if row and row[0]:
        try:
            seq = int(str(row[0]).split('-')[-1]) + 1
        except Exception:
            seq = 1
    else:
        seq = 1
    return f'{prefixo}{seq:06d}'


def gerar_codigo_curto(nome, sequencial):
    letras = ''.join(ch for ch in str(nome).upper() if ch.isalnum())[:3] or 'NEG'
    return f'{letras}-{sequencial:04d}'



def normalizar_faixas(faixas):
    limpas = []
    for f in faixas or []:
        meta = float(f.get('meta_valor', 0) or 0)
        perc = float(f.get('percentual', 0) or 0)
        bon = float(f.get('bonificacao', 0) or 0)
        verba = float(f.get('verba_comercial', 0) or 0)
        obs = str(f.get('observacao', '') or '')
        if meta > 0 or perc > 0 or bon > 0 or verba > 0 or obs.strip():
            limpas.append({'faixa': len(limpas) + 1, 'meta_valor': meta, 'percentual': perc, 'bonificacao': bon, 'verba_comercial': verba, 'observacao': obs})
    return limpas


def inserir_faixas(con, negociacao_id, faixas):
    con.execute('DELETE FROM negociacao_faixas WHERE negociacao_id=?', (int(negociacao_id),))
    for f in normalizar_faixas(faixas):
        con.execute('''
            INSERT INTO negociacao_faixas (negociacao_id, faixa, meta_valor, percentual, bonificacao, verba_comercial, observacao)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (int(negociacao_id), int(f['faixa']), f['meta_valor'], f['percentual'], f['bonificacao'], f['verba_comercial'], f['observacao']))


def carregar_faixas(negociacao_id):
    with connect() as con:
        try:
            return pd.read_sql_query('SELECT * FROM negociacao_faixas WHERE negociacao_id=? ORDER BY meta_valor, faixa', con, params=(int(negociacao_id),))
        except Exception:
            return pd.DataFrame()



def inserir_produtos_negociacao(con, negociacao_id, produtos):
    con.execute('DELETE FROM negociacao_produtos WHERE negociacao_id=?', (int(negociacao_id),))
    for p in produtos or []:
        nome = str(p.get('produto', '') or '').strip()
        ean = somente_digitos(p.get('ean', '') or '')
        codigo_interno = somente_digitos(p.get('codigo_interno', p.get('cod_interno', '')) or '')
        tipo_investimento = str(p.get('tipo_investimento', '') or 'Sell Out').strip() or 'Sell Out'
        meta_compra_qtd = float(p.get('meta_compra_qtd', 0) or 0)
        meta_venda_qtd = float(p.get('meta_venda_qtd', p.get('meta_qtd', 0)) or 0)
        meta_venda_valor = float(p.get('meta_venda_valor', 0) or 0)
        valor_unitario_sellin = float(p.get('valor_unitario_sellin', 0) or 0)
        valor_unitario_sellout = float(p.get('valor_unitario_sellout', p.get('valor_unitario', 0)) or 0)
        percentual_sellin = float(p.get('percentual_sellin', 0) or 0)
        percentual_sellout = float(p.get('percentual_sellout', p.get('percentual', 0)) or 0)
        obs = str(p.get('observacao', '') or '')
        meta_qtd = meta_venda_qtd
        valor_unitario = valor_unitario_sellout
        percentual = percentual_sellout
        if nome or ean or codigo_interno or meta_compra_qtd or meta_venda_qtd or meta_venda_valor or valor_unitario_sellin or valor_unitario_sellout or percentual_sellin or percentual_sellout:
            con.execute('''
                INSERT INTO negociacao_produtos (
                    negociacao_id, produto, ean, codigo_interno, meta_qtd, valor_unitario, percentual, observacao,
                    tipo_investimento, meta_compra_qtd, meta_venda_qtd, meta_venda_valor,
                    valor_unitario_sellin, valor_unitario_sellout,
                    percentual_sellin, percentual_sellout
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                int(negociacao_id), nome.upper(), ean, codigo_interno, meta_qtd, valor_unitario, percentual, obs,
                tipo_investimento, meta_compra_qtd, meta_venda_qtd, meta_venda_valor,
                valor_unitario_sellin, valor_unitario_sellout,
                percentual_sellin, percentual_sellout
            ))


def carregar_produtos_negociacao(negociacao_id):
    with connect() as con:
        try:
            return pd.read_sql_query('SELECT * FROM negociacao_produtos WHERE negociacao_id=? ORDER BY id', con, params=(int(negociacao_id),))
        except Exception:
            return pd.DataFrame()


def _coluna_por_nomes(df, nomes):
    cols_norm = {str(c).lower().strip(): c for c in df.columns}
    for n in nomes:
        key = str(n).lower().strip()
        if key in cols_norm:
            return cols_norm[key]
    for c in df.columns:
        c_norm = str(c).lower().strip()
        for n in nomes:
            if str(n).lower().strip() in c_norm:
                return c
    return None


def preparar_produtos_negociacao_df(df):
    cols = ['produto','ean','codigo_interno','tipo_investimento','meta_compra_qtd','meta_venda_qtd','meta_venda_valor','valor_unitario_sellin','valor_unitario_sellout','percentual_sellin','percentual_sellout','observacao']
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    col_prod = _coluna_por_nomes(df, ['produto', 'embalagem', 'descricao', 'descrição', 'item'])
    col_ean = _coluna_por_nomes(df, ['ean', 'gtin', 'codigo barras', 'código barras', 'codigobarras'])
    col_codigo = _coluna_por_nomes(df, ['codigo interno', 'código interno', 'cod_interno', 'codigo_interno', 'codigo produto', 'produto codigo'])
    col_tipo = _coluna_por_nomes(df, ['tipo investimento', 'tipo_investimento', 'investimento', 'sell in sell out'])
    col_meta_compra = _coluna_por_nomes(df, ['meta compra', 'meta sell in', 'meta si', 'meta_compra_qtd'])
    col_meta_venda = _coluna_por_nomes(df, ['meta venda qtd', 'meta venda quantidade', 'meta sell out qtd', 'meta so qtd', 'meta_venda_qtd', 'meta qtd', 'meta_qtd', 'qtd meta', 'quantidade meta'])
    col_meta_venda_valor = _coluna_por_nomes(df, ['meta venda valor', 'meta venda r$', 'meta sell out valor', 'meta sell out r$', 'meta so valor', 'meta_venda_valor', 'meta valor venda', 'valor meta venda', 'meta financeira venda'])
    col_valor_si = _coluna_por_nomes(df, ['r$ si', 'r$ sell in', 'valor unitario sell in', 'valor_unitario_sellin'])
    col_valor_so = _coluna_por_nomes(df, ['r$ so', 'r$ sell out', 'valor unitario sell out', 'valor_unitario_sellout', 'r$', 'valor unitario', 'valor unitário', 'valor_unitario'])
    col_perc_si = _coluna_por_nomes(df, ['% si', '% sell in', 'percentual sell in', 'percentual_sellin'])
    col_perc_so = _coluna_por_nomes(df, ['% so', '% sell out', 'percentual sell out', 'percentual_sellout', '%', 'percentual', 'perc', '% acordo', 'beneficio', 'benefício'])
    col_obs = _coluna_por_nomes(df, ['observacao', 'observação', 'obs'])
    out = pd.DataFrame()
    out['produto'] = df[col_prod].astype(str).str.upper().str.strip() if col_prod else ''
    out['ean'] = df[col_ean].map(somente_digitos) if col_ean else ''
    out['codigo_interno'] = df[col_codigo].map(somente_digitos) if col_codigo else ''
    out['tipo_investimento'] = df[col_tipo].astype(str).str.strip() if col_tipo else 'Sell Out'
    out['meta_compra_qtd'] = to_numero(df[col_meta_compra]).fillna(0) if col_meta_compra else 0
    out['meta_venda_qtd'] = to_numero(df[col_meta_venda]).fillna(0) if col_meta_venda else 0
    out['meta_venda_valor'] = to_numero(df[col_meta_venda_valor]).fillna(0) if col_meta_venda_valor else 0
    out['valor_unitario_sellin'] = to_numero(df[col_valor_si]).fillna(0) if col_valor_si else 0
    out['valor_unitario_sellout'] = to_numero(df[col_valor_so]).fillna(0) if col_valor_so else 0
    out['percentual_sellin'] = to_numero(df[col_perc_si]).fillna(0) if col_perc_si else 0
    out['percentual_sellout'] = to_numero(df[col_perc_so]).fillna(0) if col_perc_so else 0
    out['observacao'] = df[col_obs].astype(str).fillna('').str.strip() if col_obs else ''
    out = out[(out['produto'].astype(str).str.strip() != '') | (out['ean'].astype(str).str.strip() != '')].copy()
    return out[cols]


def ler_produtos_negociacao_upload(file):
    cols = ['produto','ean','codigo_interno','tipo_investimento','meta_compra_qtd','meta_venda_qtd','meta_venda_valor','valor_unitario_sellin','valor_unitario_sellout','percentual_sellin','percentual_sellout','observacao']
    if file is None:
        return pd.DataFrame(columns=cols)
    if file.name.lower().endswith('.csv'):
        df = pd.read_csv(file, sep=None, engine='python')
    else:
        df = pd.read_excel(file)
    return preparar_produtos_negociacao_df(df)


def registros_produtos_de_df(df):
    if df is None or df.empty:
        return []
    cols = ['produto','ean','codigo_interno','tipo_investimento','meta_compra_qtd','meta_venda_qtd','meta_venda_valor','valor_unitario_sellin','valor_unitario_sellout','percentual_sellin','percentual_sellout','observacao']
    for c in cols:
        if c not in df.columns:
            df[c] = '' if c in ['produto','ean','tipo_investimento','observacao'] else 0
    return df[cols].to_dict('records')


def get_produtos_por_nome(tipo, nomes):
    campo = 'fabricante' if tipo == 'Fabricante' else 'fornecedor'
    df = carregar_cache_compras()
    if df.empty or campo not in df.columns or 'produto' not in df.columns:
        return pd.DataFrame(columns=['produto', 'ean', 'codigo_interno'])
    nomes_norm = [str(n).upper().strip() for n in nomes or [] if str(n).strip()]
    if nomes_norm:
        df = df[df[campo].astype(str).str.upper().str.strip().isin(nomes_norm)]
    cols = ['produto'] + (['ean'] if 'ean' in df.columns else []) + (['codigo_interno'] if 'codigo_interno' in df.columns else [])
    out = df[cols].dropna(subset=['produto']).copy()
    out['produto'] = out['produto'].astype(str).str.upper().str.strip()
    if 'ean' not in out.columns:
        out['ean'] = ''
    if 'codigo_interno' not in out.columns:
        out['codigo_interno'] = ''
    out['ean'] = out['ean'].map(somente_digitos)
    out['codigo_interno'] = out['codigo_interno'].map(somente_digitos)
    out = out[out['produto'] != ''].drop_duplicates(['produto', 'ean', 'codigo_interno']).sort_values('produto')
    return out


def calcular_apuracao_produtos(compras, negociacoes, data_ini, data_fim, vendas=None):
    """Apuração por produto estável e precisa.

    Regra correta para Sell Out:
    1) Usa o código interno gravado no produto da negociação.
    2) Se não houver código interno, identifica o código interno pela base de ENTRADAS
       usando EAN e depois descrição normalizada.
    3) Com o código interno resolvido, busca a VENDA.
    4) Se ainda não encontrar, usa EAN como contingência.

    Isso evita depender de descrição na venda e aproveita a entrada, que já está
    encontrando os produtos da negociação.
    """
    vendas = carregar_cache_vendas() if vendas is None else vendas
    compras = compras.copy() if compras is not None else carregar_cache_compras()
    vendas = vendas.copy() if vendas is not None else pd.DataFrame()
    if negociacoes.empty:
        return pd.DataFrame()

    def _prep(base, data_col, qtd_col, valor_col, cod_col='codigo_interno'):
        if base is None or base.empty:
            return pd.DataFrame()
        b = base.copy()
        if qtd_col not in b.columns:
            b[qtd_col] = 0
        if valor_col not in b.columns:
            b[valor_col] = 0
        if 'ean' not in b.columns:
            b['ean'] = ''
        if cod_col not in b.columns:
            alt = 'cod_interno' if cod_col == 'codigo_interno' else 'codigo_interno'
            b[cod_col] = b[alt] if alt in b.columns else ''
        if 'produto' not in b.columns:
            b['produto'] = ''
        if data_col not in b.columns:
            return pd.DataFrame()
        b[qtd_col] = to_numero(b[qtd_col]).fillna(0)
        b[valor_col] = to_numero(b[valor_col]).fillna(0)
        b[data_col] = pd.to_datetime(b[data_col], errors='coerce').dt.date
        # Não filtra pelo período geral da tela aqui.
        # A apuração por produto usa a vigência de cada negociação em _filtro_header.
        # Isso evita zerar Sell Out quando a tela está em junho e a negociação é julho.
        b = b[b[data_col].notna()].copy()
        b['ean_norm'] = b['ean'].map(somente_digitos)
        b['cod_norm'] = b[cod_col].map(somente_digitos)
        b['produto_norm'] = b['produto'].map(normalizar_texto_chave)
        return b

    base_si = _prep(compras, 'data_compra', 'qtd_compra', 'valor_compra', cod_col='codigo_interno')
    base_so = _prep(vendas, 'data_venda', 'qtd_venda', 'valor_venda', cod_col='cod_interno')

    n = negociacoes.copy()
    n['data_inicio'] = pd.to_datetime(n['data_inicio'], errors='coerce').dt.date
    n['data_fim'] = pd.to_datetime(n['data_fim'], errors='coerce').dt.date
    n = n[n['status'].eq('Ativo')]
    rows = []

    def _filtro_header(base, neg, data_col):
        if base.empty:
            return base
        b = base[(base[data_col] >= neg['data_inicio']) & (base[data_col] <= neg['data_fim'])].copy()
        campo = 'fabricante' if neg.get('tipo') == 'Fabricante' else 'fornecedor'
        # Vendas normalmente não têm fabricante/fornecedor. Só aplica filtro se o campo existir e estiver preenchido.
        if campo in b.columns and b[campo].astype(str).str.strip().ne('').any():
            b = b[b[campo].astype(str).str.upper().str.strip().eq(str(neg.get('nome','')).upper().strip())]
        return b

    def _resumos(base, qtd_col, valor_col):
        vazio = {'cod': {}, 'ean': {}, 'produto': {}, 'ean_to_cod': {}, 'produto_to_cod': {}}
        if base.empty:
            return vazio
        res = {}
        for key in ['cod_norm', 'ean_norm', 'produto_norm']:
            bb = base[base[key].astype(str).str.strip().ne('')].copy()
            if bb.empty:
                res[key] = {}
                continue
            g = bb.groupby(key, dropna=False).agg({qtd_col:'sum', valor_col:'sum'}).reset_index()
            cnt = bb.groupby(key).size().reset_index(name='registros')
            g = g.merge(cnt, on=key, how='left')
            res[key] = g.set_index(key).to_dict('index')

        def _mapa_cod_por(chave_col):
            bb = base[(base[chave_col].astype(str).str.strip().ne('')) & (base['cod_norm'].astype(str).str.strip().ne(''))].copy()
            if bb.empty:
                return {}
            # Escolhe o código mais frequente para cada EAN/descrição; em empate fica o primeiro.
            m = (bb.groupby([chave_col, 'cod_norm']).size()
                   .reset_index(name='n')
                   .sort_values([chave_col, 'n'], ascending=[True, False])
                   .drop_duplicates(chave_col))
            return dict(zip(m[chave_col].astype(str), m['cod_norm'].astype(str)))

        return {
            'cod': res.get('cod_norm', {}),
            'ean': res.get('ean_norm', {}),
            'produto': res.get('produto_norm', {}),
            'ean_to_cod': _mapa_cod_por('ean_norm'),
            'produto_to_cod': _mapa_cod_por('produto_norm'),
        }

    def _buscar(resumos, codigo, ean, produto, qtd_col, valor_col, permitir_descricao=True):
        codigo = somente_digitos(codigo)
        ean = somente_digitos(ean)
        produto_key = normalizar_texto_chave(produto)
        tentativas = [
            ('Código interno', codigo, resumos.get('cod', {})),
            ('EAN', ean, resumos.get('ean', {})),
        ]
        if permitir_descricao:
            tentativas.append(('Descrição exata', produto_key, resumos.get('produto', {})))
        for metodo, chave, bucket in tentativas:
            if chave and chave in bucket:
                item = bucket[chave]
                return float(item.get(qtd_col, 0) or 0), float(item.get(valor_col, 0) or 0), int(item.get('registros', 0) or 0), metodo
        return 0.0, 0.0, 0, 'Não encontrado'

    def _resolver_codigo_pela_compra(res_si, codigo, ean, produto):
        """Quando a proposta não gravou código interno, usa a entrada encontrada para descobrir o código."""
        codigo = somente_digitos(codigo)
        if codigo:
            return codigo, 'Código informado na negociação'
        ean = somente_digitos(ean)
        produto_key = normalizar_texto_chave(produto)
        if ean and ean in res_si.get('ean_to_cod', {}):
            return res_si['ean_to_cod'][ean], 'Código resolvido pela entrada via EAN'
        if produto_key and produto_key in res_si.get('produto_to_cod', {}):
            return res_si['produto_to_cod'][produto_key], 'Código resolvido pela entrada via descrição'
        return '', 'Código interno não resolvido'

    for _, neg in n.iterrows():
        prod_rules = carregar_produtos_negociacao(int(neg.get('id')))
        if prod_rules.empty:
            continue
        neg_si = _filtro_header(base_si, neg, 'data_compra') if not base_si.empty else pd.DataFrame()
        neg_so = _filtro_header(base_so, neg, 'data_venda') if not base_so.empty else pd.DataFrame()
        res_si = _resumos(neg_si, 'qtd_compra', 'valor_compra')
        res_so = _resumos(neg_so, 'qtd_venda', 'valor_venda')

        for _, pr in prod_rules.iterrows():
            produto_regra = str(pr.get('produto', '') or '').upper().strip()
            ean_regra = somente_digitos(pr.get('ean', '') or '')
            cod_regra_original = somente_digitos(pr.get('codigo_interno', pr.get('cod_interno', '')) or '')
            cod_resolvido, diag_codigo = _resolver_codigo_pela_compra(res_si, cod_regra_original, ean_regra, produto_regra)
            tipo_inv = str(pr.get('tipo_investimento', '') or 'Sell Out')

            # Compra pode usar descrição como fallback, porque ela também serve para resolver o código.
            qtd_si, valor_si, reg_si, chave_si = _buscar(res_si, cod_resolvido or cod_regra_original, ean_regra, produto_regra, 'qtd_compra', 'valor_compra', permitir_descricao=True)
            # Venda deve usar prioritariamente o código resolvido pela entrada; EAN fica como contingência.
            qtd_so, valor_so, reg_so, chave_so = _buscar(res_so, cod_resolvido or cod_regra_original, ean_regra, produto_regra, 'qtd_venda', 'valor_venda', permitir_descricao=False)

            # Último fallback seguro: se o EAN da venda aponta para um código e esse código existe no resumo por código, usa esse código.
            if qtd_so == 0 and ean_regra and ean_regra in res_so.get('ean_to_cod', {}):
                cod_venda_ean = res_so['ean_to_cod'][ean_regra]
                qtd_so, valor_so, reg_so, chave_so = _buscar(res_so, cod_venda_ean, '', '', 'qtd_venda', 'valor_venda', permitir_descricao=False)
                if reg_so:
                    chave_so = 'Código da venda resolvido pelo EAN'

            meta_si = float(pr.get('meta_compra_qtd', 0) or 0)
            meta_so = float(pr.get('meta_venda_qtd', pr.get('meta_qtd', 0)) or 0)
            meta_so_valor = float(pr.get('meta_venda_valor', 0) or 0)
            rs_si = float(pr.get('valor_unitario_sellin', 0) or 0)
            rs_so = float(pr.get('valor_unitario_sellout', pr.get('valor_unitario', 0)) or 0)
            perc_si = float(pr.get('percentual_sellin', 0) or 0)
            perc_so = float(pr.get('percentual_sellout', pr.get('percentual', 0) or 0))
            inv_si = (qtd_si * rs_si) + (valor_si * perc_si / 100)
            inv_so = (qtd_so * rs_so) + (valor_so * perc_so / 100)
            ating_si = (qtd_si / meta_si * 100) if meta_si > 0 else (100 if qtd_si > 0 else 0)
            ating_so = (valor_so / meta_so_valor * 100) if meta_so_valor > 0 else ((qtd_so / meta_so * 100) if meta_so > 0 else (100 if qtd_so > 0 else 0))
            rows.append({
                'Código Negociação': neg.get('codigo_negociacao', ''),
                'Fabricante/Distribuidor': neg.get('nome', ''),
                'Produto': produto_regra,
                'Código Interno': cod_regra_original,
                'Código Usado': cod_resolvido or cod_regra_original,
                'Diagnóstico Código': diag_codigo,
                'EAN': ean_regra,
                'Tipo Investimento': tipo_inv,
                'Meta Sell In': meta_si,
                'Compra Realizada': qtd_si,
                'Valor Compra': valor_si,
                '% Atingido SI': ating_si,
                'Dif SI': max(meta_si - qtd_si, 0) if meta_si > 0 else 0,
                'R$ Un SI': rs_si,
                '% SI': perc_si,
                'Investimento Sell In': inv_si,
                'Meta Sell Out': meta_so,
                'Meta Valor Sell Out': meta_so_valor,
                'Venda Realizada': qtd_so,
                'Valor Vendido': valor_so,
                '% Atingido SO': ating_so,
                'Dif SO': (max(meta_so_valor - valor_so, 0) if meta_so_valor > 0 else (max(meta_so - qtd_so, 0) if meta_so > 0 else 0)),
                'R$ Un SO': rs_so,
                '% SO': perc_so,
                'Investimento Sell Out': inv_so,
                'Investimento': inv_si + inv_so,
                'Registros SI': reg_si,
                'Registros SO': reg_so,
                'Chave Compra': chave_si,
                'Chave Venda': chave_so,
                'Observação Produto': pr.get('observacao', '')
            })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values('Investimento', ascending=False)

def registrar_historico(con, negociacao_id, codigo, campo, anterior, novo, usuario='Paulo', observacao=''):
    con.execute('''
        INSERT INTO historico_negociacoes (negociacao_id, codigo_negociacao, usuario, campo, valor_anterior, valor_novo, observacao)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (negociacao_id, codigo, usuario, campo, str(anterior), str(novo), observacao))


def save_negociacao(tipo, nome, percentual, meta_compra, data_inicio, data_fim, status, observacao, tipo_negociacao='Desconto percentual', tipo_investimento='Sell Out', bonificacao=0, verba_comercial=0, faixas=None, produtos=None, usuario='Paulo'):
    with connect() as con:
        codigo = proximo_codigo_negociacao(con)
        codigo_curto = gerar_codigo_curto(nome, int(codigo.split('-')[-1]))
        cur = con.execute('''
            INSERT INTO negociacoes (codigo_negociacao, codigo_curto, tipo, nome, percentual, meta_compra, data_inicio, data_fim, status, observacao, tipo_negociacao, tipo_investimento, bonificacao, verba_comercial, atualizado_em, criado_por, atualizado_por)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (codigo, codigo_curto, tipo, nome.strip().upper(), percentual, float(meta_compra or 0), str(data_inicio), str(data_fim), status, observacao, tipo_negociacao, tipo_investimento, float(bonificacao or 0), float(verba_comercial or 0), datetime.now().strftime('%Y-%m-%d %H:%M:%S'), usuario, usuario))
        negociacao_id = cur.lastrowid
        if tipo_negociacao in ['Faixa de meta', 'Híbrida']:
            inserir_faixas(con, negociacao_id, faixas or [])
        inserir_produtos_negociacao(con, negociacao_id, produtos or [])
        if produtos:
            registrar_historico(con, negociacao_id, codigo, 'Produtos negociados', '', produtos, usuario=usuario, observacao='Lançamento por produto')
        registrar_historico(con, negociacao_id, codigo, 'Lançamento da negociação', '', 'Negociação criada', usuario=usuario, observacao=observacao)
        if not tipo_negociacao_faz_apuracao(tipo_negociacao):
            valor_credito_manual = float(verba_comercial or 0) or float(meta_compra or 0)
            criar_credito_financeiro_negociacao_manual(con, negociacao_id, codigo, tipo, nome.strip().upper(), tipo_negociacao, valor_credito_manual, data_inicio, str(data_inicio)[:7], usuario, observacao)
            registrar_historico(con, negociacao_id, codigo, 'Crédito financeiro automático', '', valor_credito_manual, usuario=usuario, observacao='Negociação sem apuração')
        con.commit()
    try:
        atualizar_cache_apuracao_negociacao(negociacao_id, usuario=usuario, motivo='Negociação criada')
    except Exception:
        pass
    return codigo


def save_negociacoes_multiplas(tipo, nomes, percentual, meta_compra, data_inicio, data_fim, status, observacao, tipo_negociacao='Desconto percentual', tipo_investimento='Sell Out', bonificacao=0, verba_comercial=0, faixas=None, produtos=None, usuario='Paulo'):
    nomes_limpos = [str(n).strip().upper() for n in nomes if str(n).strip()]
    if not nomes_limpos:
        return []
    codigos = []
    ids_criados = []
    with connect() as con:
        for nome in nomes_limpos:
            codigo = proximo_codigo_negociacao(con)
            codigo_curto = gerar_codigo_curto(nome, int(codigo.split('-')[-1]))
            cur = con.execute('''
                INSERT INTO negociacoes (codigo_negociacao, codigo_curto, tipo, nome, percentual, meta_compra, data_inicio, data_fim, status, observacao, tipo_negociacao, tipo_investimento, bonificacao, verba_comercial, atualizado_em, criado_por, atualizado_por)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (codigo, codigo_curto, tipo, nome, percentual, float(meta_compra or 0), str(data_inicio), str(data_fim), status, observacao, tipo_negociacao, tipo_investimento, float(bonificacao or 0), float(verba_comercial or 0), datetime.now().strftime('%Y-%m-%d %H:%M:%S'), usuario, usuario))
            negociacao_id = cur.lastrowid
            if tipo_negociacao in ['Faixa de meta', 'Híbrida']:
                inserir_faixas(con, negociacao_id, faixas or [])
            inserir_produtos_negociacao(con, negociacao_id, produtos or [])
            if produtos:
                registrar_historico(con, negociacao_id, codigo, 'Produtos negociados', '', produtos, usuario=usuario, observacao='Lançamento por produto')
            registrar_historico(con, negociacao_id, codigo, 'Lançamento da negociação', '', 'Negociação criada', usuario=usuario, observacao=observacao)
            if not tipo_negociacao_faz_apuracao(tipo_negociacao):
                valor_credito_manual = float(verba_comercial or 0) or float(meta_compra or 0)
                criar_credito_financeiro_negociacao_manual(con, negociacao_id, codigo, tipo, nome, tipo_negociacao, valor_credito_manual, data_inicio, str(data_inicio)[:7], usuario, observacao)
                registrar_historico(con, negociacao_id, codigo, 'Crédito financeiro automático', '', valor_credito_manual, usuario=usuario, observacao='Negociação sem apuração')
            codigos.append(codigo)
            ids_criados.append(negociacao_id)
        con.commit()
    try:
        atualizar_cache_apuracao_negociacoes(ids_criados, usuario=usuario, motivo='Negociação criada')
    except Exception:
        pass
    return codigos


def atualizar_negociacao(negociacao_id, dados, faixas=None, produtos=None, usuario='Paulo'):
    with connect() as con:
        atual = con.execute('SELECT * FROM negociacoes WHERE id=?', (int(negociacao_id),)).fetchone()
        cols = [d[0] for d in con.execute('SELECT * FROM negociacoes LIMIT 0').description]
        if not atual:
            return False
        atual_dict = dict(zip(cols, atual))
        codigo = atual_dict.get('codigo_negociacao', '')
        campos = ['percentual','meta_compra','data_inicio','data_fim','status','observacao','tipo_negociacao','tipo_investimento','bonificacao','verba_comercial']
        for campo in campos:
            novo = dados.get(campo, atual_dict.get(campo))
            anterior = atual_dict.get(campo)
            if str(anterior) != str(novo):
                registrar_historico(con, negociacao_id, codigo, campo, anterior, novo, usuario=usuario, observacao='Alteração pós-lançamento')
        con.execute('''
            UPDATE negociacoes
            SET percentual=?, meta_compra=?, data_inicio=?, data_fim=?, status=?, observacao=?, tipo_negociacao=?, tipo_investimento=?, bonificacao=?, verba_comercial=?, atualizado_em=?, atualizado_por=?
            WHERE id=?
        ''', (float(dados.get('percentual', 0) or 0), float(dados.get('meta_compra', 0) or 0), str(dados.get('data_inicio')), str(dados.get('data_fim')), dados.get('status'), dados.get('observacao',''), dados.get('tipo_negociacao'), dados.get('tipo_investimento','Sell Out'), float(dados.get('bonificacao',0) or 0), float(dados.get('verba_comercial',0) or 0), datetime.now().strftime('%Y-%m-%d %H:%M:%S'), usuario, int(negociacao_id)))
        if dados.get('tipo_negociacao') in ['Faixa de meta', 'Híbrida']:
            anteriores = pd.read_sql_query('SELECT faixa, meta_valor, percentual, bonificacao, verba_comercial, observacao FROM negociacao_faixas WHERE negociacao_id=? ORDER BY faixa', con, params=(int(negociacao_id),)).to_dict('records')
            novos = normalizar_faixas(faixas or [])
            if str(anteriores) != str(novos):
                registrar_historico(con, negociacao_id, codigo, 'Faixas de meta', anteriores, novos, usuario=usuario, observacao='Alteração das faixas')
            inserir_faixas(con, negociacao_id, faixas or [])
        else:
            con.execute('DELETE FROM negociacao_faixas WHERE negociacao_id=?', (int(negociacao_id),))
        anteriores_prod = pd.read_sql_query('SELECT produto, ean, meta_qtd, valor_unitario, percentual, observacao FROM negociacao_produtos WHERE negociacao_id=? ORDER BY id', con, params=(int(negociacao_id),)).to_dict('records')
        novos_prod = produtos or []
        if str(anteriores_prod) != str(novos_prod):
            registrar_historico(con, negociacao_id, codigo, 'Produtos negociados', anteriores_prod, novos_prod, usuario=usuario, observacao='Alteração dos produtos da negociação')
        inserir_produtos_negociacao(con, negociacao_id, novos_prod)
        if not tipo_negociacao_faz_apuracao(dados.get('tipo_negociacao')):
            valor_credito_manual = float(dados.get('verba_comercial',0) or 0) or float(dados.get('meta_compra',0) or 0)
            criar_credito_financeiro_negociacao_manual(con, int(negociacao_id), codigo, atual_dict.get('tipo',''), atual_dict.get('nome',''), dados.get('tipo_negociacao'), valor_credito_manual, dados.get('data_inicio'), str(dados.get('data_inicio'))[:7], usuario, dados.get('observacao',''))
        con.commit()
    try:
        atualizar_cache_apuracao_negociacao(negociacao_id, usuario=usuario, motivo='Negociação alterada')
    except Exception:
        pass
    return True


def excluir_negociacao(negociacao_id, usuario='Paulo', motivo='Exclusão manual pela tela de cadastro'):
    """Exclusão lógica: remove a negociação da apuração e das telas principais sem perder auditoria."""
    with connect() as con:
        atual = con.execute('SELECT * FROM negociacoes WHERE id=?', (int(negociacao_id),)).fetchone()
        cols = [d[0] for d in con.execute('SELECT * FROM negociacoes LIMIT 0').description]
        if not atual:
            return False
        atual_dict = dict(zip(cols, atual))
        codigo = atual_dict.get('codigo_negociacao', '')
        nome = atual_dict.get('nome', '')
        registrar_historico(con, negociacao_id, codigo, 'Exclusão', nome, 'Negociação excluída', usuario=usuario, observacao=motivo)
        con.execute('''
            UPDATE negociacoes
            SET status='Excluída', excluido_em=?, excluido_por=?, atualizado_em=?, atualizado_por=?
            WHERE id=?
        ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), usuario, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), usuario, int(negociacao_id)))
        con.commit()
    try:
        atualizar_cache_apuracao_negociacao(negociacao_id, usuario=usuario, motivo='Negociação excluída')
    except Exception:
        pass
    return True


@st.cache_data(ttl=3600, show_spinner=False)
def get_opcoes_cadastro(tipo):
    """Busca fabricantes/distribuidores já existentes na base de compras em cache externo."""
    campo = 'fabricante' if tipo == 'Fabricante' else 'fornecedor'
    try:
        df = carregar_cache_compras()
        if df.empty or campo not in df.columns:
            return []
        nomes = (
            df[campo].dropna().astype(str).str.upper().str.strip()
            .replace('', pd.NA).dropna().drop_duplicates().sort_values().tolist()
        )
        return nomes
    except Exception:
        return []

def normalize_columns(df):
    mapping = {}
    cols = {c.lower().strip(): c for c in df.columns}
    candidates = {
        'data_compra': ['data_compra', 'data compra', 'data_emissao', 'data emissão', 'datahoraemissao', 'data', 'data entrada', 'data_entrada', 'datahoraentrada'],
        'fabricante': ['fabricante', 'laboratorio', 'laboratório', 'industria', 'indústria'],
        'fornecedor': ['fornecedor', 'distribuidor', 'razao social', 'razão social', 'razaosocial', 'razaosocial_fornecedor', 'fornecedor_nome'],
        'produto': ['produto', 'descricao', 'descrição', 'descricao_embalagem'],
        'ean': ['ean', 'codigobarras', 'codigo barras', 'código barras', 'gtin'],
        'valor_compra': ['valor_compra', 'valor compra', 'valor_entrada', 'valor entrada', 'valor_nf_total', 'valor total entrada', 'valor total', 'total', 'valor']
    }
    for target, names in candidates.items():
        for n in names:
            if n in cols:
                mapping[cols[n]] = target
                break
    return df.rename(columns=mapping)


def _norm_name(col):
    return str(col).lower().strip().replace(' ', '_').replace('ã', 'a').replace('ç', 'c').replace('é', 'e').replace('á', 'a').replace('ó', 'o')


def deduplicar_colunas(df):
    """Evita erro quando o SQL retorna colunas com o mesmo alias.
    Ex.: valor_nf_total aparece duas vezes no script. O pandas aceita, mas
    base['valor_nf_total'] vira um DataFrame e quebra o pd.to_numeric.
    """
    if df is None or df.empty:
        return df
    return df.loc[:, ~pd.Index(df.columns).duplicated()].copy()


def serie_coluna(df, col):
    """Retorna sempre uma Série, mesmo se houver coluna duplicada."""
    dado = df[col]
    if isinstance(dado, pd.DataFrame):
        return dado.iloc[:, 0]
    return dado


def somente_digitos(valor):
    """Normaliza códigos vindos do Excel/PostgreSQL.
    Ex.: 7891058168049.0 -> 7891058168049; 1.234E+12 -> número sem pontuação.
    """
    if pd.isna(valor):
        return ''
    txt = str(valor).strip()
    if txt.lower() in ['', 'nan', 'none', 'nat']:
        return ''
    try:
        # Corrige valores lidos como float/notação científica sem perder EAN/código.
        if any(ch in txt.lower() for ch in ['e+', 'e-']) or txt.endswith('.0'):
            from decimal import Decimal
            txt = format(Decimal(txt), 'f')
    except Exception:
        pass
    if txt.endswith('.0'):
        txt = txt[:-2]
    return ''.join(ch for ch in txt if ch.isdigit())




def normalizar_codigo(valor):
    """Normaliza código interno para comparação e evita erro no cadastro por produto.
    Mantém apenas dígitos, removendo .0 e notação científica quando vier do Excel.
    """
    return somente_digitos(valor)


def normalizar_ean(valor):
    """Normaliza EAN/código de barras para comparação.
    Mantém apenas dígitos para permitir cruzamento entre Excel, entrada e venda.
    """
    return somente_digitos(valor)

def normalizar_texto_chave(valor):
    """Chave textual apenas para diagnóstico/fallback exato normalizado."""
    import unicodedata, re
    if pd.isna(valor):
        return ''
    txt = unicodedata.normalize('NFKD', str(valor).upper())
    txt = ''.join(ch for ch in txt if not unicodedata.combining(ch))
    txt = txt.replace('COMPRIMIDOS', 'CP').replace('COMPRIMIDO', 'CP').replace(' CAPSULAS', ' CAP').replace('CÁPSULAS','CAP')
    txt = re.sub(r'[^A-Z0-9]+', ' ', txt)
    txt = re.sub(r'\s+', ' ', txt).strip()
    return txt


def preparar_entrada_sql(df):
    """Prepara a saída do script ENTRADAS_SB.sql.
    Fabricante vem de laboratorio; Distribuidor vem de fornecedor.
    Compra base usa o valor de entrada da nota, priorizando: valor_entrada/valor_nf_total;
    se não existir, usa valor_nf_unitario x quantidade; por último, custo_final_R$ x quantidade.
    """
    base = deduplicar_colunas(df.copy())
    norm_cols = {_norm_name(c): c for c in base.columns}

    def pick(*names):
        for n in names:
            if _norm_name(n) in norm_cols:
                return norm_cols[_norm_name(n)]
        return None

    col_data = pick('data_emissao', 'datahoraemissao', 'data_compra', 'data_entrada', 'datahoraentrada')
    col_fab = pick('laboratorio', 'fabricante')
    col_forn = pick('fornecedor', 'distribuidor')
    col_prod = pick('descricao_embalagem', 'produto', 'descricao')
    col_ean = pick('codigobarras', 'ean', 'gtin')
    col_cod = pick('cod_interno', 'codigo_interno', 'codigo_produto', 'produto_codigo', 'codigo')
    col_qtd = pick('quantidade_por_produto', 'quantidade', 'qtd_nf')
    col_custo_final = pick('custo_final_R$', 'custo_final_r$', 'custo_final')
    col_valor_unitario = pick('valor_nf_unitario', 'valor_unitario', 'valorunitario')
    col_valor = pick('valor_compra', 'valor_entrada', 'valor_nf_total', 'valor_total_entrada', 'valor_total')

    out = pd.DataFrame()
    if col_data: out['data_compra'] = serie_coluna(base, col_data)
    if col_fab: out['fabricante'] = serie_coluna(base, col_fab)
    if col_forn: out['fornecedor'] = serie_coluna(base, col_forn)
    if col_prod: out['produto'] = serie_coluna(base, col_prod)
    if col_ean: out['ean'] = serie_coluna(base, col_ean)
    if col_cod: out['codigo_interno'] = serie_coluna(base, col_cod)
    if col_qtd:
        out['qtd_compra'] = to_numero(serie_coluna(base, col_qtd)).fillna(0)
    else:
        out['qtd_compra'] = 0

    if col_valor:
        out['valor_compra'] = to_numero(serie_coluna(base, col_valor))
        out['fonte_valor'] = str(col_valor)
    elif col_valor_unitario and col_qtd:
        valor_unitario = to_numero(serie_coluna(base, col_valor_unitario)).fillna(0)
        qtd = to_numero(serie_coluna(base, col_qtd)).fillna(0)
        out['valor_compra'] = valor_unitario * qtd
        out['fonte_valor'] = f'{col_valor_unitario} x {col_qtd}'
    elif col_custo_final and col_qtd:
        custo = to_numero(serie_coluna(base, col_custo_final)).fillna(0)
        qtd = to_numero(serie_coluna(base, col_qtd)).fillna(0)
        out['valor_compra'] = custo * qtd
        out['fonte_valor'] = f'{col_custo_final} x {col_qtd}'
    else:
        out['valor_compra'] = 0
        out['fonte_valor'] = 'Não identificado'

    return out



def preparar_venda_sql(df):
    """Prepara a saída do script de vendas informado pelo usuário.
    Normaliza para: data_venda, produto, ean, qtd_venda, valor_venda e campos auxiliares.
    """
    base = deduplicar_colunas(df.copy())
    norm_cols = {_norm_name(c): c for c in base.columns}

    def pick(*names):
        for n in names:
            if _norm_name(n) in norm_cols:
                return norm_cols[_norm_name(n)]
        return None

    col_data = pick('datahora_venda_final', 'data_venda', 'datahora', 'data')
    col_loja = pick('loja')
    col_usuario = pick('usuario_orcamento')
    col_vendaid = pick('vendaid')
    col_prod = pick('descricao', 'produto', 'descricao_embalagem')
    col_ean = pick('codigobarras', 'ean', 'gtin')
    col_cod = pick('cod_interno', 'codigo', 'produto_codigo')
    col_class = pick('classificacaoo', 'classificacao', 'familia')
    col_qtd = pick('quantidade', 'qtd_venda', 'qtd')
    col_valor = pick('valortotal', 'valor_venda', 'valor_total')
    col_unit = pick('valorunitario', 'valor_unitario')
    col_custo = pick('custo')
    col_lucro = pick('lucro')
    col_lucro_perc = pick('lucro_perc')
    col_pbm = pick('programa_pbm')
    col_pec = pick('pec')
    col_coo = pick('coo')
    col_media_dia = pick('media_venda_dia')
    col_media_mes = pick('media_venda_mes')

    out = pd.DataFrame()
    if col_data: out['data_venda'] = serie_coluna(base, col_data)
    if col_loja: out['loja'] = serie_coluna(base, col_loja)
    if col_usuario: out['usuario_orcamento'] = serie_coluna(base, col_usuario)
    if col_vendaid: out['vendaid'] = serie_coluna(base, col_vendaid)
    if col_prod: out['produto'] = serie_coluna(base, col_prod)
    if col_ean: out['ean'] = serie_coluna(base, col_ean)
    if col_cod: out['codigo_interno'] = serie_coluna(base, col_cod)
    if col_cod: out['cod_interno'] = serie_coluna(base, col_cod)
    if col_class: out['classificacao'] = serie_coluna(base, col_class)
    out['qtd_venda'] = to_numero(serie_coluna(base, col_qtd)).fillna(0) if col_qtd else 0
    out['valor_venda'] = to_numero(serie_coluna(base, col_valor)).fillna(0) if col_valor else 0
    out['valor_unitario'] = to_numero(serie_coluna(base, col_unit)).fillna(0) if col_unit else 0
    out['custo'] = to_numero(serie_coluna(base, col_custo)).fillna(0) if col_custo else 0
    out['lucro'] = to_numero(serie_coluna(base, col_lucro)).fillna(0) if col_lucro else 0
    out['lucro_perc'] = to_numero(serie_coluna(base, col_lucro_perc)).fillna(0) if col_lucro_perc else 0
    if col_pbm: out['programa_pbm'] = serie_coluna(base, col_pbm)
    if col_pec: out['pec'] = serie_coluna(base, col_pec)
    if col_coo: out['coo'] = serie_coluna(base, col_coo)
    out['media_venda_dia'] = to_numero(serie_coluna(base, col_media_dia)).fillna(0) if col_media_dia else 0
    out['media_venda_mes'] = to_numero(serie_coluna(base, col_media_mes)).fillna(0) if col_media_mes else 0
    return out


def insert_vendas(df, origem, substituir_origem=False):
    df_original = df.copy()
    df = preparar_venda_sql(df)
    if 'data_venda' not in df.columns:
        raise ValueError(f'Coluna obrigatória não encontrada: data_venda. Colunas recebidas: {list(df_original.columns)}')
    for c in ['loja','usuario_orcamento','vendaid','fabricante','fornecedor','produto','ean','cod_interno','classificacao','programa_pbm','pec','coo']:
        if c not in df.columns:
            df[c] = ''
    for c in ['qtd_venda','valor_venda','valor_unitario','custo','lucro','lucro_perc','media_venda_dia','media_venda_mes']:
        if c not in df.columns:
            df[c] = 0
    cols = ['data_venda','loja','usuario_orcamento','vendaid','fabricante','fornecedor','produto','ean','cod_interno','classificacao','qtd_venda','valor_venda','valor_unitario','custo','lucro','lucro_perc','programa_pbm','pec','coo','media_venda_dia','media_venda_mes']
    df = df[cols].copy()
    df['data_venda'] = pd.to_datetime(df['data_venda'], errors='coerce').dt.date.astype(str)
    df['produto'] = df['produto'].fillna('').astype(str).str.upper().str.strip()
    df['ean'] = df['ean'].map(somente_digitos)
    df['cod_interno'] = df['cod_interno'].map(somente_digitos)
    for c in ['qtd_venda','valor_venda','valor_unitario','custo','lucro','lucro_perc','media_venda_dia','media_venda_mes']:
        df[c] = to_numero(df[c]).fillna(0)
    df['origem_arquivo'] = origem
    antes = len(df)
    soma_antes = float(df['valor_venda'].sum()) if 'valor_venda' in df else 0
    df = df[(df['data_venda'] != 'NaT') & ((df['valor_venda'] > 0) | (df['qtd_venda'] > 0))]
    if df.empty:
        raise ValueError(f'O SQL/arquivo de vendas executou, mas nenhum registro válido foi importado. Registros recebidos: {antes}; Soma vendas antes do filtro: {soma_antes:.2f}; Colunas recebidas: {list(df_original.columns)}')
    with UPDATE_LOCK:
        cache_atual = carregar_cache_vendas()
        if substituir_origem and not cache_atual.empty and 'origem_arquivo' in cache_atual.columns:
            cache_atual = cache_atual[~cache_atual['origem_arquivo'].isin(['VENDAS_SB.sql', 'CACHE_SQL_VENDAS_SB'])].copy()
        cache_final = df.copy() if cache_atual.empty else pd.concat([cache_atual, df], ignore_index=True)
        formato = salvar_cache_vendas(cache_final)
    set_config('last_vendas_registros', len(df))
    set_config('last_vendas_valor', round(float(df['valor_venda'].sum()), 2))
    set_config('last_vendas_datahora', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    set_config('last_vendas_cache_format', formato)
    return len(df)

def insert_compras(df, origem, substituir_origem=False):
    df_original = df.copy()
    df = preparar_entrada_sql(df)
    required = ['data_compra', 'valor_compra']
    for r in required:
        if r not in df.columns:
            raise ValueError(f'Coluna obrigatória não encontrada: {r}. Colunas recebidas do SQL: {list(df_original.columns)}')
    for c in ['fabricante', 'fornecedor', 'produto', 'ean', 'codigo_interno', 'qtd_compra', 'fonte_valor']:
        if c not in df.columns:
            df[c] = ''
    df = df[['data_compra', 'fabricante', 'fornecedor', 'produto', 'ean', 'codigo_interno', 'qtd_compra', 'valor_compra', 'fonte_valor']].copy()
    df['data_compra'] = pd.to_datetime(df['data_compra'], errors='coerce').dt.date.astype(str)
    df['valor_compra'] = to_numero(df['valor_compra']).fillna(0)
    df['qtd_compra'] = to_numero(df['qtd_compra']).fillna(0)
    df['fabricante'] = df['fabricante'].fillna('').astype(str).str.upper().str.strip()
    df['fornecedor'] = df['fornecedor'].fillna('').astype(str).str.upper().str.strip()
    df['produto'] = df['produto'].fillna('').astype(str)
    df['ean'] = df['ean'].map(somente_digitos)
    df['codigo_interno'] = df['codigo_interno'].map(somente_digitos)
    df['fonte_valor'] = df['fonte_valor'].fillna('').astype(str)
    df['origem_arquivo'] = origem
    antes = len(df)
    soma_antes = float(df['valor_compra'].sum()) if 'valor_compra' in df else 0
    df = df[(df['data_compra'] != 'NaT') & (df['valor_compra'] > 0)]
    if df.empty:
        raise ValueError(
            'O SQL executou, mas nenhum registro válido foi importado. '
            f'Registros recebidos: {antes}; Soma valor antes do filtro: {soma_antes:.2f}; '
            f'Colunas recebidas: {list(df_original.columns)}'
        )

    with UPDATE_LOCK:
        cache_atual = carregar_cache_compras()
        if substituir_origem and not cache_atual.empty and 'origem_arquivo' in cache_atual.columns:
            cache_atual = cache_atual[~cache_atual['origem_arquivo'].isin(['ENTRADAS_SB.sql', 'CACHE_SQL_ENTRADAS_SB'])].copy()
        if substituir_origem:
            cache_final = df.copy() if cache_atual.empty else pd.concat([cache_atual, df], ignore_index=True)
        else:
            cache_final = df.copy() if cache_atual.empty else pd.concat([cache_atual, df], ignore_index=True)
        formato = salvar_cache_compras(cache_final)

    set_config('last_import_registros', len(df))
    set_config('last_import_valor', round(float(df['valor_compra'].sum()), 2))
    set_config('last_import_datahora', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    set_config('last_cache_format', formato)
    return len(df)



def validar_config_banco(host, port, database, user, password):
    """Valida campos de conexão antes de tentar conectar ao PostgreSQL."""
    host = str(host or '').strip()
    port = str(port or '').strip()
    database = str(database or '').strip()
    user = str(user or '').strip()
    password = str(password or '').strip()
    erros = []
    if not host:
        erros.append('Informe o Host do servidor PostgreSQL.')
    if not port:
        erros.append('Informe a Porta.')
    elif not str(port).isdigit():
        erros.append('A Porta deve ser numérica, normalmente 5432.')
    if not database:
        erros.append('Informe o Banco de dados.')
    if not user:
        erros.append('Informe o Usuário.')
    if not password:
        erros.append('Informe a Senha.')
    if host and database and host.strip().lower() == database.strip().lower():
        erros.append('O Host está igual ao Banco de dados. No Host informe o IP ou endereço do servidor, por exemplo 144.22.186.71. No Banco informe sbfarma_esc_20240807.')
    if host and not any(ch in host for ch in ['.', ':']) and not host.replace('-', '').isalnum():
        erros.append('O Host informado não parece ser um endereço válido de servidor.')
    return erros


def testar_conexao_postgres(host, port, database, user, password):
    """Testa a conexão PostgreSQL retornando (ok, mensagem)."""
    erros = validar_config_banco(host, port, database, user, password)
    if erros:
        return False, '\n'.join(f'- {e}' for e in erros)
    try:
        psycopg2 = _import_postgres_driver()
        conn = psycopg2.connect(host=str(host).strip(), port=str(port).strip(), dbname=str(database).strip(), user=str(user).strip(), password=str(password), connect_timeout=15)
        conn.close()
        return True, 'Conexão realizada com sucesso.'
    except Exception as e:
        return False, str(e)

def executar_script_postgres(host, port, database, user, password):
    try:
        psycopg2 = _import_postgres_driver()
    except Exception as exc:
        raise RuntimeError('Instale um driver PostgreSQL compatível para importar direto do banco PostgreSQL.') from exc
    sql = SQL_PATH.read_text(encoding='utf-8')
    conn = psycopg2.connect(host=host, port=port, dbname=database, user=user, password=password, connect_timeout=30, options='-c statement_timeout=1800000')
    try:
        df = deduplicar_colunas(pd.read_sql_query(sql, conn))
    finally:
        conn.close()
    qtd = insert_compras(df, 'CACHE_SQL_ENTRADAS_SB', substituir_origem=True)
    registrar_atualizacao('MANUAL_SQL', 'Sucesso', qtd, f'Entradas importadas pelo script SQL. Valor compra: {money(get_config("last_import_valor", 0))}.')
    return qtd, df



def executar_script_vendas_postgres(host, port, database, user, password, data_inicio=None, data_fim=None):
    try:
        psycopg2 = _import_postgres_driver()
    except Exception as exc:
        raise RuntimeError('Instale um driver PostgreSQL compatível para importar direto do banco PostgreSQL.') from exc
    if not SQL_VENDAS_PATH.exists():
        raise RuntimeError('Arquivo VENDAS_SB.sql não encontrado na pasta do app.')
    sql = SQL_VENDAS_PATH.read_text(encoding='utf-8')

    # Corrige o período do SQL de vendas na hora da execução.
    # O arquivo VENDAS_SB.sql vinha com BETWEEN fixo; se ele ficar em outro mês,
    # a apuração Sell Out sempre fica zerada.
    if data_inicio and data_fim:
        ini = pd.to_datetime(data_inicio).strftime('%Y-%m-%d 00:00:00')
        fim = pd.to_datetime(data_fim).strftime('%Y-%m-%d 23:59:59')
        padrao = r"me\.datahora\s+BETWEEN\s+'[^']+'\s+AND\s+'[^']+'"
        sql = re.sub(padrao, f"me.datahora BETWEEN '{ini}' AND '{fim}'", sql, flags=re.IGNORECASE)

    conn = psycopg2.connect(host=host, port=port, dbname=database, user=user, password=password, connect_timeout=30, options='-c statement_timeout=1800000')
    try:
        df = deduplicar_colunas(pd.read_sql_query(sql, conn))
    finally:
        conn.close()
    qtd = salvar_vendas_periodo(df, 'CACHE_SQL_VENDAS_SB', data_inicio or date(2025,1,1), data_fim or date.today(), substituir_periodo=True)
    periodo_msg = f' Período: {data_inicio} a {data_fim}.' if data_inicio and data_fim else ''
    registrar_atualizacao('MANUAL_SQL_VENDAS', 'Sucesso', qtd, f'Vendas importadas pelo script SQL.{periodo_msg} Valor vendido: {money(get_config("last_vendas_valor", 0))}.')
    return qtd, df



def executar_script_entradas_postgres_periodo(host, port, database, user, password, data_inicio, data_fim, origem='CACHE_SQL_ENTRADAS_SB', substituir_periodo=True):
    """Executa ENTRADAS_SB.sql com período dinâmico e atualiza o cache local.

    Regra v10.13:
    - carga completa: 01/01/2025 até hoje;
    - carga incremental: somente o dia atual, substituindo o mesmo dia no cache.
    """
    try:
        psycopg2 = _import_postgres_driver()
    except Exception as exc:
        raise RuntimeError('Instale um driver PostgreSQL compatível para importar direto do banco PostgreSQL.') from exc
    if not SQL_PATH.exists():
        raise RuntimeError('Arquivo ENTRADAS_SB.sql não encontrado na pasta do app.')
    sql = SQL_PATH.read_text(encoding='utf-8')
    ini_date = pd.to_datetime(data_inicio).strftime('%Y-%m-%d')
    fim_date = pd.to_datetime(data_fim).strftime('%Y-%m-%d')
    # Substitui a janela padrão do SQL, sem depender de período fixo no arquivo.
    sql = re.sub(
        r"AND\s+nf\.datahoraemissao::date\s*>=\s*CURRENT_DATE\s*-\s*INTERVAL\s+'[^']+'",
        f"AND nf.datahoraemissao::date >= DATE '{ini_date}'",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"AND\s+nf\.datahoraemissao::date\s*<\s*CURRENT_DATE\s*\+\s*INTERVAL\s+'[^']+'",
        f"AND nf.datahoraemissao::date < DATE '{fim_date}' + INTERVAL '1 day'",
        sql,
        flags=re.IGNORECASE,
    )
    conn = psycopg2.connect(host=host, port=port, dbname=database, user=user, password=password, connect_timeout=30, options='-c statement_timeout=1800000')
    try:
        df_raw = deduplicar_colunas(pd.read_sql_query(sql, conn))
    finally:
        conn.close()

    df = preparar_entrada_sql(df_raw)
    qtd = salvar_compras_periodo(df, origem, data_inicio, data_fim, substituir_periodo=substituir_periodo)
    set_config('last_import_registros', qtd)
    set_config('last_import_valor', round(float(df.get('valor_compra', pd.Series(dtype=float)).sum()), 2))
    set_config('last_import_datahora', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    registrar_atualizacao(origem, 'Sucesso', qtd, f'Entradas atualizadas de {ini_date} a {fim_date}. Valor compra: {money(get_config("last_import_valor", 0))}.')
    return qtd, df


def salvar_compras_periodo(df, origem, data_inicio, data_fim, substituir_periodo=True):
    """Salva compras substituindo apenas o período informado."""
    if df is None:
        df = pd.DataFrame()
    df = df.copy()
    required = ['data_compra', 'valor_compra']
    for r in required:
        if r not in df.columns:
            raise ValueError(f'Coluna obrigatória não encontrada: {r}.')
    for c in ['fabricante', 'fornecedor', 'produto', 'ean', 'codigo_interno', 'qtd_compra', 'fonte_valor']:
        if c not in df.columns:
            df[c] = ''
    df = df[['data_compra', 'fabricante', 'fornecedor', 'produto', 'ean', 'codigo_interno', 'qtd_compra', 'valor_compra', 'fonte_valor']].copy()
    df['data_compra'] = pd.to_datetime(df['data_compra'], errors='coerce').dt.date.astype(str)
    df['valor_compra'] = to_numero(df['valor_compra']).fillna(0)
    df['qtd_compra'] = to_numero(df['qtd_compra']).fillna(0)
    df['fabricante'] = df['fabricante'].fillna('').astype(str).str.upper().str.strip()
    df['fornecedor'] = df['fornecedor'].fillna('').astype(str).str.upper().str.strip()
    df['produto'] = df['produto'].fillna('').astype(str)
    df['ean'] = df['ean'].map(somente_digitos)
    df['codigo_interno'] = df['codigo_interno'].map(somente_digitos)
    df['fonte_valor'] = df['fonte_valor'].fillna('').astype(str)
    df['origem_arquivo'] = origem
    df = df[(df['data_compra'] != 'NaT') & (df['valor_compra'] > 0)]
    ini = pd.to_datetime(data_inicio).date()
    fim = pd.to_datetime(data_fim).date()
    with UPDATE_LOCK:
        atual = carregar_cache_compras()
        if substituir_periodo and not atual.empty and 'data_compra' in atual.columns:
            datas = pd.to_datetime(atual['data_compra'], errors='coerce').dt.date
            atual = atual[~((datas >= ini) & (datas <= fim))].copy()
        final = df.copy() if atual.empty else pd.concat([atual, df], ignore_index=True)
        formato = salvar_cache_compras(final)
    set_config('last_cache_format', formato)
    return len(df)


def salvar_vendas_periodo(df, origem, data_inicio, data_fim, substituir_periodo=True):
    """Salva vendas substituindo apenas o período informado."""
    df_original = df.copy() if df is not None else pd.DataFrame()
    df = preparar_venda_sql(df_original)
    if 'data_venda' not in df.columns:
        raise ValueError(f'Coluna obrigatória não encontrada: data_venda. Colunas recebidas: {list(df_original.columns)}')
    for c in ['loja','usuario_orcamento','vendaid','fabricante','fornecedor','produto','ean','cod_interno','classificacao','programa_pbm','pec','coo']:
        if c not in df.columns:
            df[c] = ''
    for c in ['qtd_venda','valor_venda','valor_unitario','custo','lucro','lucro_perc','media_venda_dia','media_venda_mes']:
        if c not in df.columns:
            df[c] = 0
    cols = ['data_venda','loja','usuario_orcamento','vendaid','fabricante','fornecedor','produto','ean','cod_interno','classificacao','qtd_venda','valor_venda','valor_unitario','custo','lucro','lucro_perc','programa_pbm','pec','coo','media_venda_dia','media_venda_mes']
    df = df[cols].copy()
    df['data_venda'] = pd.to_datetime(df['data_venda'], errors='coerce').dt.date.astype(str)
    df['produto'] = df['produto'].fillna('').astype(str).str.upper().str.strip()
    df['ean'] = df['ean'].map(somente_digitos)
    df['cod_interno'] = df['cod_interno'].map(somente_digitos)
    for c in ['qtd_venda','valor_venda','valor_unitario','custo','lucro','lucro_perc','media_venda_dia','media_venda_mes']:
        df[c] = to_numero(df[c]).fillna(0)
    df['origem_arquivo'] = origem
    df = df[(df['data_venda'] != 'NaT') & ((df['valor_venda'] > 0) | (df['qtd_venda'] > 0))]
    ini = pd.to_datetime(data_inicio).date()
    fim = pd.to_datetime(data_fim).date()
    with UPDATE_LOCK:
        atual = carregar_cache_vendas()
        if substituir_periodo and not atual.empty and 'data_venda' in atual.columns:
            datas = pd.to_datetime(atual['data_venda'], errors='coerce').dt.date
            atual = atual[~((datas >= ini) & (datas <= fim))].copy()
        final = df.copy() if atual.empty else pd.concat([atual, df], ignore_index=True)
        formato = salvar_cache_vendas(final)
    set_config('last_vendas_registros', len(df))
    set_config('last_vendas_valor', round(float(df['valor_venda'].sum()) if not df.empty else 0, 2))
    set_config('last_vendas_datahora', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    set_config('last_vendas_cache_format', formato)
    return len(df)

def atualizar_cache_sql(tipo='MANUAL_SQL'):
    host = get_config('auto_host')
    port = get_config('auto_port', '5432')
    database = get_config('auto_database')
    user = get_config('auto_user')
    password = get_config('auto_password')
    if not all([host, port, database, user, password]):
        raise RuntimeError('Configure os dados de conexão do banco antes de atualizar automaticamente.')

    try:
        psycopg2 = _import_postgres_driver()
    except Exception as exc:
        raise RuntimeError('Instale um driver PostgreSQL compatível para conectar no PostgreSQL.') from exc

    qtd, df = executar_script_entradas_postgres_periodo(host, port, database, user, password, FULL_SYNC_START_DATE, date.today(), origem='CACHE_SQL_ENTRADAS_SB', substituir_periodo=True)
    set_config('last_auto_update_date', str(date.today()))
    set_config('last_auto_update_datetime', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    registrar_atualizacao(tipo, 'Sucesso', qtd, f'Cache de entradas atualizado de 01/01/2025 até hoje. Registros: {qtd}.')
    return qtd, df



def atualizar_vendas_sql_por_periodo(data_inicio, data_fim, tipo='MANUAL_SQL_VENDAS'):
    """Atualiza somente o cache de vendas usando o período informado.

    Esta função centraliza a regra que corrigiu o Sell Out: o SQL de vendas
    precisa ser executado exatamente na vigência da negociação/apuração.
    """
    host = get_config('auto_host')
    port = get_config('auto_port', '5432')
    database = get_config('auto_database')
    user = get_config('auto_user')
    password = get_config('auto_password')
    if not all([host, port, database, user, password]):
        raise RuntimeError('Configure os dados de conexão do banco em Importar dados antes de atualizar vendas automaticamente.')
    qtd, df = executar_script_vendas_postgres(host, port, database, user, password, data_inicio, data_fim)
    registrar_atualizacao(tipo, 'Sucesso', qtd, f'Cache de vendas atualizado pela vigência {data_inicio} a {data_fim}. Valor vendido: {money(get_config("last_vendas_valor", 0))}.')
    return qtd, df


def atualizar_bases_periodo(data_inicio, data_fim, tipo='MANUAL_PERIODO'):
    """Atualiza entradas e vendas para o mesmo período, substituindo somente esse intervalo no cache."""
    host = get_config('auto_host')
    port = get_config('auto_port', '5432')
    database = get_config('auto_database')
    user = get_config('auto_user')
    password = get_config('auto_password')
    if not all([host, port, database, user, password]):
        raise RuntimeError('Configure os dados de conexão do banco em Importar dados antes de atualizar automaticamente.')
    qtd_e, df_e = executar_script_entradas_postgres_periodo(host, port, database, user, password, data_inicio, data_fim, origem='CACHE_SQL_ENTRADAS_SB', substituir_periodo=True)
    qtd_v, df_v = atualizar_vendas_sql_por_periodo(data_inicio, data_fim, tipo=f'{tipo}_VENDAS')
    registrar_atualizacao(tipo, 'Sucesso', int(qtd_e) + int(qtd_v), f'Entradas e vendas atualizadas de {data_inicio} a {data_fim}. Entradas: {qtd_e}; Vendas: {qtd_v}.')
    return qtd_e, qtd_v, df_e, df_v


def atualizar_bases_completa_ate_hoje(tipo='AUTO_FULL_04H'):
    """Carga oficial diária: entradas e vendas de 01/01/2025 até hoje."""
    ini = FULL_SYNC_START_DATE
    fim = date.today()
    qtd_e, qtd_v, df_e, df_v = atualizar_bases_periodo(ini, fim, tipo=tipo)
    set_config('last_full_update_date', str(date.today()))
    set_config('last_full_update_datetime', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    set_config('last_hourly_update_key', datetime.now().strftime('%Y-%m-%d %H'))
    recalcular_cache_apuracao_silencioso()
    return qtd_e, qtd_v, df_e, df_v


def atualizar_bases_dia_atual(tipo='AUTO_HOURLY_TODAY'):
    """Carga incremental do dia: substitui apenas hoje no cache, preservando histórico desde 01/01/2025."""
    hoje = date.today()
    qtd_e, qtd_v, df_e, df_v = atualizar_bases_periodo(hoje, hoje, tipo=tipo)
    set_config('last_hourly_update_key', datetime.now().strftime('%Y-%m-%d %H'))
    set_config('last_hourly_update_datetime', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    recalcular_cache_apuracao_silencioso()
    return qtd_e, qtd_v, df_e, df_v

def auto_update_if_needed(force_after_schedule=True):
    """Rotina automática v10.13.

    - Às 04:00: carga completa de entradas e vendas de 01/01/2025 até hoje.
    - Durante o dia: atualização incremental de hora em hora somente do dia atual.
    """
    if get_config('auto_enabled', '0') != '1':
        return
    agora = datetime.now()
    h = int(get_config('auto_hour', str(SCHEDULE_HOUR)) or SCHEDULE_HOUR)
    m = int(get_config('auto_minute', str(SCHEDULE_MINUTE)) or SCHEDULE_MINUTE)

    # Carga completa diária a partir do horário configurado.
    if force_after_schedule and (agora.hour, agora.minute) >= (h, m):
        if get_config('last_full_update_date') != str(date.today()):
            try:
                atualizar_bases_completa_ate_hoje(tipo='AUTO_FULL_04H')
            except Exception as e:
                registrar_atualizacao('AUTO_FULL_04H', 'Erro', 0, str(e))
            return

    # Incremental do dia, uma vez por hora, enquanto o app estiver aberto.
    chave_hora = agora.strftime('%Y-%m-%d %H')
    if get_config('last_hourly_update_key') != chave_hora and agora.minute >= int(get_config('hourly_minute', str(HOURLY_SYNC_MINUTE)) or HOURLY_SYNC_MINUTE):
        try:
            atualizar_bases_dia_atual(tipo='AUTO_HOURLY_TODAY')
        except Exception as e:
            registrar_atualizacao('AUTO_HOURLY_TODAY', 'Erro', 0, str(e))


def scheduler_04h_loop():
    """Rotina simples em segundo plano. Funciona enquanto o app Streamlit estiver aberto/rodando no servidor."""
    while True:
        try:
            auto_update_if_needed(force_after_schedule=True)
        except Exception:
            pass
        time_module.sleep(60)


def iniciar_scheduler():
    # No Streamlit Cloud, background threads não são confiáveis para rotinas de produção.
    # Use o botão manual do app ou um agendador externo (cron/GitHub Actions/servidor).
    if CLOUD_MODE and not _truthy(APP_SECRETS.get('allow_scheduler')):
        return
    if get_config('auto_enabled', '0') != '1':
        return
    if 'scheduler_started' not in st.session_state:
        th = threading.Thread(target=scheduler_04h_loop, daemon=True)
        th.start()
        st.session_state['scheduler_started'] = True



def selecionar_faixa_progressiva(faixas, valor_base, percentual_padrao=0.0, bonificacao_padrao=0.0, verba_padrao=0.0, meta_padrao=0.0):
    """Seleciona a MAIOR faixa atingida de forma não cumulativa.

    Regra de negócio:
    - Faixas de meta são progressivas/substitutivas, não acumulativas.
    - Exemplo: 320.000 => 10% e 370.000 => 12%.
      Se a compra for 380.000, aplica somente 12% sobre o total apurado.
    - O sistema deve gerar apenas 1 crédito/apuração para a negociação.
    """
    resultado = {
        'meta': float(meta_padrao or 0),
        'percentual': float(percentual_padrao or 0),
        'bonificacao': float(bonificacao_padrao or 0),
        'verba': float(verba_padrao or 0),
        'faixa_label': '',
        'situacao': '',
        'proxima_meta': 0.0,
        'falta_proxima': 0.0,
        'regra': 'Sem faixa progressiva',
    }
    if faixas is None or faixas.empty:
        return resultado

    fx = faixas.copy()
    for col in ['meta_valor', 'percentual', 'bonificacao', 'verba_comercial']:
        if col not in fx.columns:
            fx[col] = 0
        fx[col] = pd.to_numeric(fx[col], errors='coerce').fillna(0)
    if 'faixa' not in fx.columns:
        fx['faixa'] = range(1, len(fx) + 1)
    fx = fx[fx['meta_valor'] > 0].sort_values(['meta_valor', 'faixa']).reset_index(drop=True)
    if fx.empty:
        return resultado

    valor_base = float(valor_base or 0)
    atingidas = fx[fx['meta_valor'] <= valor_base]
    if not atingidas.empty:
        f = atingidas.iloc[-1]
        resultado['meta'] = float(f.get('meta_valor', resultado['meta']) or 0)
        resultado['percentual'] = float(f.get('percentual', resultado['percentual']) or 0)
        resultado['bonificacao'] = float(f.get('bonificacao', resultado['bonificacao']) or 0)
        resultado['verba'] = float(f.get('verba_comercial', resultado['verba']) or 0)
        resultado['faixa_label'] = f"Meta {int(f.get('faixa', len(atingidas)))}"
        resultado['situacao'] = f"{resultado['faixa_label']} atingida - {resultado['percentual']:.2f}% aplicado sobre o total"
        resultado['regra'] = 'Faixa progressiva não cumulativa'
    else:
        primeira = fx.iloc[0]
        resultado['meta'] = float(primeira.get('meta_valor', resultado['meta']) or 0)
        resultado['situacao'] = 'Não atingida'
        resultado['regra'] = 'Faixa progressiva não cumulativa'

    prox = fx[fx['meta_valor'] > valor_base]
    if not prox.empty:
        resultado['proxima_meta'] = float(prox.iloc[0]['meta_valor'])
        resultado['falta_proxima'] = max(resultado['proxima_meta'] - valor_base, 0)
    else:
        resultado['proxima_meta'] = float(fx.iloc[-1]['meta_valor'])
        resultado['falta_proxima'] = 0.0
    return resultado

def calcular_apuracao(compras, negociacoes, data_ini, data_fim):
    if compras.empty or negociacoes.empty:
        return pd.DataFrame()
    c = compras.copy()
    n = negociacoes.copy()
    c['data_compra'] = pd.to_datetime(c['data_compra']).dt.date
    n['data_inicio'] = pd.to_datetime(n['data_inicio']).dt.date
    n['data_fim'] = pd.to_datetime(n['data_fim']).dt.date
    # A apuração NÃO usa mais período fixo da tela.
    # Cada negociação é apurada automaticamente pela sua própria vigência
    # (data_inicio até data_fim). Os parâmetros data_ini/data_fim ficam
    # apenas para compatibilidade com telas antigas/exportações.
    n = n[n['status'].eq('Ativo')]
    rows = []
    for _, neg in n.iterrows():
        campo = 'fabricante' if neg['tipo'] == 'Fabricante' else 'fornecedor'
        compras_match = c[
            c[campo].astype(str).str.upper().str.strip().eq(str(neg['nome']).upper().strip()) &
            (c['data_compra'] >= neg['data_inicio']) &
            (c['data_compra'] <= neg['data_fim'])
        ]
        valor_base = compras_match['valor_compra'].sum()
        tipo_neg = neg.get('tipo_negociacao', 'Desconto percentual') or 'Desconto percentual'
        meta = float(neg.get('meta_compra', 0) or 0)
        percentual_aplicado = float(neg.get('percentual', 0) or 0)
        bonificacao_aplicada = float(neg.get('bonificacao', 0) or 0)
        verba_aplicada = float(neg.get('verba_comercial', 0) or 0)
        faixa_atingida = ''
        proxima_meta = 0.0
        falta_proxima = 0.0
        regra_faixa = ''
        if tipo_neg in ['Faixa de meta', 'Híbrida']:
            faixas = carregar_faixas(int(neg.get('id')))
            faixa = selecionar_faixa_progressiva(
                faixas, valor_base,
                percentual_padrao=percentual_aplicado,
                bonificacao_padrao=bonificacao_aplicada,
                verba_padrao=verba_aplicada,
                meta_padrao=meta,
            )
            meta = faixa['meta']
            percentual_aplicado = faixa['percentual']
            bonificacao_aplicada = faixa['bonificacao']
            verba_aplicada = faixa['verba']
            faixa_atingida = faixa['situacao'] or faixa['faixa_label']
            proxima_meta = faixa['proxima_meta']
            falta_proxima = faixa['falta_proxima']
            regra_faixa = faixa['regra']
        # Regra: mesmo sem meta cadastrada, o investimento deve ser apurado
        # pelo percentual do acordo sobre a compra realizada. Meta só controla
        # acompanhamento/atingimento; não pode zerar o valor do investimento.
        if meta > 0:
            atingimento = (valor_base / meta * 100)
            situacao_meta = faixa_atingida or ('Meta base' if valor_base >= meta else 'Não atingida')
        else:
            atingimento = 100 if valor_base > 0 and percentual_aplicado > 0 else 0
            situacao_meta = 'Sem meta - percentual do acordo aplicado' if percentual_aplicado > 0 else 'Sem meta'

        investimento = valor_base * (percentual_aplicado / 100) + verba_aplicada
        fontes = ', '.join(sorted([x for x in compras_match.get('fonte_valor', pd.Series(dtype=str)).dropna().astype(str).unique() if x])[:3])
        rows.append({
            'Código Negociação': neg.get('codigo_negociacao', ''),
            'Código Curto': neg.get('codigo_curto', ''),
            'Tipo': neg['tipo'],
            'Tipo Negociação': tipo_neg,
            'Fabricante/Distribuidor': neg['nome'],
            '% Investimento': percentual_aplicado,
            'Bonificação': bonificacao_aplicada,
            'Verba Comercial': verba_aplicada,
            'Meta de Compra': meta,
            'Faixa Atingida': situacao_meta,
            'Regra Faixa': regra_faixa,
            'Próxima Meta': proxima_meta,
            'Falta Próxima Meta': falta_proxima,
            'Compra Realizada': valor_base,
            '% Atingimento Meta': atingimento,
            'Validade Início': neg['data_inicio'],
            'Validade Fim': neg['data_fim'],
            'Valor Compra Base': valor_base,
            'Valor Investimento a Receber': investimento,
            'Fonte do Valor': fontes or 'valor_compra',
            'Qtd. Registros Compra': len(compras_match),
            'Observação': neg.get('observacao', '')
        })
    return pd.DataFrame(rows).sort_values('Valor Investimento a Receber', ascending=False)




def _pdf_formatar_numero(valor, casas=2):
    try:
        return f"{float(valor):,.{casas}f}".replace(',', 'X').replace('.', ',').replace('X', '.')
    except Exception:
        return "0,00"


def _pdf_money(valor):
    return money(valor)


def _pdf_pct(valor):
    return pct(valor)


def _pdf_valor_linha(row, col, padrao=0):
    try:
        if hasattr(row, 'get'):
            return row.get(col, padrao)
    except Exception:
        pass
    return padrao


def _pdf_filtrar_produtos_por_modelo(df, tipo_relatorio):
    """Seleciona os produtos que devem aparecer em cada modelo de relatório."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    tipo = str(tipo_relatorio or '').lower()

    if 'sell in' in tipo and 'compras' in tipo:
        cols_base = ['Produto', 'Tipo Investimento', 'Meta Sell In', 'Compra Realizada', 'Valor Compra', '% Atingido SI', 'Investimento Sell In', 'Observação Produto']
        return out[[c for c in cols_base if c in out.columns]]

    if 'sell out' in tipo and 'vendas' in tipo:
        cols_base = ['Produto', 'Tipo Investimento', 'Meta Sell Out', 'Venda Realizada', 'Valor Vendido', '% Atingido SO', 'R$ Un SO', '% SO', 'Investimento Sell Out', 'Observação Produto']
        return out[[c for c in cols_base if c in out.columns]]

    if 'abaixo' in tipo:
        if 'Meta Sell Out' in out.columns and 'Venda Realizada' in out.columns:
            meta = pd.to_numeric(out['Meta Sell Out'], errors='coerce').fillna(0)
            venda = pd.to_numeric(out['Venda Realizada'], errors='coerce').fillna(0)
            out = out[(meta > 0) & (venda < meta)].copy()
        elif 'Meta Sell In' in out.columns and 'Compra Realizada' in out.columns:
            meta = pd.to_numeric(out['Meta Sell In'], errors='coerce').fillna(0)
            compra = pd.to_numeric(out['Compra Realizada'], errors='coerce').fillna(0)
            out = out[(meta > 0) & (compra < meta)].copy()
        cols_base = ['Produto', 'Tipo Investimento', 'Meta Sell Out', 'Venda Realizada', 'Dif SO', 'R$ Un SO', 'Investimento Sell Out', 'Meta Sell In', 'Compra Realizada', 'Dif SI', 'Investimento Sell In']
        return out[[c for c in cols_base if c in out.columns]]

    # Automático/resumo executivo: mostra os campos principais, misturando Sell In e Sell Out.
    cols_base = ['Produto', 'Tipo Investimento', 'Meta Sell In', 'Compra Realizada', 'Meta Sell Out', 'Venda Realizada', 'Valor Vendido', 'Investimento Sell In', 'Investimento Sell Out', 'Investimento']
    return out[[c for c in cols_base if c in out.columns]]


def _pdf_preparar_tabela(df):
    """Aplica nomes amigáveis e formatação antes de enviar para o PDF."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()

    moeda_cols = ['Valor Compra', 'Valor Vendido', 'R$ Un SI', 'R$ Un SO', 'Investimento Sell In', 'Investimento Sell Out', 'Investimento', 'R$ Unitário']
    perc_cols = ['% Atingido SI', '% Atingido SO', '% SI', '% SO', '% Atingido', '% Acordo Produto']
    qtd_cols = ['Meta Sell In', 'Compra Realizada', 'Meta Sell Out', 'Venda Realizada', 'Dif SI', 'Dif SO']

    for c in moeda_cols:
        if c in out.columns:
            out[c] = out[c].map(_pdf_money)
    for c in perc_cols:
        if c in out.columns:
            out[c] = out[c].map(_pdf_pct)
    for c in qtd_cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors='coerce').fillna(0).map(lambda x: _pdf_formatar_numero(x, 0))

    rename = {
        'Tipo Investimento': 'Tipo',
        'Meta Sell In': 'Meta compra',
        'Compra Realizada': 'Compra realizada',
        'Valor Compra': 'Valor compra',
        '% Atingido SI': '% compra',
        'Dif SI': 'Falta compra',
        'R$ Un SI': 'R$/un compra',
        '% SI': '% compra',
        'Investimento Sell In': 'Invest. compra',
        'Meta Sell Out': 'Meta venda',
        'Venda Realizada': 'Venda realizada',
        'Valor Vendido': 'Valor vendido',
        '% Atingido SO': '% venda',
        'Dif SO': 'Falta venda',
        'R$ Un SO': 'R$/un venda',
        '% SO': '% venda',
        'Investimento Sell Out': 'Invest. venda',
        'Investimento': 'Invest. total',
        'Observação Produto': 'Observação',
    }
    return out.rename(columns={k:v for k,v in rename.items() if k in out.columns})



def _pdf_preparar_resumo_negociacao(ap_resumo):
    """Prepara uma tabela de relatório para negociações sem produto.

    Atende ações de desconto percentual, faixa de meta, bonificação,
    verba comercial/sell-out geral e negociações híbridas, mesmo quando
    não houver SKUs vinculados na negociação.
    """
    if ap_resumo is None or ap_resumo.empty:
        return pd.DataFrame()
    cols = [
        'Código Curto', 'Fabricante/Distribuidor', 'Tipo Negociação',
        'Meta de Compra', 'Compra Realizada', '% Atingimento Meta',
        '% Investimento', 'Faixa Atingida', 'Próxima Meta', 'Falta Próxima Meta',
        'Bonificação', 'Verba Comercial', 'Valor Investimento a Receber',
        'Validade Início', 'Validade Fim', 'Observação'
    ]
    out = ap_resumo[[c for c in cols if c in ap_resumo.columns]].copy()
    moeda_cols = ['Meta de Compra', 'Compra Realizada', 'Próxima Meta', 'Falta Próxima Meta', 'Verba Comercial', 'Valor Investimento a Receber']
    perc_cols = ['% Atingimento Meta', '% Investimento']
    qtd_cols = ['Bonificação']
    for c in moeda_cols:
        if c in out.columns:
            out[c] = out[c].map(_pdf_money)
    for c in perc_cols:
        if c in out.columns:
            out[c] = out[c].map(_pdf_pct)
    for c in qtd_cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors='coerce').fillna(0).map(lambda x: _pdf_formatar_numero(x, 0))
    rename = {
        'Código Curto': 'Código',
        'Fabricante/Distribuidor': 'Fabricante/Distribuidor',
        'Tipo Negociação': 'Tipo',
        'Meta de Compra': 'Meta negociada',
        'Compra Realizada': 'Compra realizada',
        '% Atingimento Meta': '% atingido',
        '% Investimento': '% acordo',
        'Faixa Atingida': 'Faixa',
        'Próxima Meta': 'Próxima meta',
        'Falta Próxima Meta': 'Falta próxima meta',
        'Bonificação': 'Bonificação',
        'Verba Comercial': 'Verba comercial',
        'Valor Investimento a Receber': 'Investimento a receber',
        'Validade Início': 'Início',
        'Validade Fim': 'Fim',
        'Observação': 'Observação',
    }
    return out.rename(columns={k:v for k,v in rename.items() if k in out.columns})

def gerar_pdf_relatorio_negociacao(neg, ap_resumo, ap_produtos, tipo_relatorio, data_ini, data_fim):
    """Gera relatório PDF da negociação em memória.

    A função é autocontida para evitar erro de função ausente na tela Relatórios PDF.
    Usa ReportLab, que já está no requirements.txt.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image
    except Exception as exc:
        raise RuntimeError('Dependência reportlab ausente. Execute: pip install reportlab') from exc

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=1.0*cm,
        leftMargin=1.0*cm,
        topMargin=0.9*cm,
        bottomMargin=0.9*cm,
        title='Relatório da Negociação - SB Farma'
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='SBTitle', parent=styles['Title'], fontName='Helvetica-Bold', fontSize=18, leading=22, textColor=colors.HexColor('#123A56'), alignment=TA_LEFT))
    styles.add(ParagraphStyle(name='SBSubTitle', parent=styles['Normal'], fontName='Helvetica', fontSize=9, leading=12, textColor=colors.HexColor('#475569')))
    styles.add(ParagraphStyle(name='SBSection', parent=styles['Heading2'], fontName='Helvetica-Bold', fontSize=12, leading=15, textColor=colors.HexColor('#123A56'), spaceBefore=8, spaceAfter=6))
    styles.add(ParagraphStyle(name='SBCell', parent=styles['Normal'], fontName='Helvetica', fontSize=7, leading=8.5, wordWrap='CJK'))
    styles.add(ParagraphStyle(name='SBCellBold', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=7, leading=8.5, wordWrap='CJK'))

    story = []
    codigo = str(_pdf_valor_linha(neg, 'codigo_curto') or _pdf_valor_linha(neg, 'codigo_negociacao') or _pdf_valor_linha(neg, 'id') or '')
    nome = str(_pdf_valor_linha(neg, 'nome') or _pdf_valor_linha(neg, 'Fabricante/Distribuidor') or '')
    tipo_investimento = str(_pdf_valor_linha(neg, 'tipo_investimento') or _pdf_valor_linha(neg, 'Tipo Investimento') or '')
    tipo_negociacao = str(_pdf_valor_linha(neg, 'tipo_negociacao') or _pdf_valor_linha(neg, 'Tipo Negociação') or '')
    status = str(_pdf_valor_linha(neg, 'status') or 'Ativo')
    usuario = usuario_atual()

    logo_added = False
    try:
        if LOGO_BRANCO.exists():
            # Logo branca pode sumir em PDF branco; usa pequena no cabeçalho apenas se disponível.
            img = Image(str(LOGO_BRANCO), width=4.2*cm, height=1.0*cm)
            story.append(img)
            logo_added = True
    except Exception:
        logo_added = False
    if logo_added:
        story.append(Spacer(1, 0.15*cm))

    titulo_modelo = str(tipo_relatorio or 'Resumo executivo').replace('Automático pela ação', 'Resumo executivo')
    story.append(Paragraph('Relatório da Negociação', styles['SBTitle']))
    story.append(Paragraph(f'SB Farma - {titulo_modelo}', styles['SBSubTitle']))
    story.append(Spacer(1, 0.25*cm))

    resumo_neg = [
        ['Código', codigo, 'Fabricante/Distribuidor', nome],
        ['Tipo de negociação', tipo_negociacao, 'Tipo de investimento', tipo_investimento],
        ['Vigência', f"{pd.to_datetime(data_ini).strftime('%d/%m/%Y')} até {pd.to_datetime(data_fim).strftime('%d/%m/%Y')}", 'Status', status],
        ['Emitido por', usuario, 'Emissão', datetime.now().strftime('%d/%m/%Y %H:%M')],
    ]
    t_info = Table(resumo_neg, colWidths=[3.1*cm, 8.2*cm, 4.0*cm, 12.2*cm])
    t_info.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F8FAFC')),
        ('TEXTCOLOR', (0,0), (-1,-1), colors.HexColor('#0F172A')),
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
        ('FONTNAME', (2,0), (2,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('GRID', (0,0), (-1,-1), 0.25, colors.HexColor('#CBD5E1')),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [colors.HexColor('#F8FAFC'), colors.white]),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(t_info)
    story.append(Spacer(1, 0.35*cm))

    ap_produtos = ap_produtos.copy() if ap_produtos is not None else pd.DataFrame()
    ap_resumo = ap_resumo.copy() if ap_resumo is not None else pd.DataFrame()
    total_si = float(pd.to_numeric(ap_produtos.get('Investimento Sell In', pd.Series(dtype=float)), errors='coerce').fillna(0).sum()) if not ap_produtos.empty else 0.0
    total_so = float(pd.to_numeric(ap_produtos.get('Investimento Sell Out', pd.Series(dtype=float)), errors='coerce').fillna(0).sum()) if not ap_produtos.empty else 0.0
    total = float(pd.to_numeric(ap_produtos.get('Investimento', pd.Series(dtype=float)), errors='coerce').fillna(0).sum()) if not ap_produtos.empty else 0.0
    # Para negociações sem produto, o investimento fica na apuração geral.
    if total == 0 and not ap_resumo.empty and 'Valor Investimento a Receber' in ap_resumo.columns:
        total = float(pd.to_numeric(ap_resumo['Valor Investimento a Receber'], errors='coerce').fillna(0).sum())
        # Quando não existe detalhe por SKU, classifica o valor geral conforme o tipo informado no cabeçalho.
        ti = str(tipo_investimento or '').lower()
        tn = str(tipo_negociacao or '').lower()
        if 'sell out' in ti or 'sell-out' in tn or 'verba comercial' in tn:
            total_so = total
        else:
            total_si = total
    compra_qtd = float(pd.to_numeric(ap_produtos.get('Compra Realizada', pd.Series(dtype=float)), errors='coerce').fillna(0).sum()) if not ap_produtos.empty else 0.0
    if compra_qtd == 0 and not ap_resumo.empty and 'Compra Realizada' in ap_resumo.columns:
        compra_qtd = float(pd.to_numeric(ap_resumo['Compra Realizada'], errors='coerce').fillna(0).sum())
    venda_qtd = float(pd.to_numeric(ap_produtos.get('Venda Realizada', pd.Series(dtype=float)), errors='coerce').fillna(0).sum()) if not ap_produtos.empty else 0.0
    venda_valor = float(pd.to_numeric(ap_produtos.get('Valor Vendido', pd.Series(dtype=float)), errors='coerce').fillna(0).sum()) if not ap_produtos.empty else 0.0

    story.append(Paragraph('Resumo executivo', styles['SBSection']))
    kpis = [
        ['Produtos', _pdf_formatar_numero(len(ap_produtos), 0), 'Compra realizada', _pdf_formatar_numero(compra_qtd, 0)],
        ['Venda realizada', _pdf_formatar_numero(venda_qtd, 0), 'Valor vendido', _pdf_money(venda_valor)],
        ['Investimento Sell In', _pdf_money(total_si), 'Investimento Sell Out', _pdf_money(total_so)],
        ['Investimento total', _pdf_money(total), 'Modelo', titulo_modelo],
    ]
    t_kpis = Table(kpis, colWidths=[4.0*cm, 5.0*cm, 4.2*cm, 6.0*cm])
    t_kpis.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#EAF2F8')),
        ('TEXTCOLOR', (0,0), (-1,-1), colors.HexColor('#0F172A')),
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
        ('FONTNAME', (2,0), (2,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('GRID', (0,0), (-1,-1), 0.25, colors.HexColor('#AFC4D4')),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(t_kpis)
    story.append(Spacer(1, 0.35*cm))

    tabela = _pdf_filtrar_produtos_por_modelo(ap_produtos, tipo_relatorio)
    tabela = _pdf_preparar_tabela(tabela)

    if tabela.empty:
        # Negociações de desconto, faixa, bonificação ou verba geral podem não ter produtos.
        # Nestes casos o relatório deve sair com os principais campos da ação.
        tabela = _pdf_preparar_resumo_negociacao(ap_resumo)
        if tabela.empty:
            story.append(Paragraph('Não há dados apurados para este modelo de relatório.', styles['SBSubTitle']))
        else:
            story.append(Paragraph('Resumo da ação / negociação', styles['SBSection']))
            if len(tabela.columns) > 10:
                tabela = tabela.iloc[:, :10]
            dados = [[Paragraph(str(c), styles['SBCellBold']) for c in tabela.columns]]
            for _, row in tabela.head(250).iterrows():
                dados.append([Paragraph(str(row.get(c, '')), styles['SBCell']) for c in tabela.columns])
            ncols = len(tabela.columns)
            page_width = landscape(A4)[0] - 2.0*cm
            col_widths = [page_width / max(ncols, 1)] * ncols
            t_geral = Table(dados, colWidths=col_widths, repeatRows=1)
            t_geral.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#123A56')),
                ('TEXTCOLOR', (0,0), (-1,0), colors.white),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE', (0,0), (-1,-1), 7),
                ('GRID', (0,0), (-1,-1), 0.25, colors.HexColor('#CBD5E1')),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#F8FAFC')]),
                ('VALIGN', (0,0), (-1,-1), 'TOP'),
                ('LEFTPADDING', (0,0), (-1,-1), 4),
                ('RIGHTPADDING', (0,0), (-1,-1), 4),
                ('TOPPADDING', (0,0), (-1,-1), 4),
                ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ]))
            story.append(t_geral)
    else:
        story.append(Paragraph('Produtos da negociação', styles['SBSection']))
        # Limita colunas para caber em paisagem. Se houver muitas, prioriza principais.
        if len(tabela.columns) > 9:
            tabela = tabela.iloc[:, :9]
        dados = [[Paragraph(str(c), styles['SBCellBold']) for c in tabela.columns]]
        for _, row in tabela.head(250).iterrows():
            dados.append([Paragraph(str(row.get(c, '')), styles['SBCell']) for c in tabela.columns])

        # Larguras dinâmicas: produto maior, demais compactas.
        ncols = len(tabela.columns)
        page_width = landscape(A4)[0] - 2.0*cm
        produto_idx = list(tabela.columns).index('Produto') if 'Produto' in tabela.columns else -1
        fixed = []
        remaining = page_width
        for i, c in enumerate(tabela.columns):
            if i == produto_idx:
                fixed.append(7.0*cm)
                remaining -= 7.0*cm
            else:
                fixed.append(None)
        other_count = max(ncols - (1 if produto_idx >= 0 else 0), 1)
        other_w = max(2.0*cm, remaining / other_count)
        col_widths = [w if w is not None else other_w for w in fixed]

        t_prod = Table(dados, colWidths=col_widths, repeatRows=1)
        t_prod.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#123A56')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,-1), 7),
            ('GRID', (0,0), (-1,-1), 0.25, colors.HexColor('#CBD5E1')),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#F8FAFC')]),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('LEFTPADDING', (0,0), (-1,-1), 4),
            ('RIGHTPADDING', (0,0), (-1,-1), 4),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ]))
        story.append(t_prod)
        if len(tabela) > 250:
            story.append(Spacer(1, 0.2*cm))
            story.append(Paragraph(f'O PDF exibiu os primeiros 250 produtos de {len(tabela)}. Use Excel para a base completa.', styles['SBSubTitle']))

    def _page_footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(colors.HexColor('#64748B'))
        canvas.drawString(1.0*cm, 0.45*cm, 'SB Farma - Relatório de Negociação')
        canvas.drawRightString(landscape(A4)[0] - 1.0*cm, 0.45*cm, f'Página {doc_obj.page}')
        canvas.restoreState()

    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    return buffer.getvalue()



# =====================================================================
# V7.1 - Painéis executivos e módulos de gestão
# =====================================================================
def _num_col(df, col):
    if df is None or df.empty or col not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[col], errors='coerce').fillna(0)


def _periodo_global_negociacoes(negociacoes):
    if negociacoes is None or negociacoes.empty:
        return date(2000, 1, 1), date(2099, 12, 31)
    ini = pd.to_datetime(negociacoes.get('data_inicio'), errors='coerce').dropna()
    fim = pd.to_datetime(negociacoes.get('data_fim'), errors='coerce').dropna()
    data_ini = ini.min().date() if not ini.empty else date(2000, 1, 1)
    data_fim = fim.max().date() if not fim.empty else date(2099, 12, 31)
    return data_ini, data_fim


# =====================================================================
# V12.1 - Performance estrutural
# As telas executivas/extrato não recalculam mais compras e vendas ao abrir.
# Elas consomem a última apuração resumida gravada pela tela Apuração.
# =====================================================================
def _tabela_existe(nome_tabela):
    try:
        with connect() as con:
            row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (nome_tabela,)).fetchone()
            return row is not None
    except Exception:
        return False


def salvar_apuracao_resumida(ap, ap_prod):
    """Persiste a última apuração em tabelas leves para navegação rápida."""
    try:
        with connect() as con:
            if ap is not None and not ap.empty:
                ap_cache = ap.copy()
                ap_cache['cache_gerado_em'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                ap_cache.to_sql('cache_apuracao_resumo', con, if_exists='replace', index=False)
            elif not _tabela_existe('cache_apuracao_resumo'):
                con.execute('CREATE TABLE IF NOT EXISTS cache_apuracao_resumo (cache_gerado_em TEXT)')

            if ap_prod is not None and not ap_prod.empty:
                ap_prod_cache = ap_prod.copy()
                ap_prod_cache['cache_gerado_em'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                ap_prod_cache.to_sql('cache_apuracao_produtos', con, if_exists='replace', index=False)
            elif not _tabela_existe('cache_apuracao_produtos'):
                con.execute('CREATE TABLE IF NOT EXISTS cache_apuracao_produtos (cache_gerado_em TEXT)')

            con.execute('CREATE TABLE IF NOT EXISTS cache_info (chave TEXT PRIMARY KEY, valor TEXT)')
            con.execute('INSERT OR REPLACE INTO cache_info (chave, valor) VALUES (?, ?)', ('apuracao_resumida_gerada_em', datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            con.commit()
        # Não limpar todos os caches do Streamlit, pois isso torna a troca de telas lenta.
        # Invalidamos apenas as leituras que dependem do resumo de apuração.
        try:
            carregar_apuracao_resumida.clear()
        except Exception:
            pass
        try:
            carregar_visao_executiva.clear()
        except Exception:
            pass
        return True
    except Exception:
        return False


@st.cache_data(ttl=3600, show_spinner=False)
def carregar_apuracao_resumida():
    """Lê somente as tabelas resumidas. Não carrega compras/vendas brutas."""
    try:
        with connect() as con:
            ap = pd.read_sql_query('SELECT * FROM cache_apuracao_resumo', con) if _tabela_existe('cache_apuracao_resumo') else pd.DataFrame()
            ap_prod = pd.read_sql_query('SELECT * FROM cache_apuracao_produtos', con) if _tabela_existe('cache_apuracao_produtos') else pd.DataFrame()
        return ap, ap_prod
    except Exception:
        return pd.DataFrame(), pd.DataFrame()


def obter_info_cache_apuracao():
    try:
        with connect() as con:
            if not _tabela_existe('cache_info'):
                return ''
            row = con.execute("SELECT valor FROM cache_info WHERE chave='apuracao_resumida_gerada_em'").fetchone()
            return row[0] if row else ''
    except Exception:
        return ''


@st.cache_data(ttl=600, show_spinner=False)
def carregar_visao_executiva():
    """Visão rápida para Painel/Extrato.

    Importante: não recalcula apuração e não carrega vendas/compras brutas.
    Para atualizar os números, abra Apuração ou use os botões de atualização.
    """
    negociacoes = load_table('negociacoes')
    ap, ap_prod = carregar_apuracao_resumida()
    return pd.DataFrame(), pd.DataFrame(), negociacoes, ap, ap_prod


def avisar_resumo_desatualizado():
    gerado = obter_info_cache_apuracao()
    if gerado:
        st.caption(f'Resumo executivo carregado do cache da última apuração: {gerado}.')
    else:
        st.warning('Resumo executivo ainda não foi gerado. Abra a tela Apuração uma vez para gerar o resumo rápido usado pelo Painel e pelo Extrato.')


def recalcular_cache_apuracao_silencioso():
    """Recalcula e grava o cache de apuração sem renderizar tela.

    Usado após rotinas automáticas/manuais de atualização para que as telas
    apenas leiam resultados prontos. Não deve ser chamado na abertura de telas.
    """
    try:
        compras = load_table('compras')
        vendas = load_table('vendas')
        negociacoes = load_table('negociacoes')
        ap = calcular_apuracao(compras, negociacoes, date(2000, 1, 1), date(2099, 12, 31))
        ap_prod = calcular_apuracao_produtos(compras, negociacoes, date(2000, 1, 1), date(2099, 12, 31), vendas)
        ok = salvar_apuracao_resumida(ap, ap_prod)
        if ok:
            registrar_atualizacao('CACHE_APURACAO_AUTO', 'Sucesso', int(len(ap) + len(ap_prod)), 'Cache de apuração recalculado automaticamente após atualização das bases.')
        return ok
    except Exception as e:
        try:
            registrar_atualizacao('CACHE_APURACAO_AUTO', 'Erro', 0, str(e))
        except Exception:
            pass
        return False



# =====================================================================
# V12.3 - Cache incremental por negociação
# Ao salvar/alterar/excluir uma negociação, o sistema atualiza somente
# essa negociação no cache pronto de apuração. Assim ela aparece
# imediatamente em Apuração, Extrato e Relatórios sem recalcular tudo.
# =====================================================================
def _remover_linhas_cache_negociacao(df, codigo_negociacao='', codigo_curto=''):
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    mask = pd.Series(False, index=out.index)
    if codigo_negociacao and 'Código Negociação' in out.columns:
        mask = mask | out['Código Negociação'].astype(str).eq(str(codigo_negociacao))
    if codigo_curto and 'Código Curto' in out.columns:
        mask = mask | out['Código Curto'].astype(str).eq(str(codigo_curto))
    return out.loc[~mask].copy()


def atualizar_cache_apuracao_negociacao(negociacao_id, usuario='Sistema', motivo='Atualização incremental da negociação'):
    """Recalcula somente uma negociação no cache de apuração.

    A função preserva a performance da v12.2 porque não é chamada na
    abertura das telas. Ela roda apenas após salvar/alterar/excluir uma
    negociação, mantendo Apuração/Extrato/Relatórios sincronizados.
    """
    try:
        negociacao_id = int(negociacao_id)
    except Exception:
        return False

    try:
        with connect() as con:
            neg_row = pd.read_sql_query('SELECT * FROM negociacoes WHERE id=?', con, params=(negociacao_id,))
        if neg_row.empty:
            return False

        codigo_neg = str(neg_row.iloc[0].get('codigo_negociacao', '') or '')
        codigo_curto = str(neg_row.iloc[0].get('codigo_curto', '') or '')
        excluida = str(neg_row.iloc[0].get('status', '') or '').lower() == 'excluída' or bool(str(neg_row.iloc[0].get('excluido_em', '') or '').strip())

        ap_cache, ap_prod_cache = carregar_apuracao_resumida()
        ap_cache = _remover_linhas_cache_negociacao(ap_cache, codigo_neg, codigo_curto)
        ap_prod_cache = _remover_linhas_cache_negociacao(ap_prod_cache, codigo_neg, codigo_curto)

        # Se foi excluída, apenas remove do cache. Não recalcula.
        if not excluida:
            compras = load_table('compras')
            vendas = load_table('vendas')
            neg_calc = neg_row.copy()
            if 'excluido_em' in neg_calc.columns:
                neg_calc = neg_calc[neg_calc['excluido_em'].fillna('').astype(str).str.strip().eq('')]
            ap_one = calcular_apuracao(compras, neg_calc, date(2000, 1, 1), date(2099, 12, 31))
            ap_prod_one = calcular_apuracao_produtos(compras, neg_calc, date(2000, 1, 1), date(2099, 12, 31), vendas)
            if ap_one is not None and not ap_one.empty:
                ap_cache = pd.concat([ap_cache, ap_one], ignore_index=True)
            if ap_prod_one is not None and not ap_prod_one.empty:
                ap_prod_cache = pd.concat([ap_prod_cache, ap_prod_one], ignore_index=True)

        ok = salvar_apuracao_resumida(ap_cache, ap_prod_cache)
        if ok:
            try:
                registrar_atualizacao('CACHE_APURACAO_NEGOCIACAO', 'Sucesso', 1, f'{motivo}: {codigo_neg or negociacao_id}')
            except Exception:
                pass
        return ok
    except Exception as e:
        try:
            registrar_atualizacao('CACHE_APURACAO_NEGOCIACAO', 'Erro', 0, f'{negociacao_id}: {e}')
        except Exception:
            pass
        return False


def atualizar_cache_apuracao_negociacoes(ids, usuario='Sistema', motivo='Atualização incremental de negociações'):
    ok_geral = True
    for _id in ids or []:
        ok_geral = atualizar_cache_apuracao_negociacao(_id, usuario=usuario, motivo=motivo) and ok_geral
    return ok_geral


def consolidar_conta_corrente(ap, ap_prod):
    linhas = []
    if ap is not None and not ap.empty:
        g = ap.groupby('Fabricante/Distribuidor', dropna=False).agg({
            'Valor Compra Base': 'sum',
            'Valor Investimento a Receber': 'sum',
            'Código Negociação': 'count'
        }).reset_index().rename(columns={
            'Valor Compra Base': 'Compra Apurada',
            'Valor Investimento a Receber': 'Investimento Geral',
            'Código Negociação': 'Negociações'
        })
        linhas.append(g)
    base = pd.concat(linhas, ignore_index=True) if linhas else pd.DataFrame(columns=['Fabricante/Distribuidor','Compra Apurada','Investimento Geral','Negociações'])

    if ap_prod is not None and not ap_prod.empty:
        gp = ap_prod.groupby('Fabricante/Distribuidor', dropna=False).agg({
            'Investimento Sell In': 'sum',
            'Investimento Sell Out': 'sum',
            'Investimento': 'sum',
            'Valor Vendido': 'sum',
            'Produto': 'count'
        }).reset_index().rename(columns={
            'Investimento Sell In': 'Investimento Sell In',
            'Investimento Sell Out': 'Investimento Sell Out',
            'Investimento': 'Investimento por Produtos',
            'Valor Vendido': 'Venda Apurada',
            'Produto': 'Produtos'
        })
        base = base.merge(gp, on='Fabricante/Distribuidor', how='outer')

    if base.empty:
        return base
    for c in ['Compra Apurada','Investimento Geral','Investimento Sell In','Investimento Sell Out','Investimento por Produtos','Venda Apurada','Negociações','Produtos']:
        if c not in base.columns:
            base[c] = 0
        base[c] = pd.to_numeric(base[c], errors='coerce').fillna(0)
    base['Investimento Total a Receber'] = base['Investimento Geral'] + base['Investimento por Produtos']
    base['Recebido'] = 0.0
    try:
        rec = load_table_safe('recebimentos_negociacao')
        neg_local = load_table_safe('negociacoes')
        if not rec.empty and not neg_local.empty and 'negociacao_id' in rec.columns and 'id' in neg_local.columns:
            rec['valor_recebido'] = to_numero(rec.get('valor_recebido')).fillna(0)
            rec['negociacao_id'] = rec['negociacao_id'].astype(str)
            neg_local['id_str'] = neg_local['id'].astype(str)
            rec_nome = rec.merge(neg_local[['id_str','nome']], left_on='negociacao_id', right_on='id_str', how='left')
            rec_grp = rec_nome.groupby('nome')['valor_recebido'].sum().to_dict()
            base['Recebido'] = base['Fabricante/Distribuidor'].map(rec_grp).fillna(0.0)
    except Exception:
        base['Recebido'] = 0.0
    base['Saldo a Receber'] = base['Investimento Total a Receber'] - base['Recebido']
    return base.sort_values('Saldo a Receber', ascending=False)



@st.cache_data(ttl=300, show_spinner=False)
def carregar_lancamentos_financeiros():
    """Une lançamentos financeiros novos com recebimentos antigos para manter compatibilidade."""
    lanc = load_table_safe('financeiro_lancamentos')
    if lanc.empty:
        lanc = pd.DataFrame(columns=['id','negociacao_id','data_lancamento','competencia','tipo_movimento','natureza','valor','documento','usuario','observacao','criado_em','origem_lancamento','entidade_tipo','entidade_nome'])
    if 'origem_tabela' not in lanc.columns:
        lanc['origem_tabela'] = 'financeiro_lancamentos'
    else:
        lanc['origem_tabela'] = lanc['origem_tabela'].fillna('financeiro_lancamentos')
    rec = load_table_safe('recebimentos_negociacao')
    if rec is not None and not rec.empty:
        rec2 = pd.DataFrame()
        rec2['id'] = rec.get('id')
        rec2['negociacao_id'] = rec.get('negociacao_id')
        rec2['data_lancamento'] = rec.get('data_recebimento')
        rec2['competencia'] = rec.get('competencia', '') if 'competencia' in rec.columns else rec.get('data_recebimento').map(competencia_from_date)
        rec2['tipo_movimento'] = rec.get('forma_recebimento', 'Recebimento')
        rec2['natureza'] = 'Débito'
        rec2['valor'] = rec.get('valor_recebido')
        rec2['documento'] = rec.get('forma_recebimento', '')
        rec2['usuario'] = rec.get('usuario', '')
        rec2['observacao'] = rec.get('observacao', '')
        rec2['criado_em'] = rec.get('criado_em', '')
        rec2['origem'] = 'Recebimentos'
        rec2['origem_lancamento'] = 'Recebimentos antigos'
        rec2['origem_tabela'] = 'recebimentos_negociacao'
        rec2['status_lancamento'] = rec.get('status_lancamento', 'Ativo') if 'status_lancamento' in rec.columns else 'Ativo'
        rec2['parent_credito_id'] = 0
        lanc['origem'] = 'Lançamentos'
        lanc['origem_tabela'] = 'financeiro_lancamentos'
        lanc = pd.concat([lanc, rec2], ignore_index=True, sort=False)
    if not lanc.empty:
        if 'status_lancamento' not in lanc.columns:
            lanc['status_lancamento'] = 'Ativo'
        lanc['status_lancamento'] = lanc['status_lancamento'].fillna('Ativo').astype(str)
        # Lixeira financeira: registros excluídos não entram no extrato,
        # na conta corrente, nos dashboards ou nos saldos.
        lanc = lanc[~lanc['status_lancamento'].str.lower().eq('excluído') & ~lanc['status_lancamento'].str.lower().eq('excluido')].copy()
        lanc['valor'] = to_numero(lanc.get('valor')).fillna(0)
        if 'competencia' not in lanc.columns:
            lanc['competencia'] = ''
        lanc['competencia'] = lanc['competencia'].fillna('').astype(str)
        mask_sem_comp = lanc['competencia'].str.strip().eq('')
        if 'data_lancamento' in lanc.columns:
            lanc.loc[mask_sem_comp, 'competencia'] = lanc.loc[mask_sem_comp, 'data_lancamento'].map(competencia_from_date)
        for c in ['origem_lancamento','entidade_tipo','entidade_nome','origem_tabela']:
            if c not in lanc.columns:
                lanc[c] = ''
            lanc[c] = lanc[c].fillna('').astype(str)
    return lanc






def listar_lancamentos_avulsos_financeiros(entidade_tipo=None, entidade_nome=None):
    """Lista lançamentos financeiros avulsos ativos para edição/exclusão."""
    with connect() as con:
        df = pd.read_sql_query("SELECT * FROM financeiro_lancamentos", con)
    if df.empty:
        return df
    for col in ['origem_lancamento','entidade_tipo','entidade_nome','status_lancamento']:
        if col not in df.columns:
            df[col] = ''
        df[col] = df[col].fillna('').astype(str)
    df = df[~df['status_lancamento'].str.lower().isin(['excluído','excluido'])].copy()
    mask_avulso = df['origem_lancamento'].str.lower().str.contains('avulso', na=False) | df.get('negociacao_id', pd.Series(dtype=str)).astype(str).isin(['0','', 'nan', 'None'])
    df = df[mask_avulso].copy()
    if entidade_tipo and entidade_tipo != 'Todos':
        df = df[df['entidade_tipo'].astype(str).str.lower().eq(str(entidade_tipo).lower())].copy()
    if entidade_nome and entidade_nome != 'Todos':
        df = df[df['entidade_nome'].astype(str).eq(str(entidade_nome))].copy()
    if 'valor' in df.columns:
        df['valor'] = to_numero(df['valor']).fillna(0)
    return df.sort_values(['data_lancamento','id'], ascending=[False, False])


def atualizar_lancamento_avulso(lancamento_id, dados, usuario='Sistema'):
    """Atualiza lançamento avulso e preserva auditoria."""
    campos_permitidos = [
        'data_lancamento','competencia','tipo_movimento','natureza','valor','documento',
        'observacao','entidade_tipo','entidade_nome','data_vencimento','forma_pagamento',
        'parent_credito_id','status_credito'
    ]
    sets = []
    vals = []
    for c in campos_permitidos:
        if c in dados:
            sets.append(f"{c}=?")
            vals.append(dados[c])
    if not sets:
        return False, 'Nenhum campo para atualizar.'
    sets.extend(['alterado_em=CURRENT_TIMESTAMP','alterado_por=?'])
    vals.append(usuario)
    vals.append(int(lancamento_id))
    with connect() as con:
        antigo = pd.read_sql_query('SELECT * FROM financeiro_lancamentos WHERE id=?', con, params=(int(lancamento_id),))
        con.execute(f"UPDATE financeiro_lancamentos SET {', '.join(sets)} WHERE id=?", vals)
        try:
            # Auditoria simplificada: registra alteração como histórico financeiro quando houver negociação vinculada.
            if not antigo.empty and int(float(antigo.iloc[0].get('negociacao_id') or 0)):
                neg_id = int(float(antigo.iloc[0].get('negociacao_id') or 0))
                registrar_historico(con, neg_id, str(neg_id), 'Alteração lançamento financeiro', str(antigo.iloc[0].to_dict()), str(dados), usuario=usuario, observacao='Edição de lançamento avulso/financeiro')
        except Exception:
            pass
        con.commit()
    try:
        carregar_lancamentos_financeiros.clear()
    except Exception:
        pass
    try:
        atualizar_status_creditos_financeiros()
    except Exception:
        pass
    return True, 'Lançamento atualizado com sucesso.'


def excluir_lancamento_avulso(lancamento_id, motivo, usuario='Sistema'):
    """Exclusão lógica do lançamento avulso com validação de vínculos."""
    with connect() as con:
        atual = pd.read_sql_query('SELECT * FROM financeiro_lancamentos WHERE id=?', con, params=(int(lancamento_id),))
        if atual.empty:
            return False, 'Lançamento não encontrado.'
        filhos = pd.read_sql_query("""
            SELECT id, valor, tipo_movimento
            FROM financeiro_lancamentos
            WHERE COALESCE(parent_credito_id,0)=?
              AND COALESCE(status_lancamento,'Ativo') NOT IN ('Excluído','Excluido')
        """, con, params=(int(lancamento_id),))
        if not filhos.empty:
            return False, 'Este crédito possui recebimentos/baixas vinculados. Faça um estorno ou exclua primeiro as baixas vinculadas.'
        con.execute("""
            UPDATE financeiro_lancamentos
               SET status_lancamento='Excluído', excluido_em=CURRENT_TIMESTAMP,
                   excluido_por=?, motivo_exclusao=?
             WHERE id=?
        """, (usuario, str(motivo or ''), int(lancamento_id)))
        try:
            neg_id = int(float(atual.iloc[0].get('negociacao_id') or 0))
            if neg_id:
                registrar_historico(con, neg_id, str(neg_id), 'Exclusão lançamento financeiro', str(atual.iloc[0].to_dict()), 'Excluído', usuario=usuario, observacao=str(motivo or ''))
        except Exception:
            pass
        con.commit()
    try:
        limpar_cache_telas()
    except Exception:
        pass
    try:
        atualizar_status_creditos_financeiros()
    except Exception:
        pass
    try:
        limpar_cache_telas()
    except Exception:
        pass
    return True, 'Lançamento excluído com sucesso. Ele saiu do extrato e ficará preservado na auditoria.'


def excluir_recebimento_antigo(recebimento_id, motivo, usuario='Sistema'):
    # Exclusão lógica de recebimentos da tabela legada recebimentos_negociacao.
    with connect() as con:
        atual = pd.read_sql_query('SELECT * FROM recebimentos_negociacao WHERE id=?', con, params=(int(recebimento_id),))
        if atual.empty:
            return False, 'Recebimento antigo não encontrado.'
        for col_def in [
            "status_lancamento TEXT DEFAULT 'Ativo'",
            'excluido_em TEXT',
            'excluido_por TEXT',
            'motivo_exclusao TEXT'
        ]:
            try:
                con.execute(f'ALTER TABLE recebimentos_negociacao ADD COLUMN {col_def}')
            except Exception:
                pass
        con.execute("""
            UPDATE recebimentos_negociacao
               SET status_lancamento='Excluído', excluido_em=CURRENT_TIMESTAMP,
                   excluido_por=?, motivo_exclusao=?
             WHERE id=?
        """, (usuario, str(motivo or ''), int(recebimento_id)))
        try:
            neg_id = int(float(atual.iloc[0].get('negociacao_id') or 0))
            if neg_id:
                registrar_historico(con, neg_id, str(neg_id), 'Exclusão recebimento financeiro', str(atual.iloc[0].to_dict()), 'Excluído', usuario=usuario, observacao=str(motivo or ''))
        except Exception:
            pass
        con.commit()
    try:
        limpar_cache_telas()
    except Exception:
        pass
    try:
        atualizar_status_creditos_financeiros()
    except Exception:
        pass
    try:
        limpar_cache_telas()
    except Exception:
        pass
    return True, 'Recebimento excluído com sucesso. Ele saiu do extrato e ficará preservado na auditoria.'


def excluir_lancamento_extrato(lancamento_id, origem_registro, motivo, usuario='Sistema'):
    # Exclui o lançamento correto, seja financeiro_lancamentos ou recebimentos_negociacao.
    origem = str(origem_registro or '').lower()
    if 'recebimentos_negociacao' in origem or 'recebimento' in origem:
        return excluir_recebimento_antigo(lancamento_id, motivo, usuario=usuario)
    return excluir_lancamento_avulso(lancamento_id, motivo, usuario=usuario)

def _is_credito_natureza(natureza):
    txt = str(natureza or '').lower()
    return ('crédito' in txt) or ('credito' in txt) or ('a receber' in txt)


def _is_recebimento_tipo(tipo_movimento):
    txt = str(tipo_movimento or '').lower()
    return any(x in txt for x in ['receb', 'pix', 'ted', 'boleto', 'baixa'])


def atualizar_status_creditos_financeiros():
    # Recalcula status dos créditos financeiros manuais com base nos recebimentos/glosas vinculados.
    with connect() as con:
        lanc = pd.read_sql_query('SELECT * FROM financeiro_lancamentos', con)
        if lanc.empty or 'parent_credito_id' not in lanc.columns:
            return
        lanc['valor_num'] = pd.to_numeric(lanc.get('valor'), errors='coerce').fillna(0)
        creditos = lanc[lanc.get('natureza','').astype(str).map(_is_credito_natureza)].copy()
        for _, cred in creditos.iterrows():
            cid = int(cred.get('id') or 0)
            valor_credito = float(cred.get('valor_num') or 0)
            baixas = lanc[pd.to_numeric(lanc.get('parent_credito_id',0), errors='coerce').fillna(0).astype(int).eq(cid)].copy()
            valor_baixado = float(baixas['valor_num'].sum()) if not baixas.empty else 0.0
            if valor_baixado <= 0:
                status = 'Em Aberto'
            elif valor_baixado + 0.01 < valor_credito:
                status = 'Parcialmente Recebido'
            else:
                status = 'Recebido'
            con.execute('UPDATE financeiro_lancamentos SET status_credito=? WHERE id=?', (status, cid))
        con.commit()


def listar_creditos_em_aberto(tipo_entidade=None, nome_entidade=None, negociacao_id=None):
    # Lista créditos a receber ainda com saldo para baixa/recebimento.
    atualizar_status_creditos_financeiros()
    lanc = load_table_safe('financeiro_lancamentos')
    if lanc.empty:
        return pd.DataFrame()
    for c in ['parent_credito_id','valor','negociacao_id']:
        if c not in lanc.columns:
            lanc[c] = 0
    lanc['id_int'] = pd.to_numeric(lanc.get('id'), errors='coerce').fillna(0).astype(int)
    lanc['valor_num'] = pd.to_numeric(lanc.get('valor'), errors='coerce').fillna(0)
    lanc['parent_int'] = pd.to_numeric(lanc.get('parent_credito_id'), errors='coerce').fillna(0).astype(int)
    creditos = lanc[lanc.get('natureza','').astype(str).map(_is_credito_natureza)].copy()
    if tipo_entidade:
        creditos = creditos[creditos.get('entidade_tipo','').astype(str).eq(str(tipo_entidade))]
    if nome_entidade:
        creditos = creditos[creditos.get('entidade_nome','').astype(str).eq(str(nome_entidade))]
    if negociacao_id:
        creditos = creditos[pd.to_numeric(creditos.get('negociacao_id'), errors='coerce').fillna(0).astype(int).eq(int(negociacao_id))]
    baixas = lanc[lanc['parent_int'].gt(0)].groupby('parent_int')['valor_num'].sum().to_dict()
    creditos['Valor Crédito'] = creditos['valor_num']
    creditos['Valor Baixado'] = creditos['id_int'].map(baixas).fillna(0.0)
    creditos['Saldo em Aberto'] = creditos['Valor Crédito'] - creditos['Valor Baixado']
    creditos = creditos[creditos['Saldo em Aberto'] > 0.01].copy()
    creditos['Label'] = creditos.apply(lambda r: f"#{int(r.get('id_int',0))} | {str(r.get('competencia',''))} | {str(r.get('tipo_movimento','Crédito a receber'))} | {money(float(r.get('Saldo em Aberto',0)))} em aberto", axis=1)
    return creditos.sort_values(['competencia','data_lancamento','id_int'], na_position='last')

def montar_creditos_negociacoes(ap, ap_prod, negociacoes):
    """Cria a base de créditos apurados por negociação."""
    neg = negociacoes.copy() if negociacoes is not None else pd.DataFrame()
    if neg.empty:
        return pd.DataFrame(columns=['negociacao_id','Código Negociação','Tipo Entidade','Fabricante/Fornecedor','Histórico','Crédito Sell In','Crédito Sell Out','Crédito Geral','Crédito Total'])
    neg_map = neg.set_index('codigo_negociacao').to_dict('index') if 'codigo_negociacao' in neg.columns else {}
    rows = []
    if ap is not None and not ap.empty:
        for _, r in ap.iterrows():
            cod = str(r.get('Código Negociação',''))
            info = neg_map.get(cod, {})
            credito = float(pd.to_numeric(pd.Series([r.get('Valor Investimento a Receber',0)]), errors='coerce').fillna(0).iloc[0])
            if credito:
                faixa_txt = str(r.get('Faixa Atingida','') or '').strip()
                regra_txt = str(r.get('Regra Faixa','') or '').strip()
                hist = 'Crédito geral da negociação'
                if regra_txt:
                    hist = f'Crédito por faixa progressiva ({faixa_txt})' if faixa_txt else 'Crédito por faixa progressiva'
                rows.append({'negociacao_id': info.get('id',''), 'Código Negociação': cod, 'Tipo Entidade': info.get('tipo', r.get('Tipo','')), 'Fabricante/Fornecedor': r.get('Fabricante/Distribuidor', info.get('nome','')), 'Histórico': hist, 'Crédito Sell In': 0.0, 'Crédito Sell Out': 0.0, 'Crédito Geral': credito, 'Crédito Total': credito})
    if ap_prod is not None and not ap_prod.empty:
        grp = ap_prod.copy()
        for c in ['Investimento Sell In','Investimento Sell Out','Investimento']:
            if c not in grp.columns:
                grp[c] = 0
            grp[c] = pd.to_numeric(grp[c], errors='coerce').fillna(0)
        g = grp.groupby(['Código Negociação','Fabricante/Distribuidor'], dropna=False)[['Investimento Sell In','Investimento Sell Out','Investimento']].sum().reset_index()
        for _, r in g.iterrows():
            cod = str(r.get('Código Negociação',''))
            info = neg_map.get(cod, {})
            si = float(r.get('Investimento Sell In',0) or 0)
            so = float(r.get('Investimento Sell Out',0) or 0)
            total = float(r.get('Investimento',0) or 0)
            if total == 0 and (si or so):
                total = si + so
            if total:
                rows.append({'negociacao_id': info.get('id',''), 'Código Negociação': cod, 'Tipo Entidade': info.get('tipo',''), 'Fabricante/Fornecedor': r.get('Fabricante/Distribuidor', info.get('nome','')), 'Histórico': 'Crédito por produto / Sell In / Sell Out', 'Crédito Sell In': si, 'Crédito Sell Out': so, 'Crédito Geral': 0.0, 'Crédito Total': total})
    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=['negociacao_id','Código Negociação','Tipo Entidade','Fabricante/Fornecedor','Histórico','Crédito Sell In','Crédito Sell Out','Crédito Geral','Crédito Total'])
    for c in ['Crédito Sell In','Crédito Sell Out','Crédito Geral','Crédito Total']:
        out[c] = pd.to_numeric(out[c], errors='coerce').fillna(0)
    # Segurança: a conta corrente deve exibir apenas 1 crédito consolidado por negociação/entidade.
    # Em negociação por faixa progressiva, as faixas são parâmetros internos; não viram múltiplos créditos.
    if not out.empty:
        chaves = ['negociacao_id', 'Código Negociação', 'Tipo Entidade', 'Fabricante/Fornecedor']
        out = (out.groupby(chaves, dropna=False)
                  .agg({'Histórico': lambda x: ' + '.join([str(v) for v in x.dropna().astype(str).unique() if str(v).strip()])[:250],
                        'Crédito Sell In': 'sum',
                        'Crédito Sell Out': 'sum',
                        'Crédito Geral': 'sum',
                        'Crédito Total': 'sum'})
                  .reset_index())
    return out


def montar_posicao_financeira(ap, ap_prod, negociacoes):
    creditos = montar_creditos_negociacoes(ap, ap_prod, negociacoes)
    neg = negociacoes.copy() if negociacoes is not None else pd.DataFrame()
    lanc = carregar_lancamentos_financeiros()
    if creditos.empty:
        pos = pd.DataFrame(columns=['Tipo Entidade','Fabricante/Fornecedor','Negociações','Crédito Sell In','Crédito Sell Out','Crédito Geral','Total Créditos','Total Débitos','Saldo em Aberto'])
    else:
        pos = creditos.groupby(['Tipo Entidade','Fabricante/Fornecedor'], dropna=False).agg({'Código Negociação':'nunique','Crédito Sell In':'sum','Crédito Sell Out':'sum','Crédito Geral':'sum','Crédito Total':'sum'}).reset_index().rename(columns={'Código Negociação':'Negociações','Crédito Total':'Total Créditos'})
    if not lanc.empty and not neg.empty and 'negociacao_id' in lanc.columns:
        neg['id_str'] = neg['id'].astype(str)
        lanc['negociacao_id_str'] = lanc['negociacao_id'].astype(str)
        lanc = lanc.merge(neg[['id_str','tipo','nome']], left_on='negociacao_id_str', right_on='id_str', how='left')
        lanc['valor'] = to_numero(lanc.get('valor')).fillna(0)
        lanc['natureza_norm'] = lanc.get('natureza','Débito').astype(str).str.lower()
        lanc['debito'] = lanc.apply(lambda r: float(r['valor']) if 'débito' in r['natureza_norm'] or 'debito' in r['natureza_norm'] else 0.0, axis=1)
        lanc['credito_extra'] = lanc.apply(lambda r: float(r['valor']) if 'crédito' in r['natureza_norm'] or 'credito' in r['natureza_norm'] else 0.0, axis=1)
        deb = lanc.groupby(['tipo','nome'], dropna=False)[['debito','credito_extra']].sum().reset_index().rename(columns={'tipo':'Tipo Entidade','nome':'Fabricante/Fornecedor','debito':'Total Débitos','credito_extra':'Outros Créditos'})
        if pos.empty:
            pos = deb.copy()
            for c in ['Negociações','Crédito Sell In','Crédito Sell Out','Crédito Geral','Total Créditos']:
                pos[c] = 0
        else:
            pos = pos.merge(deb, on=['Tipo Entidade','Fabricante/Fornecedor'], how='outer')
    for c in ['Negociações','Crédito Sell In','Crédito Sell Out','Crédito Geral','Total Créditos','Outros Créditos','Total Débitos']:
        if c not in pos.columns:
            pos[c] = 0
        pos[c] = pd.to_numeric(pos[c], errors='coerce').fillna(0)
    pos['Total Créditos'] = pos['Total Créditos'] + pos['Outros Créditos']
    pos['Saldo em Aberto'] = pos['Total Créditos'] - pos['Total Débitos']
    return pos.sort_values('Saldo em Aberto', ascending=False)


def montar_extrato_entidade(ap, ap_prod, negociacoes, tipo_entidade, nome_entidade):
    creditos = montar_creditos_negociacoes(ap, ap_prod, negociacoes)
    neg = negociacoes.copy() if negociacoes is not None else pd.DataFrame()
    linhas = []
    if not creditos.empty:
        filt = creditos[(creditos['Tipo Entidade'].astype(str) == str(tipo_entidade)) & (creditos['Fabricante/Fornecedor'].astype(str) == str(nome_entidade))].copy()
        for _, r in filt.iterrows():
            val = float(r.get('Crédito Total',0) or 0)
            if val:
                linhas.append({'Data': '', 'Documento': str(r.get('Código Negociação','')), 'Negociação': str(r.get('Código Negociação','')), 'Histórico': str(r.get('Histórico','Crédito apurado')), 'Crédito': val, 'Débito': 0.0, 'Usuário': '', 'Observação': ''})
    lanc = carregar_lancamentos_financeiros()
    if not lanc.empty and not neg.empty:
        neg['id_str'] = neg['id'].astype(str)
        lanc['negociacao_id_str'] = lanc['negociacao_id'].astype(str)
        cols_neg = ['id_str','tipo','nome','codigo_negociacao'] + (['codigo_curto'] if 'codigo_curto' in neg.columns else [])
        lanc = lanc.merge(neg[cols_neg], left_on='negociacao_id_str', right_on='id_str', how='left')
        lanc = lanc[(lanc['tipo'].astype(str)==str(tipo_entidade)) & (lanc['nome'].astype(str)==str(nome_entidade))].copy()
        for _, r in lanc.sort_values('data_lancamento').iterrows():
            natureza = str(r.get('natureza','Débito')).lower()
            valor = float(pd.to_numeric(pd.Series([r.get('valor',0)]), errors='coerce').fillna(0).iloc[0])
            is_credito = ('crédito' in natureza) or ('credito' in natureza)
            linhas.append({'Data': str(r.get('data_lancamento','')), 'Documento': str(r.get('documento','')), 'Negociação': str(r.get('codigo_curto') or r.get('codigo_negociacao') or r.get('negociacao_id','')), 'Histórico': str(r.get('tipo_movimento','Lançamento financeiro')), 'Crédito': valor if is_credito else 0.0, 'Débito': 0.0 if is_credito else valor, 'Usuário': str(r.get('usuario','')), 'Observação': str(r.get('observacao',''))})
    ext = pd.DataFrame(linhas)
    if ext.empty:
        return pd.DataFrame(columns=['Data','Documento','Negociação','Histórico','Crédito','Débito','Saldo','Usuário','Observação'])
    ext['Crédito'] = pd.to_numeric(ext['Crédito'], errors='coerce').fillna(0)
    ext['Débito'] = pd.to_numeric(ext['Débito'], errors='coerce').fillna(0)
    ext['Saldo'] = (ext['Crédito'] - ext['Débito']).cumsum()
    return ext


def _parse_data_segura(valor, padrao=None):
    try:
        dt = pd.to_datetime(valor, errors='coerce')
        if pd.isna(dt):
            return padrao or date.today()
        return dt.date()
    except Exception:
        return padrao or date.today()


def listar_entidades_com_negociacao(negociacoes):
    if negociacoes is None or negociacoes.empty:
        return pd.DataFrame(columns=['Tipo Entidade','Fabricante/Fornecedor'])
    out = negociacoes.copy()
    out['Tipo Entidade'] = out.get('tipo', '').astype(str).replace('', 'Fabricante')
    out['Fabricante/Fornecedor'] = out.get('nome', '').astype(str)
    out = out[['Tipo Entidade','Fabricante/Fornecedor']].dropna().drop_duplicates()
    out = out[out['Fabricante/Fornecedor'].astype(str).str.strip()!='']
    return out.sort_values(['Tipo Entidade','Fabricante/Fornecedor'])


def montar_extrato_bancario(ap, ap_prod, negociacoes, tipo_entidade='Todos', nome_entidade='Todos', data_ini=None, data_fim=None, situacao='Todos', tipo_lanc='Todos'):
    """Monta extrato estilo banco: créditos automáticos da apuração + débitos/créditos manuais.

    Crédito = valores apurados/ajustes a receber.
    Débito = recebimentos, glosas, abatimentos, baixas e estornos que reduzem o saldo.
    """
    neg = negociacoes.copy() if negociacoes is not None else pd.DataFrame()
    if neg.empty:
        return pd.DataFrame(columns=['Competência','Data','Histórico','Negociação','Documento','Crédito (R$)','Débito (R$)','Saldo (R$)','Tipo','Situação','Data Recebimento','Comprovantes','Usuário','Observação','Fabricante/Fornecedor'])
    if 'id' not in neg.columns:
        return pd.DataFrame()
    neg['id_str'] = neg['id'].astype(str)
    if 'codigo_curto' not in neg.columns:
        neg['codigo_curto'] = neg.get('codigo_negociacao', neg['id_str'])
    neg['Tipo Entidade'] = neg.get('tipo', '').astype(str).replace('', 'Fabricante')
    neg['Fabricante/Fornecedor'] = neg.get('nome', '').astype(str)

    linhas = []
    creditos = montar_creditos_negociacoes(ap, ap_prod, neg)
    if creditos is not None and not creditos.empty:
        for _, r in creditos.iterrows():
            cod = str(r.get('Código Negociação',''))
            nrow = neg[(neg.get('codigo_negociacao','').astype(str)==cod) | (neg.get('codigo_curto','').astype(str)==cod)]
            info = nrow.iloc[0].to_dict() if not nrow.empty else {}
            data_ref = info.get('data_fim') or info.get('data_inicio') or ''
            competencia_ref = competencia_from_date(data_ref)
            credito = float(pd.to_numeric(pd.Series([r.get('Crédito Total',0)]), errors='coerce').fillna(0).iloc[0])
            if credito:
                hist = str(r.get('Histórico') or 'Crédito apurado')
                tipo_credito = 'Crédito'
                linhas.append({
                    'Lançamento ID': '',
                    'Crédito Vinculado': '',
                    'Competência': competencia_ref,
                    'Data': data_ref,
                    'Histórico': hist,
                    'Negociação': str(info.get('codigo_curto') or cod),
                    'Documento': f"APU-{str(info.get('id') or '').zfill(5)}" if info else cod,
                    'Crédito (R$)': credito,
                    'Débito (R$)': 0.0,
                    'Tipo': 'Crédito a Receber',
                    'Situação': 'Em Aberto',
                    'Data Recebimento': '',
                    'Comprovantes': '',
                    'Usuário': '',
                    'Observação': 'Gerado automaticamente pela apuração',
                    'Origem Registro': 'Apuração',
                    'Tipo Entidade': info.get('Tipo Entidade', r.get('Tipo Entidade','')),
                    'Fabricante/Fornecedor': info.get('Fabricante/Fornecedor', r.get('Fabricante/Fornecedor','')),
                })

    lanc = carregar_lancamentos_financeiros()
    comp_counts = contar_comprovantes_financeiros()
    if lanc is not None and not lanc.empty:
        lanc = lanc.copy()
        lanc['negociacao_id_str'] = lanc.get('negociacao_id','').astype(str)
        cols_neg = ['id_str','tipo','nome','codigo_negociacao','codigo_curto','data_inicio','data_fim']
        cols_neg = [c for c in cols_neg if c in neg.columns]
        lanc = lanc.merge(neg[cols_neg], left_on='negociacao_id_str', right_on='id_str', how='left')
        for _, r in lanc.iterrows():
            natureza = str(r.get('natureza','Débito')).lower()
            valor = float(pd.to_numeric(pd.Series([r.get('valor',0)]), errors='coerce').fillna(0).iloc[0])
            is_credito = _is_credito_natureza(natureza)
            tipo_mov = str(r.get('tipo_movimento') or ('Crédito a receber' if is_credito else 'Débito'))
            status_cred = str(r.get('status_credito') or '')
            if is_credito:
                situacao_l = status_cred if status_cred.strip() else 'Em Aberto'
            else:
                situacao_l = 'Recebido' if _is_recebimento_tipo(tipo_mov) else ('Baixa' if valor else '-')
            def _valido_extrato(v):
                return v is not None and not pd.isna(v) and str(v).strip() not in ['', 'nan', 'None']
            tipo_ent_linha = r.get('tipo') if _valido_extrato(r.get('tipo')) else r.get('entidade_tipo')
            nome_ent_linha = r.get('nome') if _valido_extrato(r.get('nome')) else r.get('entidade_nome')
            neg_linha = r.get('codigo_curto') if _valido_extrato(r.get('codigo_curto')) else (r.get('codigo_negociacao') if _valido_extrato(r.get('codigo_negociacao')) else ('AVULSO' if _valido_extrato(r.get('entidade_nome')) else r.get('negociacao_id','')))
            linhas.append({
                'Lançamento ID': str(r.get('id','')),
                'Crédito Vinculado': str(int(float(r.get('parent_credito_id') or 0))) if str(r.get('parent_credito_id','')).strip() not in ['', 'nan'] else '',
                'Competência': str(r.get('competencia','') or competencia_from_date(r.get('data_lancamento',''))),
                'Data': r.get('data_lancamento',''),
                'Histórico': tipo_mov,
                'Negociação': str(neg_linha),
                'Documento': str(r.get('documento','')),
                'Crédito (R$)': valor if is_credito else 0.0,
                'Débito (R$)': 0.0 if is_credito else valor,
                'Tipo': 'Crédito a Receber' if is_credito else 'Baixa / Recebimento',
                'Situação': situacao_l,
                'Data Recebimento': str(r.get('data_lancamento','')) if situacao_l == 'Recebido' else '',
                'Comprovantes': int(comp_counts.get(str(r.get('id','')), 0)),
                'Usuário': str(r.get('usuario','')), 
                'Observação': str(r.get('observacao','')),
                'Origem Registro': str(r.get('origem_tabela') or r.get('origem') or 'financeiro_lancamentos'),
                'Tipo Entidade': str(tipo_ent_linha or ''),
                'Fabricante/Fornecedor': str(nome_ent_linha or ''),
            })

    ext = pd.DataFrame(linhas)
    if ext.empty:
        return pd.DataFrame(columns=['Competência','Data','Histórico','Negociação','Documento','Crédito (R$)','Débito (R$)','Saldo (R$)','Tipo','Situação','Data Recebimento','Comprovantes','Usuário','Observação','Fabricante/Fornecedor'])

    # Filtros por entidade
    if tipo_entidade and tipo_entidade != 'Todos':
        ext = ext[ext['Tipo Entidade'].astype(str) == str(tipo_entidade)]
    if nome_entidade and nome_entidade != 'Todos':
        ext = ext[ext['Fabricante/Fornecedor'].astype(str) == str(nome_entidade)]
    if situacao and situacao != 'Todos':
        ext = ext[ext['Situação'].astype(str) == str(situacao)]
    if tipo_lanc and tipo_lanc != 'Todos':
        ext = ext[ext['Tipo'].astype(str) == str(tipo_lanc)]

    ext['Data_dt'] = pd.to_datetime(ext['Data'], errors='coerce')
    if data_ini is not None:
        ext = ext[(ext['Data_dt'].isna()) | (ext['Data_dt'].dt.date >= data_ini)]
    if data_fim is not None:
        ext = ext[(ext['Data_dt'].isna()) | (ext['Data_dt'].dt.date <= data_fim)]
    if ext.empty:
        return pd.DataFrame(columns=['Competência','Data','Histórico','Negociação','Documento','Crédito (R$)','Débito (R$)','Saldo (R$)','Tipo','Situação','Data Recebimento','Comprovantes','Usuário','Observação','Fabricante/Fornecedor'])

    ext['Crédito (R$)'] = pd.to_numeric(ext['Crédito (R$)'], errors='coerce').fillna(0)
    ext['Débito (R$)'] = pd.to_numeric(ext['Débito (R$)'], errors='coerce').fillna(0)
    ext = ext.sort_values(['Data_dt','Tipo','Histórico'], na_position='first').reset_index(drop=True)
    ext['Saldo (R$)'] = (ext['Crédito (R$)'] - ext['Débito (R$)']).cumsum()
    return ext.drop(columns=['Data_dt'], errors='ignore')


def gerar_pdf_extrato_financeiro(ext, titulo='Extrato Financeiro', filtros=''):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    except Exception as exc:
        raise RuntimeError('Dependência reportlab ausente. Execute: pip install reportlab') from exc
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=0.8*cm, rightMargin=0.8*cm, topMargin=0.7*cm, bottomMargin=0.7*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('TituloSB', parent=styles['Title'], fontSize=17, textColor=colors.HexColor('#0B2A42'), alignment=1, spaceAfter=8)
    small = ParagraphStyle('SmallSB', parent=styles['Normal'], fontSize=8, textColor=colors.HexColor('#334155'))
    elems = [Paragraph('SB FARMA', title_style), Paragraph(titulo, styles['Heading2'])]
    if filtros:
        elems.append(Paragraph(filtros, small))
    elems.append(Spacer(1, 0.25*cm))
    total_credito = float(ext['Crédito (R$)'].sum()) if not ext.empty and 'Crédito (R$)' in ext.columns else 0
    total_debito = float(ext['Débito (R$)'].sum()) if not ext.empty and 'Débito (R$)' in ext.columns else 0
    saldo = float(ext['Saldo (R$)'].iloc[-1]) if not ext.empty and 'Saldo (R$)' in ext.columns else 0
    resumo = [['Total de Créditos', money(total_credito), 'Total de Débitos', money(total_debito), 'Saldo Atual', money(saldo)]]
    t_res = Table(resumo, colWidths=[3.2*cm, 3.0*cm, 3.2*cm, 3.0*cm, 2.8*cm, 3.0*cm])
    t_res.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#EAF2F8')),('TEXTCOLOR',(0,0),(-1,-1),colors.HexColor('#0B2A42')),('FONTNAME',(0,0),(-1,-1),'Helvetica-Bold'),('GRID',(0,0),(-1,-1),0.25,colors.HexColor('#CBD5E1')),('VALIGN',(0,0),(-1,-1),'MIDDLE')]))
    elems += [t_res, Spacer(1,0.35*cm)]
    cols = ['Data','Histórico','Negociação','Documento','Crédito (R$)','Débito (R$)','Saldo (R$)','Tipo','Situação']
    rows = [cols]
    view = ext.copy() if ext is not None else pd.DataFrame(columns=cols)
    for c in ['Crédito (R$)','Débito (R$)','Saldo (R$)']:
        if c in view.columns:
            view[c] = view[c].map(money)
    for _, r in view[cols].head(500).iterrows():
        rows.append([str(r.get(c,''))[:45] for c in cols])
    tab = Table(rows, repeatRows=1, colWidths=[2.0*cm,5.2*cm,2.5*cm,2.5*cm,2.6*cm,2.6*cm,2.6*cm,2.0*cm,2.0*cm])
    tab.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#0B2A42')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,-1),7),('GRID',(0,0),(-1,-1),0.25,colors.HexColor('#CBD5E1')),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white, colors.HexColor('#F8FAFC')]),('ALIGN',(4,1),(6,-1),'RIGHT')
    ]))
    elems.append(tab)
    doc.build(elems)
    buffer.seek(0)
    return buffer.getvalue()



def aviso_streamlit_cloud():
    if not CLOUD_MODE:
        return
    if not st.session_state.get('_aviso_cloud_exibido'):
        st.info('Modo Streamlit Cloud ativo. O app usa st.secrets para o PostgreSQL e sincroniza negociações/financeiro/comprovantes no banco externo quando [app].state_sync=true. Caches de compras/vendas podem ser recriados pela rotina de atualização.')
        st.session_state['_aviso_cloud_exibido'] = True


def mostrar_df_moeda(df, moedas=None, percentuais=None):
    show = df.copy()
    moedas = moedas or []
    percentuais = percentuais or []
    for c in moedas:
        if c in show.columns:
            show[c] = show[c].map(money)
    for c in percentuais:
        if c in show.columns:
            show[c] = show[c].map(pct)
    st.dataframe(show, use_container_width=True)

def inicializar_app_leve():
    """Inicializa estrutura do app apenas uma vez por sessão.

    A troca de páginas no Streamlit reexecuta o arquivo inteiro. Antes, a cada clique
    no menu, o app refazia validações de banco/DDL e configuração. Agora isso roda
    apenas na primeira abertura da sessão, preservando a estabilidade da v11.9 e
    deixando a transição entre telas muito mais leve.
    """
    if not st.session_state.get('_sb_app_inicializado_v120'):
        restaurar_estado_cloud_se_necessario()
        init_db()
        garantir_config_padrao()
        st.session_state['_sb_app_inicializado_v120'] = True
    # Não sincroniza estado durante a renderização no Cloud; isso evita instabilidade de frontend.
    # A sincronização é feita explicitamente após salvar/importar dados.
    if not CLOUD_MODE:
        sincronizar_estado_cloud()
    # No Streamlit Cloud evitamos iniciar threads/rotinas automáticas dentro do app,
    # pois elas podem concorrer com reruns da interface. Use os botões de atualização
    # ou um job externo. Localmente, mantém a rotina automática.
    if not CLOUD_MODE:
        iniciar_scheduler()


inicializar_app_leve()
# IMPORTANTE: não executa atualização SQL na abertura da tela.
# Isso evita tela escura/travada quando o PostgreSQL demora para responder.
# A rotina automática continua rodando em segundo plano às 04:00, e a atualização manual
# fica disponível no menu Importar compras > Atualização automática/cache.

render_sb_header()
mostrar_status_conexao()
aviso_streamlit_cloud()
try:
    st.sidebar.image(str(LOGO_BRANCO), use_container_width=True)
except Exception:
    st.sidebar.markdown('### SB Farma')
st.sidebar.markdown('---')
st.sidebar.markdown('### Usuário responsável')
st.sidebar.text_input('Quem está operando?', value=st.session_state.get('usuario_operacao', 'Paulo'), key='usuario_operacao')
st.sidebar.caption('Esse nome será gravado no lançamento, alteração e exclusão.')
st.sidebar.markdown('---')
menu = st.sidebar.radio(
    'Menu',
    [
        'Início',
        'Painel Executivo',
        'Negociações',
        'Importar dados',
        'Apuração',
        'Extrato Financeiro',
        'Parâmetros Financeiros',
        'Relatórios',
        'Auditoria',
        'Performance',
        'Base de dados',
    ]
)
reset_render_stability_state(menu)
st.sidebar.markdown('---')
st.sidebar.caption('Versão 14.8 Cloud Estável')
# Perfil do usuário: use key para evitar erro de modificar session_state após criar widget.
perfis_operacao = ['Administrador','Compras','Diretoria','Financeiro','Auditoria']
perfil_atual = st.session_state.get('perfil_operacao', 'Administrador')
if perfil_atual not in perfis_operacao:
    perfil_atual = 'Administrador'
st.sidebar.selectbox('Perfil', perfis_operacao, index=perfis_operacao.index(perfil_atual), key='perfil_operacao')
if st.sidebar.button('🔄 Recarregar dados da tela', help='Limpa apenas o cache visual. Use após cadastrar/alterar dados e desejar atualizar os resultados imediatamente.'):
    limpar_cache_telas()
    safe_rerun()

# Medição técnica da tela atual. Não altera regra de negócio nem renderização.
_sb_tela_inicio = time_module.perf_counter()

if menu == 'Início':
    st.subheader('SB Farma | Conta Corrente de Negociações')
    st.success('Aplicação carregada com sucesso no Streamlit Cloud.')
    st.info('Use o menu lateral para acessar Painel Executivo, Negociações, Importar dados, Apuração e demais módulos.')
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric('Status do app', 'Online')
    with c2:
        st.metric('Banco local', 'Pronto')
    with c3:
        st.metric('Modo', 'Cloud' if CLOUD_MODE else 'Local')
    st.caption('Para atualizar entradas/vendas, acesse Importar dados ou Base de dados. Nenhuma carga pesada é executada automaticamente na abertura.')

elif menu == 'Negociações':
    st.subheader('Cadastrar negociação acordada')
    st.info('Escolha primeiro se a negociação será por Fabricante ou Distribuidor. Em seguida, selecione um ou vários nomes existentes na base de entrada.')

    tipo = st.selectbox('Apurar por', ['Fabricante', 'Distribuidor'], key='tipo_cadastro')
    opcoes = get_opcoes_cadastro(tipo)

    # Campo obrigatório logo abaixo da seleção acima: permite escolher um ou vários fabricantes/distribuidores.
    st.markdown(f'### Escolher {tipo}')
    col_sel1, col_sel2 = st.columns([3, 1])
    with col_sel2:
        selecionar_todos = st.checkbox(f'Selecionar todos', key=f'sel_todos_{tipo}') if opcoes else False
    with col_sel1:
        if opcoes:
            nomes = st.multiselect(
                f'Selecione um ou mais {tipo.lower()}s',
                options=opcoes,
                default=opcoes if selecionar_todos else [],
                placeholder=f'Digite para pesquisar e selecione um ou mais {tipo.lower()}s',
                key=f'nomes_multiselect_{tipo}'
            )
        else:
            nomes = []
            st.multiselect(
                f'Selecione um ou mais {tipo.lower()}s',
                options=[],
                placeholder='Importe a base de entrada para carregar esta lista',
                key=f'nomes_multiselect_vazio_{tipo}'
            )
    st.caption(f'{len(nomes)} selecionado(s).')

    if not opcoes:
        st.warning('Nenhum registro encontrado na base de compras. Primeiro importe o Excel/CSV exportado do script de entrada ou execute o SQL direto no banco.')

    # v11.7: esta flag fica FORA do formulário para a tela reagir imediatamente.
    # Dentro de st.form o Streamlit só atualiza os widgets no submit, por isso o usuário
    # precisava salvar para a grade de produtos aparecer. Agora a grade aparece na hora,
    # mas a negociação continua sendo gravada apenas uma vez no botão final.
    usar_produtos_nova_flag = st.checkbox(
        'Lançar / controlar negociação por produto',
        key='usar_produtos_nova_flag',
        help='Marque para abrir a grade de produtos sem salvar a negociação. O salvamento continua único, junto com o cabeçalho.'
    )

    with st.form('form_neg'):
        tipos_negociacao_disponiveis = listar_tipos_negociacao_ativos()
        tipo_negociacao = st.selectbox('Tipo de negociação', tipos_negociacao_disponiveis)
        param_tipo_negociacao = obter_parametro_tipo_negociacao(tipo_negociacao)
        negociacao_sem_apuracao = not bool(param_tipo_negociacao.get('faz_apuracao', 0))
        if negociacao_sem_apuracao:
            st.info('Este tipo não depende de compras/vendas. Ao salvar, o sistema cria uma negociação normal e gera automaticamente um crédito a receber no extrato financeiro.')
        tipo_investimento_geral = st.radio('Tipo de investimento da negociação', ['Sell In (Compra)', 'Sell Out (Venda)', 'Sell In + Sell Out'], horizontal=True, index=1, help='Sell In apura sobre compras/entradas. Sell Out apura sobre vendas/saídas. Sell In + Sell Out calcula os dois separadamente.')
        col1, col2, col3 = st.columns(3)
        with col1:
            percentual = st.number_input('% de investimento sobre a compra', min_value=0.0, max_value=100.0, step=0.1, format='%.2f')
            status = st.selectbox('Status', ['Ativo', 'Inativo', 'Em negociação', 'Aprovada', 'Finalizada', 'Cancelada'])
        with col2:
            meta_label = 'Valor negociado / crédito a receber (R$)' if negociacao_sem_apuracao else 'Meta de compra negociada (R$)'
            meta_compra = st.number_input(meta_label, min_value=0.0, step=1000.0, format='%.2f')
            st.caption('Se deixar a meta zerada, o investimento será calculado pelo % do acordo sobre a compra realizada.')
            data_inicio = st.date_input('Início da validade', value=date.today())
        with col3:
            st.text_input('Código da negociação', value='Gerado automaticamente ao salvar', disabled=True)
            data_fim = st.date_input('Fim da validade', value=date.today())
        colb1, colb2 = st.columns(2)
        with colb1:
            bonificacao = st.number_input('Bonificação negociada (unidades)', min_value=0.0, step=1.0, format='%.0f')
        with colb2:
            verba_label = 'Valor da negociação financeira (R$)' if negociacao_sem_apuracao else 'Verba comercial / Sell-out (R$)'
            verba_comercial = st.number_input(verba_label, min_value=0.0, step=100.0, format='%.2f')
        faixas = []
        if tipo_negociacao in ['Faixa de meta', 'Híbrida']:
            st.markdown('### Faixas de meta')
            st.caption('Cadastre pelo menos 3 opções. Regra: faixa progressiva NÃO cumulativa. O app aplica somente a maior faixa atingida e gera apenas 1 crédito para a negociação.')
            for i in range(1, 4):
                cfa1, cfa2, cfa3, cfa4 = st.columns([1.2, 1, 1, 1.5])
                with cfa1:
                    meta_f = st.number_input(f'Meta {i} (R$)', min_value=0.0, step=100.0, format='%.2f', key=f'nova_meta_{i}')
                with cfa2:
                    perc_f = st.number_input(f'Benefício {i} (%)', min_value=0.0, max_value=100.0, step=0.1, format='%.2f', key=f'nova_perc_{i}')
                with cfa3:
                    bon_f = st.number_input(f'Bonif. {i}', min_value=0.0, step=1.0, format='%.0f', key=f'nova_bon_{i}')
                with cfa4:
                    obs_f = st.text_input(f'Obs. faixa {i}', key=f'nova_obs_faixa_{i}')
                faixas.append({'meta_valor': meta_f, 'percentual': perc_f, 'bonificacao': bon_f, 'verba_comercial': 0, 'observacao': obs_f})
        st.markdown('### Produtos da Negociação')
        st.caption('Cadastre aqui os produtos/SKUs que fazem parte do acordo. Você pode selecionar da base, digitar manualmente ou importar Excel/CSV. Colunas aceitas: Produto/Embalagem, EAN, Critério de Apuração, Meta de Compra, Meta de Venda Qtd, Meta de Venda Valor, Valor por Unidade Compra, Valor por Unidade Venda, Percentual Compra, Percentual Venda, Observação.')
        usar_produtos = bool(usar_produtos_nova_flag)
        produtos_negociados = []
        if usar_produtos:
            st.success('Controle por produto ativado. Preencha as metas, percentuais, valores unitários e observações de cada produto abaixo. A negociação será salva uma única vez.')
            modo_produto = st.radio('Forma de lançamento dos produtos', ['Selecionar da base', 'Digitar manualmente', 'Importar Excel/CSV'], horizontal=True, key='modo_produto_novo')
            if modo_produto == 'Selecionar da base':
                produtos_base = get_produtos_por_nome(tipo, nomes)
                op_prod = [f"{r['produto']} | {r.get('ean','')} | {r.get('codigo_interno','')}" for _, r in produtos_base.head(2000).iterrows()]
                escolhidos_prod = st.multiselect('Selecione os produtos da negociação', options=op_prod, key='produtos_negociados_novo')
                for i, item in enumerate(escolhidos_prod, start=1):
                    partes_item = item.split(' | ')
                    nome_prod = partes_item[0] if len(partes_item) > 0 else item
                    ean_prod = partes_item[1] if len(partes_item) > 1 else ''
                    cod_prod = partes_item[2] if len(partes_item) > 2 else ''
                    st.markdown(f'**{i}. {nome_prod}**')
                    cp0, cp1, cp2, cp3 = st.columns([1.1, 1, 1, 1])
                    with cp0:
                        tipo_inv_p = st.selectbox(f'Critério de apuração {i}', ['Sell In (Compra)', 'Sell Out (Venda)', 'Sell In + Sell Out'], index=['Sell In (Compra)', 'Sell Out (Venda)', 'Sell In + Sell Out'].index(tipo_investimento_geral), key=f'prod_tipo_inv_{i}')
                    with cp1:
                        meta_compra_p = st.number_input(f'Meta de Compra (Sell In) {i}', min_value=0.0, step=1.0, format='%.0f', key=f'prod_meta_si_{i}')
                    with cp2:
                        meta_venda_p = st.number_input(f'Meta de Venda Qtd (Sell Out) {i}', min_value=0.0, step=1.0, format='%.0f', key=f'prod_meta_so_{i}')
                    with cp3:
                        meta_venda_valor_p = st.number_input(f'Meta de Venda Valor (R$) {i}', min_value=0.0, step=100.0, format='%.2f', key=f'prod_meta_so_valor_{i}')
                    obs_prod_p = st.text_input(f'Observações do Produto {i}', key=f'prod_obs_{i}')
                    cp4, cp5, cp6, cp7 = st.columns([1, 1, 1, 1])
                    with cp4:
                        valor_si_p = st.number_input(f'Valor por Unidade (Compra) {i}', min_value=0.0, step=0.10, format='%.2f', key=f'prod_valor_si_{i}')
                    with cp5:
                        valor_so_p = st.number_input(f'Valor por Unidade (Venda) {i}', min_value=0.0, step=0.10, format='%.2f', key=f'prod_valor_so_{i}')
                    with cp6:
                        perc_si_p = st.number_input(f'Percentual sobre Compra (%) {i}', min_value=0.0, max_value=100.0, step=0.1, format='%.2f', key=f'prod_perc_si_{i}')
                    with cp7:
                        perc_so_p = st.number_input(f'Percentual sobre Venda (%) {i}', min_value=0.0, max_value=100.0, step=0.1, format='%.2f', key=f'prod_perc_so_{i}')
                    produtos_negociados.append({'produto': nome_prod, 'ean': ean_prod, 'codigo_interno': cod_prod, 'tipo_investimento': tipo_inv_p, 'meta_compra_qtd': meta_compra_p, 'meta_venda_qtd': meta_venda_p, 'meta_venda_valor': meta_venda_valor_p, 'valor_unitario_sellin': valor_si_p, 'valor_unitario_sellout': valor_so_p, 'percentual_sellin': perc_si_p, 'percentual_sellout': perc_so_p, 'observacao': obs_prod_p})
            elif modo_produto == 'Digitar manualmente':
                qtd_linhas_nova = st.number_input('Quantidade de Produtos na Negociação', min_value=1, max_value=100, step=1, value=3, key='qtd_prod_manual_nova')
                for i in range(1, int(qtd_linhas_nova) + 1):
                    st.markdown(f'**Produto negociado {i}**')
                    cm0, cm1, cm1b, cm2, cm3 = st.columns([2.2, 1.0, 1.0, 1.1, 1.1])
                    with cm0:
                        nome_prod = st.text_input(f'Produto negociado / Embalagem {i}', key=f'manual_prod_nome_{i}')
                    with cm1:
                        ean_prod = st.text_input(f'EAN {i}', key=f'manual_prod_ean_{i}')
                    with cm1b:
                        cod_prod = st.text_input(f'Código interno {i}', key=f'manual_prod_cod_{i}')
                    with cm2:
                        tipo_inv_p = st.selectbox(f'Critério de apuração {i}', ['Sell In (Compra)', 'Sell Out (Venda)', 'Sell In + Sell Out'], index=['Sell In (Compra)', 'Sell Out (Venda)', 'Sell In + Sell Out'].index(tipo_investimento_geral), key=f'manual_prod_tipo_{i}')
                    with cm3:
                        obs_prod_p = st.text_input(f'Observações do Produto {i}', key=f'manual_prod_obs_{i}')
                    cm4, cm5, cm5b, cm6, cm7, cm8, cm9 = st.columns([1, 1, 1, 1, 1, 1, 1])
                    with cm4:
                        meta_compra_p = st.number_input(f'Meta de Compra (Sell In) {i}', min_value=0.0, step=1.0, format='%.0f', key=f'manual_prod_meta_si_{i}')
                    with cm5:
                        meta_venda_p = st.number_input(f'Meta de Venda Qtd (Sell Out) {i}', min_value=0.0, step=1.0, format='%.0f', key=f'manual_prod_meta_so_{i}')
                    with cm5b:
                        meta_venda_valor_p = st.number_input(f'Meta de Venda Valor (R$) {i}', min_value=0.0, step=100.0, format='%.2f', key=f'manual_prod_meta_so_valor_{i}')
                    with cm6:
                        valor_si_p = st.number_input(f'Valor por Unidade (Compra) {i}', min_value=0.0, step=0.10, format='%.2f', key=f'manual_prod_valor_si_{i}')
                    with cm7:
                        valor_so_p = st.number_input(f'Valor por Unidade (Venda) {i}', min_value=0.0, step=0.10, format='%.2f', key=f'manual_prod_valor_so_{i}')
                    with cm8:
                        perc_si_p = st.number_input(f'Percentual sobre Compra (%) {i}', min_value=0.0, max_value=100.0, step=0.1, format='%.2f', key=f'manual_prod_perc_si_{i}')
                    with cm9:
                        perc_so_p = st.number_input(f'Percentual sobre Venda (%) {i}', min_value=0.0, max_value=100.0, step=0.1, format='%.2f', key=f'manual_prod_perc_so_{i}')
                    if str(nome_prod).strip() or str(ean_prod).strip():
                        produtos_negociados.append({'produto': nome_prod, 'ean': ean_prod, 'codigo_interno': cod_prod, 'tipo_investimento': tipo_inv_p, 'meta_compra_qtd': meta_compra_p, 'meta_venda_qtd': meta_venda_p, 'meta_venda_valor': meta_venda_valor_p, 'valor_unitario_sellin': valor_si_p, 'valor_unitario_sellout': valor_so_p, 'percentual_sellin': perc_si_p, 'percentual_sellout': perc_so_p, 'observacao': obs_prod_p})
            else:
                arq_prod = st.file_uploader('Importar produtos da negociação (Excel/CSV)', type=['xlsx', 'xls', 'csv'], key='upload_produtos_nova')
                if arq_prod is not None:
                    try:
                        df_prod_import = ler_produtos_negociacao_upload(arq_prod)
                        st.caption(f'{len(df_prod_import)} produto(s) reconhecido(s) no arquivo.')
                        st.dataframe(df_prod_import.head(30), use_container_width=True)
                        produtos_negociados = registros_produtos_de_df(df_prod_import)
                    except Exception as e:
                        st.error(f'Erro ao ler produtos: {e}')
        observacao = st.text_area('Observação / condição comercial')
        submitted = st.form_submit_button('Salvar negociação')
        if submitted:
            nomes_final = [str(n).strip() for n in nomes if str(n).strip()]
            if not nomes_final:
                st.error(f'Selecione pelo menos um {tipo.lower()} no campo acima.')
            elif data_fim < data_inicio:
                st.error('A data final não pode ser menor que a data inicial.')
            elif usar_produtos and not produtos_negociados:
                st.error('Você marcou controle por produto, mas nenhum produto foi informado. Inclua pelo menos um produto ou desmarque a opção.')
            elif negociacao_sem_apuracao and float((verba_comercial or 0) or (meta_compra or 0)) <= 0:
                st.error('Informe o valor financeiro da negociação para gerar o crédito a receber no extrato.')
            else:
                # Evita duplicidade de produto na mesma negociação antes de gravar.
                vistos_prod = set()
                produtos_limpos = []
                duplicados = []
                for p in produtos_negociados:
                    chave = normalizar_codigo(p.get('codigo_interno')) or normalizar_ean(p.get('ean')) or normalizar_texto_chave(p.get('produto'))
                    if not chave:
                        continue
                    if chave in vistos_prod:
                        duplicados.append(str(p.get('produto') or p.get('ean') or p.get('codigo_interno')))
                        continue
                    vistos_prod.add(chave)
                    produtos_limpos.append(p)
                if duplicados:
                    st.error('Existem produtos duplicados na negociação: ' + ', '.join(duplicados[:10]))
                else:
                    codigos = save_negociacoes_multiplas(tipo, nomes_final, percentual, meta_compra, data_inicio, data_fim, status, observacao, tipo_negociacao, tipo_investimento_geral, bonificacao, verba_comercial, faixas, produtos_limpos, usuario=usuario_atual())
                    st.success(f'{len(codigos)} negociação(ões) salva(s) com sucesso. Código(s): {", ".join(codigos[:5])}{"..." if len(codigos) > 5 else ""}')

    st.divider()
    st.subheader('Editar negociação já lançada')
    neg_edit = load_table('negociacoes')
    if neg_edit.empty:
        st.info('Ainda não existe negociação lançada para editar.')
    else:
        neg_edit = neg_edit.sort_values('id', ascending=False)
        opcoes_edit = [f"{r.get('codigo_negociacao','')} | {r.get('nome','')} | {r.get('tipo_negociacao','')}" for _, r in neg_edit.iterrows()]
        escolha = st.selectbox('Selecione a negociação para editar', opcoes_edit)
        idx = opcoes_edit.index(escolha)
        row = neg_edit.iloc[idx]
        st.caption(f"Código fixo: {row.get('codigo_negociacao','')} | Lançada por: {row.get('criado_por','')} | Criada em: {row.get('criado_em','')} | Última alteração por: {row.get('atualizado_por','')} | Atualizada em: {row.get('atualizado_em','')}")
        faixas_existentes = carregar_faixas(int(row['id']))
        produtos_existentes = carregar_produtos_negociacao(int(row['id']))
        with st.form('form_edit_neg'):
            tipos_negociacao_edit = listar_tipos_negociacao_ativos()
            tipo_atual_edit = row.get('tipo_negociacao','Desconto percentual') if row.get('tipo_negociacao','Desconto percentual') in tipos_negociacao_edit else 'Desconto percentual'
            tipo_neg_edit = st.selectbox('Tipo de negociação', tipos_negociacao_edit, index=tipos_negociacao_edit.index(tipo_atual_edit), key='edit_tipo_neg')
            tipo_inv_opts_edit = ['Sell In (Compra)', 'Sell Out (Venda)', 'Sell In + Sell Out']
            tipo_inv_atual_edit = str(row.get('tipo_investimento','Sell Out') or 'Sell Out')
            if tipo_inv_atual_edit == 'Sell In': tipo_inv_atual_edit = 'Sell In (Compra)'
            if tipo_inv_atual_edit == 'Sell Out': tipo_inv_atual_edit = 'Sell Out (Venda)'
            tipo_investimento_edit = st.radio('Tipo de investimento da negociação', tipo_inv_opts_edit, horizontal=True, index=tipo_inv_opts_edit.index(tipo_inv_atual_edit) if tipo_inv_atual_edit in tipo_inv_opts_edit else 1, help='Define se o acordo será apurado por compras, vendas ou ambos.')
            e1, e2, e3 = st.columns(3)
            with e1:
                percentual_e = st.number_input('% investimento', min_value=0.0, max_value=100.0, step=0.1, format='%.2f', value=float(row.get('percentual',0) or 0), key='edit_perc')
                status_e = st.selectbox('Status', ['Ativo', 'Inativo', 'Em negociação', 'Aprovada', 'Finalizada', 'Cancelada'], index=['Ativo', 'Inativo', 'Em negociação', 'Aprovada', 'Finalizada', 'Cancelada'].index(row.get('status','Ativo') if row.get('status','Ativo') in ['Ativo', 'Inativo', 'Em negociação', 'Aprovada', 'Finalizada', 'Cancelada'] else 'Ativo'), key='edit_status')
            with e2:
                meta_e = st.number_input('Meta de compra (R$)', min_value=0.0, step=1000.0, format='%.2f', value=float(row.get('meta_compra',0) or 0), key='edit_meta')
                data_inicio_e = st.date_input('Início validade', value=pd.to_datetime(row.get('data_inicio')).date(), key='edit_ini')
            with e3:
                bon_e = st.number_input('Bonificação', min_value=0.0, step=1.0, format='%.0f', value=float(row.get('bonificacao',0) or 0), key='edit_bon')
                data_fim_e = st.date_input('Fim validade', value=pd.to_datetime(row.get('data_fim')).date(), key='edit_fim')
            verba_e = st.number_input('Verba comercial / Sell-out (R$)', min_value=0.0, step=100.0, format='%.2f', value=float(row.get('verba_comercial',0) or 0), key='edit_verba')
            faixas_edit = []
            if tipo_neg_edit in ['Faixa de meta', 'Híbrida']:
                st.markdown('### Faixas de meta')
                for i in range(1, 4):
                    frow = faixas_existentes.iloc[i-1] if not faixas_existentes.empty and len(faixas_existentes) >= i else {}
                    cfa1, cfa2, cfa3, cfa4 = st.columns([1.2, 1, 1, 1.5])
                    with cfa1:
                        meta_f = st.number_input(f'Meta {i} (R$)', min_value=0.0, step=100.0, format='%.2f', value=float(frow.get('meta_valor',0) or 0), key=f'edit_meta_f_{i}')
                    with cfa2:
                        perc_f = st.number_input(f'Benefício {i} (%)', min_value=0.0, max_value=100.0, step=0.1, format='%.2f', value=float(frow.get('percentual',0) or 0), key=f'edit_perc_f_{i}')
                    with cfa3:
                        bon_f = st.number_input(f'Bonif. {i}', min_value=0.0, step=1.0, format='%.0f', value=float(frow.get('bonificacao',0) or 0), key=f'edit_bon_f_{i}')
                    with cfa4:
                        obs_f = st.text_input(f'Obs. faixa {i}', value=str(frow.get('observacao','') or ''), key=f'edit_obs_f_{i}')
                    faixas_edit.append({'meta_valor': meta_f, 'percentual': perc_f, 'bonificacao': bon_f, 'verba_comercial': 0, 'observacao': obs_f})
            st.markdown('### Produtos da Negociação')
            produtos_edit = []
            qtd_linhas_prod = st.number_input('Quantidade de Produtos na Negociação', min_value=0, max_value=50, step=1, value=max(0, min(10, len(produtos_existentes) if not produtos_existentes.empty else 0)), key='edit_qtd_produtos')
            produtos_base_edit = get_produtos_por_nome(str(row.get('tipo','Fabricante')), [str(row.get('nome',''))])
            op_prod_edit = [f"{r['produto']} | {r.get('ean','')} | {r.get('codigo_interno','')}" for _, r in produtos_base_edit.head(1000).iterrows()]
            for i in range(1, int(qtd_linhas_prod) + 1):
                prow = produtos_existentes.iloc[i-1] if not produtos_existentes.empty and len(produtos_existentes) >= i else {}
                valor_padrao_prod = (str(prow.get('produto','') or '').upper().strip() + ' | ' + str(prow.get('ean','') or '').strip() + ' | ' + str(prow.get('codigo_interno','') or '').strip()).strip(' |')
                opts = op_prod_edit.copy()
                if valor_padrao_prod and valor_padrao_prod not in opts:
                    opts = [valor_padrao_prod] + opts
                ce0, ce1, ce2, ce3, ce3b = st.columns([2.0, 1.0, 1.0, 1.0, 1.0])
                with ce0:
                    item_prod = st.selectbox(f'Produto negociado {i}', options=opts if opts else [valor_padrao_prod], index=0, key=f'edit_prod_item_{i}')
                partes_prod = item_prod.split(' | ')
                nome_prod = partes_prod[0] if len(partes_prod) > 0 else item_prod
                ean_prod = partes_prod[1] if len(partes_prod) > 1 else ''
                cod_prod = partes_prod[2] if len(partes_prod) > 2 else str(prow.get('codigo_interno','') or '')
                tipo_atual = str(prow.get('tipo_investimento','Sell Out') or 'Sell Out')
                if tipo_atual == 'Sell In': tipo_atual = 'Sell In (Compra)'
                if tipo_atual == 'Sell Out': tipo_atual = 'Sell Out (Venda)'
                tipo_opts = ['Sell In (Compra)', 'Sell Out (Venda)', 'Sell In + Sell Out']
                with ce1:
                    tipo_inv_p = st.selectbox(f'Critério de apuração {i}', tipo_opts, index=tipo_opts.index(tipo_atual) if tipo_atual in tipo_opts else 1, key=f'edit_prod_tipo_{i}')
                with ce2:
                    meta_compra_p = st.number_input(f'Meta de Compra (Sell In) {i}', min_value=0.0, step=1.0, format='%.0f', value=float(prow.get('meta_compra_qtd',0) or 0), key=f'edit_prod_meta_si_{i}')
                with ce3:
                    meta_venda_p = st.number_input(f'Meta de Venda Qtd (Sell Out) {i}', min_value=0.0, step=1.0, format='%.0f', value=float(prow.get('meta_venda_qtd', prow.get('meta_qtd',0)) or 0), key=f'edit_prod_meta_so_{i}')
                with ce3b:
                    meta_venda_valor_p = st.number_input(f'Meta de Venda Valor (R$) {i}', min_value=0.0, step=100.0, format='%.2f', value=float(prow.get('meta_venda_valor',0) or 0), key=f'edit_prod_meta_so_valor_{i}')
                ce4, ce5, ce6, ce7, ce8 = st.columns([1, 1, 1, 1, 1.6])
                with ce4:
                    valor_si_p = st.number_input(f'Valor por Unidade (Compra) {i}', min_value=0.0, step=0.10, format='%.2f', value=float(prow.get('valor_unitario_sellin',0) or 0), key=f'edit_prod_valor_si_{i}')
                with ce5:
                    valor_so_p = st.number_input(f'Valor por Unidade (Venda) {i}', min_value=0.0, step=0.10, format='%.2f', value=float(prow.get('valor_unitario_sellout', prow.get('valor_unitario',0)) or 0), key=f'edit_prod_valor_so_{i}')
                with ce6:
                    perc_si_p = st.number_input(f'Percentual sobre Compra (%) {i}', min_value=0.0, max_value=100.0, step=0.1, format='%.2f', value=float(prow.get('percentual_sellin',0) or 0), key=f'edit_prod_perc_si_{i}')
                with ce7:
                    perc_so_p = st.number_input(f'Percentual sobre Venda (%) {i}', min_value=0.0, max_value=100.0, step=0.1, format='%.2f', value=float(prow.get('percentual_sellout', prow.get('percentual',0)) or 0), key=f'edit_prod_perc_so_{i}')
                with ce8:
                    obs_prod_p = st.text_input(f'Observações do Produto {i}', value=str(prow.get('observacao','') or ''), key=f'edit_prod_obs_{i}')
                produtos_edit.append({'produto': nome_prod, 'ean': ean_prod, 'codigo_interno': cod_prod, 'tipo_investimento': tipo_inv_p, 'meta_compra_qtd': meta_compra_p, 'meta_venda_qtd': meta_venda_p, 'meta_venda_valor': meta_venda_valor_p, 'valor_unitario_sellin': valor_si_p, 'valor_unitario_sellout': valor_so_p, 'percentual_sellin': perc_si_p, 'percentual_sellout': perc_so_p, 'observacao': obs_prod_p})
            observacao_e = st.text_area('Observação / condição comercial', value=str(row.get('observacao','') or ''), key='edit_obs')
            salvar_edit = st.form_submit_button('Salvar alterações')
            if salvar_edit:
                dados = {'percentual': percentual_e, 'meta_compra': meta_e, 'data_inicio': data_inicio_e, 'data_fim': data_fim_e, 'status': status_e, 'observacao': observacao_e, 'tipo_negociacao': tipo_neg_edit, 'tipo_investimento': tipo_investimento_edit, 'bonificacao': bon_e, 'verba_comercial': verba_e}
                atualizar_negociacao(int(row['id']), dados, faixas_edit, produtos_edit, usuario=usuario_atual())
                st.success('Negociação atualizada com histórico de alterações.')
                safe_rerun()
        st.divider()
        st.markdown('### Excluir negociação')
        st.warning('A exclusão remove a negociação da apuração e das telas principais, mas mantém o histórico para auditoria.')
        confirmar_exclusao = st.checkbox(f"Confirmo a exclusão da negociação {row.get('codigo_negociacao','')}", key=f"confirma_excluir_{int(row['id'])}")
        motivo_exclusao = st.text_input('Motivo da exclusão', value='Lançamento incorreto / cancelado', key=f"motivo_excluir_{int(row['id'])}")
        if st.button('Excluir negociação selecionada', type='secondary', disabled=not confirmar_exclusao, key=f"btn_excluir_{int(row['id'])}"):
            excluir_negociacao(int(row['id']), usuario=usuario_atual(), motivo=motivo_exclusao)
            st.success('Negociação excluída com sucesso. Ela não será mais considerada na apuração.')
            safe_rerun()

        with connect() as con:
            hist = pd.read_sql_query('SELECT data_hora, usuario, campo, valor_anterior, valor_novo, observacao FROM historico_negociacoes WHERE negociacao_id=? ORDER BY id DESC LIMIT 50', con, params=(int(row['id']),))
        if not hist.empty:
            st.markdown('### Histórico de alterações')
            st.dataframe(hist, use_container_width=True)

elif menu == 'Importar dados':
    st.subheader('Importar base de compras e vendas')
    st.info('Este app está preparado para o script ENTRADAS_SB.sql. Fabricante = laboratorio, Distribuidor = fornecedor, Data da apuração = data_emissao da nota, Compra Base = coluna Compra do sistema. O SQL padrão usa inf.custo dividido pela quantidade por embalagem multiplicado pela quantidade, para bater com o relatório Análise de Notas Fiscais de Entrada.')

    aba_import = st.radio('Escolha a rotina', ['Importar compras Excel/CSV', 'Executar entradas SQL', 'Importar vendas SQL/Excel', 'Atualização automática/cache', 'Ver scripts SQL'], horizontal=True, key='importacao_secao')

    if aba_import == 'Importar compras Excel/CSV':
        file = st.file_uploader('Arquivo Excel ou CSV exportado do script de entrada', type=['xlsx', 'xls', 'csv'])
        if file:
            if file.name.lower().endswith('.csv'):
                df = pd.read_csv(file, sep=None, engine='python')
            else:
                df = pd.read_excel(file)
            df_preparado = preparar_entrada_sql(df)
            st.write('Prévia reconhecida pelo app:')
            st.dataframe(df_preparado.head(30), use_container_width=True)
            if st.button('Importar para o banco', key='btn_import_arquivo'):
                try:
                    qtd = insert_compras(df, file.name)
                    st.success(f'{qtd} registros importados/atualizados com sucesso.')
                    sincronizar_estado_cloud(force=True)
                    finalizar_atualizacao_cloud(f'{qtd} registros importados/atualizados com sucesso.')
                    safe_rerun()
                except Exception as e:
                    st.error(f'Erro ao importar: {e}')

    if aba_import == 'Executar entradas SQL':
        st.warning('Use esta opção somente no computador/servidor que tenha acesso ao banco do sistema.')
        c1, c2, c3 = st.columns(3)
        with c1:
            host = st.text_input('Host', value=get_config('auto_host', DEFAULT_DB_CONFIG['host']))
            database = st.text_input('Banco de dados', value=get_config('auto_database', DEFAULT_DB_CONFIG['database']))
        with c2:
            port = st.text_input('Porta', value=get_config('auto_port', DEFAULT_DB_CONFIG['port']))
            user = st.text_input('Usuário', value=get_config('auto_user', DEFAULT_DB_CONFIG['user']))
        with c3:
            password = st.text_input('Senha', value=get_config('auto_password', DEFAULT_DB_CONFIG['password']), type='password')
        col_sql_save, col_sql_test, col_sql_exec = st.columns([1, 1, 2])
        with col_sql_save:
            if st.button('💾 Salvar dados do banco', key='btn_salvar_sql_config'):
                erros = validar_config_banco(host, port, database, user, password)
                if erros:
                    st.error('Corrija a configuração antes de salvar:\n' + '\n'.join(f'- {e}' for e in erros))
                else:
                    salvar_configuracao_sql(host, port, database, user, password, get_config('auto_enabled', '0') == '1', SCHEDULE_HOUR, SCHEDULE_MINUTE)
                    st.success('Dados do banco salvos.')
        with col_sql_test:
            testar_entradas_sql = st.button('🔌 Testar conexão', key='btn_testar_sql_config')
        with col_sql_exec:
            executar_entradas_sql = st.button('Executar ENTRADAS_SB.sql e importar', key='btn_sql')
        if testar_entradas_sql:
            ok, msg = testar_conexao_postgres(host, port, database, user, password)
            if ok:
                st.success(msg)
            else:
                st.error('Falha ao conectar:\n' + msg)
        if executar_entradas_sql:
            try:
                erros = validar_config_banco(host, port, database, user, password)
                if erros:
                    st.error('Corrija a configuração antes de executar:\n' + '\n'.join(f'- {e}' for e in erros))
                else:
                    salvar_configuracao_sql(host, port, database, user, password, True, SCHEDULE_HOUR, SCHEDULE_MINUTE)
                    st.info('Executando importação de entradas. Aguarde até finalizar...')
                    qtd, df_sql = executar_script_postgres(host, port, database, user, password)
                    valor_total = float(get_config('last_import_valor', 0) or 0)
                    st.success(f'{qtd} registros importados/atualizados com sucesso. Compra base carregada: {money(valor_total)}')
                    sincronizar_estado_cloud(force=True)
                    finalizar_atualizacao_cloud(
                        f'Entradas importadas com sucesso: {qtd} registros.',
                        f'Compra base carregada: {money(valor_total)}'
                    )
                    st.info('Importação concluída. Os fabricantes/distribuidores já devem aparecer no cadastro e nas apurações.')
            except Exception as e:
                registrar_atualizacao('MANUAL_SQL', 'Erro', 0, str(e))
                st.error(f'Erro ao executar SQL: {e}')

    if aba_import == 'Importar vendas SQL/Excel':
        st.subheader('Importar vendas para apuração por produto')
        st.info('Use esta aba para carregar a venda realizada no período. A negociação por produto será apurada por Meta de Venda, usando EAN ou produto.')
        vendas_file = st.file_uploader('Arquivo de vendas Excel/CSV', type=['xlsx', 'xls', 'csv'], key='upload_vendas')
        if vendas_file is not None:
            try:
                if vendas_file.name.lower().endswith('.csv'):
                    df_vendas = pd.read_csv(vendas_file)
                else:
                    df_vendas = pd.read_excel(vendas_file)
                st.write('Prévia da venda normalizada')
                st.dataframe(preparar_venda_sql(df_vendas).head(30), use_container_width=True)
                if st.button('Importar vendas do arquivo', key='btn_import_vendas_arquivo'):
                    qtd = insert_vendas(df_vendas, vendas_file.name, substituir_origem=False)
                    st.success(f'{qtd} registros de venda importados com sucesso.')
                    sincronizar_estado_cloud(force=True)
                    finalizar_atualizacao_cloud(f'Vendas importadas com sucesso: {qtd} registros.')
                    safe_rerun()
            except Exception as e:
                st.error(f'Erro ao ler/importar vendas: {e}')

        st.markdown('---')
        st.warning('Use esta opção somente no computador/servidor que tenha acesso ao banco do sistema.')
        vc1, vc2, vc3 = st.columns(3)
        with vc1:
            host_v = st.text_input('Host vendas', value=get_config('auto_host', DEFAULT_DB_CONFIG['host']))
            database_v = st.text_input('Banco vendas', value=get_config('auto_database', DEFAULT_DB_CONFIG['database']))
        with vc2:
            port_v = st.text_input('Porta vendas', value=get_config('auto_port', DEFAULT_DB_CONFIG['port']))
            user_v = st.text_input('Usuário vendas', value=get_config('auto_user', DEFAULT_DB_CONFIG['user']))
        with vc3:
            password_v = st.text_input('Senha vendas', value=get_config('auto_password', DEFAULT_DB_CONFIG['password']), type='password')
        st.markdown('#### Período para carregar as vendas')
        colvdi, colvdf = st.columns(2)
        with colvdi:
            data_vendas_ini = st.date_input('Data inicial das vendas', value=date(date.today().year, date.today().month, 1), key='sql_vendas_ini')
        with colvdf:
            data_vendas_fim = st.date_input('Data final das vendas', value=date.today(), key='sql_vendas_fim')
        st.caption('Informe o período da negociação. Exemplo: Opella Julho = 01/07/2026 até 31/07/2026.')

        col_v_save, col_v_test, col_v_exec = st.columns([1, 1, 2])
        with col_v_save:
            if st.button('💾 Salvar dados do banco', key='btn_salvar_vendas_config'):
                erros = validar_config_banco(host_v, port_v, database_v, user_v, password_v)
                if erros:
                    st.error('Corrija a configuração antes de salvar:\n' + '\n'.join(f'- {e}' for e in erros))
                else:
                    salvar_configuracao_sql(host_v, port_v, database_v, user_v, password_v, get_config('auto_enabled', '0') == '1', SCHEDULE_HOUR, SCHEDULE_MINUTE)
                    st.success('Dados do banco salvos.')
        with col_v_test:
            testar_vendas_sql = st.button('🔌 Testar conexão', key='btn_testar_vendas_config')
        with col_v_exec:
            executar_vendas_sql = st.button('Executar VENDAS_SB.sql e importar', key='btn_sql_vendas')
        if testar_vendas_sql:
            ok, msg = testar_conexao_postgres(host_v, port_v, database_v, user_v, password_v)
            if ok:
                st.success(msg)
            else:
                st.error('Falha ao conectar:\n' + msg)
        if executar_vendas_sql:
            try:
                erros = validar_config_banco(host_v, port_v, database_v, user_v, password_v)
                if erros:
                    st.error('Corrija a configuração antes de executar:\n' + '\n'.join(f'- {e}' for e in erros))
                else:
                    salvar_configuracao_sql(host_v, port_v, database_v, user_v, password_v, True, SCHEDULE_HOUR, SCHEDULE_MINUTE)
                    st.info('Executando importação de vendas. Aguarde até finalizar...')
                    qtd_v, df_sql_v = executar_script_vendas_postgres(host_v, port_v, database_v, user_v, password_v, data_vendas_ini, data_vendas_fim)
                    st.success(f'{qtd_v} registros de vendas importados. Valor vendido: {money(get_config("last_vendas_valor", 0))}')
                    sincronizar_estado_cloud(force=True)
                    finalizar_atualizacao_cloud(
                        f'Vendas importadas com sucesso: {qtd_v} registros.',
                        f'Período: {data_vendas_ini} até {data_vendas_fim} | Valor vendido: {money(get_config("last_vendas_valor", 0))}'
                    )
                    st.caption(f'Período importado: {data_vendas_ini} até {data_vendas_fim}. Prévia removida no Cloud para evitar instabilidade de interface após atualização pesada.')
            except Exception as e:
                registrar_atualizacao('MANUAL_SQL_VENDAS', 'Erro', 0, str(e))
                st.error(f'Erro ao executar SQL de vendas: {e}')

        vendas_cache = carregar_cache_vendas()
        if not vendas_cache.empty:
            st.caption(f'Cache de vendas: {len(vendas_cache)} registros | Valor vendido: {money(vendas_cache["valor_venda"].sum())} | Arquivo: {CACHE_VENDAS_PARQUET}')

    if aba_import == 'Atualização automática/cache':
        st.subheader('Atualização automática de entradas e vendas')
        st.info(f'Modo Locaweb/Streamlit: cache e negociações ficam salvos em {DATA_DIR}. O banco SB Farma é usado apenas para leitura. Regra: às 04:00 carrega entradas e vendas de 01/01/2025 até hoje; durante o dia atualiza entradas e vendas do dia de hora em hora.')
        ultimo = ultima_atualizacao_cache()
        if ultimo:
            st.caption(f'Última atualização: {ultimo[0]} | Status: {ultimo[1]} | Registros: {ultimo[2]} | {ultimo[3]}')
        else:
            st.caption('Ainda não existe atualização automática/manual registrada.')
        st.caption(f'Arquivo de cache: {CACHE_PARQUET}')
        st.caption(f'Banco local do app: {DB_PATH}')

        with st.form('form_auto_cache'):
            habilitado = st.checkbox('Ativar atualização automática: 04:00 carga completa + hora em hora do dia', value=get_config('auto_enabled', '0') == '1')
            c1, c2, c3 = st.columns(3)
            with c1:
                host_cfg = st.text_input('Host do banco', value=get_config('auto_host', DEFAULT_DB_CONFIG['host']))
                database_cfg = st.text_input('Banco de dados', value=get_config('auto_database', DEFAULT_DB_CONFIG['database']))
            with c2:
                port_cfg = st.text_input('Porta', value=get_config('auto_port', DEFAULT_DB_CONFIG['port']))
                user_cfg = st.text_input('Usuário', value=get_config('auto_user', DEFAULT_DB_CONFIG['user']))
            with c3:
                password_cfg = st.text_input('Senha', value=get_config('auto_password', DEFAULT_DB_CONFIG['password']), type='password')
            salvar_cfg = st.form_submit_button('Salvar configuração')
            if salvar_cfg:
                salvar_configuracao_sql(host_cfg, port_cfg, database_cfg, user_cfg, password_cfg, habilitado, SCHEDULE_HOUR, SCHEDULE_MINUTE)
                st.success('Configuração salva. Em modo desenvolvimento, os dados só serão recarregados automaticamente se essa opção ficar ativada.')

        col_a, col_b = st.columns([1, 3])
        with col_a:
            if st.button('Atualizar entradas e vendas agora', type='primary'):
                try:
                    with st.spinner('Atualizando entradas e vendas de 01/01/2025 até hoje...'):
                        qtd_e, qtd_v, df_e, df_v = atualizar_bases_completa_ate_hoje(tipo='MANUAL_FULL')
                    st.success(f'Base completa atualizada: {qtd_e} entradas e {qtd_v} vendas. Compra: {money(get_config("last_import_valor", 0))} | Venda: {money(get_config("last_vendas_valor", 0))}')
                    finalizar_atualizacao_cloud(
                        f'Base completa atualizada: {qtd_e} entradas e {qtd_v} vendas.',
                        f'Compra: {money(get_config("last_import_valor", 0))} | Venda: {money(get_config("last_vendas_valor", 0))}'
                    )
                    st.caption('Prévia removida no Cloud para manter a interface estável após atualização pesada.')
                except Exception as e:
                    registrar_atualizacao('MANUAL_FULL', 'Erro', 0, str(e))
                    st.error(f'Erro ao atualizar cache: {e}')
        with col_b:
            st.caption('Use este botão quando precisar atualizar manualmente a base completa antes da próxima rotina automática.')
            if st.button('Atualizar somente o dia atual'):
                try:
                    with st.spinner('Atualizando entradas e vendas do dia atual...'):
                        qtd_e, qtd_v, _, _ = atualizar_bases_dia_atual(tipo='MANUAL_TODAY')
                    st.success(f'Dia atual atualizado: {qtd_e} entradas e {qtd_v} vendas.')
                    finalizar_atualizacao_cloud(f'Dia atual atualizado: {qtd_e} entradas e {qtd_v} vendas.')
                except Exception as e:
                    registrar_atualizacao('MANUAL_TODAY', 'Erro', 0, str(e))
                    st.error(f'Erro ao atualizar o dia atual: {e}')

        try:
            hist = load_table('atualizacoes_entrada').tail(20).sort_values('id', ascending=False)
            st.write('Histórico de atualizações')
            st.dataframe(hist, use_container_width=True)
        except Exception:
            pass

    if aba_import == 'Ver scripts SQL':
        if SQL_PATH.exists():
            sql_txt = SQL_PATH.read_text(encoding='utf-8')
            st.download_button('Baixar ENTRADAS_SB.sql', sql_txt.encode('utf-8'), file_name='ENTRADAS_SB.sql')
            st.code(sql_txt, language='sql')
        else:
            st.error('Arquivo ENTRADAS_SB.sql não encontrado na pasta do app.')
        st.markdown('---')
        if SQL_VENDAS_PATH.exists():
            sql_v_txt = SQL_VENDAS_PATH.read_text(encoding='utf-8')
            st.download_button('Baixar VENDAS_SB.sql', sql_v_txt.encode('utf-8'), file_name='VENDAS_SB.sql')
            st.code(sql_v_txt, language='sql')
        else:
            st.error('Arquivo VENDAS_SB.sql não encontrado na pasta do app.')

elif menu == 'Apuração':
    st.subheader('Apuração')
    st.info('As apurações já iniciam carregadas do cache. Nenhum cálculo pesado é executado ao abrir esta tela. Use Atualizar/Recalcular somente quando precisar buscar dados novos ou refazer os cálculos.')

    negociacoes_all = load_table('negociacoes')
    ap_cache, ap_prod_cache = carregar_apuracao_resumida()
    cache_gerado = obter_info_cache_apuracao()

    negociacoes = negociacoes_all.copy()
    opcoes_neg = ['Todas as negociações ativas — cache atual']
    mapa_neg = {}
    if not negociacoes.empty:
        neg_tmp = negociacoes.copy()
        neg_tmp['data_inicio_dt'] = pd.to_datetime(neg_tmp.get('data_inicio'), errors='coerce').dt.date
        neg_tmp['data_fim_dt'] = pd.to_datetime(neg_tmp.get('data_fim'), errors='coerce').dt.date
        if 'status' in neg_tmp.columns:
            neg_tmp = neg_tmp[neg_tmp['status'].astype(str).eq('Ativo')]
        for _, nrow in neg_tmp.sort_values(['data_inicio_dt', 'nome'], ascending=[False, True]).iterrows():
            label = f"{nrow.get('codigo_curto') or nrow.get('codigo_negociacao') or nrow.get('id')} | {nrow.get('nome','')} | {nrow.get('data_inicio_dt')} a {nrow.get('data_fim_dt')}"
            opcoes_neg.append(label)
            mapa_neg[label] = nrow

    escolha_neg = st.selectbox('Negociação / ação', opcoes_neg, index=0)
    neg_selecionada = mapa_neg.get(escolha_neg)
    modo_todas = neg_selecionada is None

    def _filtrar_cache_negociacao(df, neg):
        if df is None or df.empty or neg is None:
            return df.copy() if df is not None else pd.DataFrame()
        out = df.copy()
        candidatos = []
        for v in [neg.get('id'), neg.get('codigo_negociacao'), neg.get('codigo_curto')]:
            if v is not None and str(v).strip() not in ['', 'nan', 'None']:
                candidatos.append(str(v).strip())
        if not candidatos:
            return out.iloc[0:0].copy()
        mask = pd.Series(False, index=out.index)
        for col in ['negociacao_id', 'Negociação ID', 'id_negociacao', 'Código Negociação', 'Código Curto']:
            if col in out.columns:
                mask = mask | out[col].astype(str).str.strip().isin(candidatos)
        # fallback por nome da negociação/fabricante quando caches antigos não trazem ID
        nome = str(neg.get('nome','')).strip().upper()
        if nome:
            for col in ['Fabricante/Distribuidor', 'Fabricante', 'Fornecedor', 'nome', 'Nome']:
                if col in out.columns:
                    mask = mask | out[col].astype(str).str.upper().str.strip().eq(nome)
        return out[mask].copy()

    if neg_selecionada is not None:
        ap = _filtrar_cache_negociacao(ap_cache, neg_selecionada)
        ap_prod = _filtrar_cache_negociacao(ap_prod_cache, neg_selecionada)
        data_ini_sel = pd.to_datetime(neg_selecionada.get('data_inicio'), errors='coerce')
        data_fim_sel = pd.to_datetime(neg_selecionada.get('data_fim'), errors='coerce')
        periodo_txt = f"{data_ini_sel.date().strftime('%d/%m/%Y') if not pd.isna(data_ini_sel) else '-'} até {data_fim_sel.date().strftime('%d/%m/%Y') if not pd.isna(data_fim_sel) else '-'}"
        detalhe_periodo = 'Período da negociação selecionada. Dados exibidos a partir do cache de apuração.'
    else:
        ap = ap_cache.copy() if ap_cache is not None else pd.DataFrame()
        ap_prod = ap_prod_cache.copy() if ap_prod_cache is not None else pd.DataFrame()
        periodo_txt = 'Todas as negociações ativas'
        detalhe_periodo = 'Dados exibidos a partir do último cache de apuração gerado.'

    st.markdown(
        f"""
        <div class="metric-card" style="margin-bottom:12px;">
            <div class="metric-label">Regra da tela</div>
            <div class="metric-value" style="font-size:1.45rem;">{periodo_txt}</div>
            <div class="metric-label">{detalhe_periodo}</div>
        </div>
        """,
        unsafe_allow_html=True
    )

    cinfo1, cinfo2, cinfo3 = st.columns(3)
    cinfo1.metric('Cache de apuração', cache_gerado or 'Ainda não gerado')
    cinfo2.metric('Linhas resumo', 0 if ap is None else len(ap))
    cinfo3.metric('Linhas por produto', 0 if ap_prod is None else len(ap_prod))

    with st.expander('Atualizar / Recalcular', expanded=False):
        st.caption('A tela não recalcula automaticamente ao abrir. Use os botões abaixo somente quando precisar atualizar a base ou refazer a apuração.')
        col_upd, col_calc = st.columns(2)
        with col_upd:
            st.markdown('**1) Atualizar dados do banco**')
            atualizar_entradas_junto = st.checkbox('Atualizar entradas também', value=False)
            if st.button('🔄 Atualizar dados pela vigência', use_container_width=True):
                try:
                    # Determina o período para buscar vendas. Para todas, usa o menor início e maior fim das negociações ativas.
                    neg_periodo = negociacoes.copy()
                    if neg_selecionada is not None:
                        neg_periodo = pd.DataFrame([neg_selecionada])
                    neg_periodo['data_inicio_dt'] = pd.to_datetime(neg_periodo.get('data_inicio'), errors='coerce').dt.date
                    neg_periodo['data_fim_dt'] = pd.to_datetime(neg_periodo.get('data_fim'), errors='coerce').dt.date
                    neg_periodo = neg_periodo[neg_periodo['data_inicio_dt'].notna() & neg_periodo['data_fim_dt'].notna()]
                    data_ini = neg_periodo['data_inicio_dt'].min() if not neg_periodo.empty else date.today().replace(day=1)
                    data_fim = neg_periodo['data_fim_dt'].max() if not neg_periodo.empty else date.today()
                    if atualizar_entradas_junto:
                        with st.spinner('Atualizando entradas do banco...'):
                            qtd_e, _ = atualizar_cache_sql(tipo='MANUAL_SQL_APURACAO')
                        st.success(f'Entradas atualizadas: {qtd_e} registros.')
                    with st.spinner(f'Atualizando vendas de {data_ini.strftime("%d/%m/%Y")} até {data_fim.strftime("%d/%m/%Y")}...'):
                        qtd_v, _ = atualizar_vendas_sql_por_periodo(data_ini, data_fim, tipo='MANUAL_SQL_VENDAS_APURACAO')
                    st.success(f'Vendas atualizadas: {qtd_v} registros. Agora clique em Recalcular apuração para atualizar os valores da tela.')
                except Exception as e:
                    registrar_atualizacao('MANUAL_SQL_VENDAS_APURACAO', 'Erro', 0, str(e))
                    st.error(f'Erro ao atualizar os dados: {e}')
        with col_calc:
            st.markdown('**2) Recalcular cache de apuração**')
            st.caption('Recalcula com compras/vendas já salvas em cache. Só use após atualizar dados ou alterar regras da negociação.')
            if st.button('⚙️ Recalcular apuração agora', type='primary', use_container_width=True):
                try:
                    with st.spinner('Carregando bases em cache e recalculando apuração...'):
                        compras_calc = load_table('compras')
                        vendas_calc = load_table('vendas')
                        neg_calc = negociacoes.copy()
                        if neg_selecionada is not None:
                            if 'id' in neg_calc.columns:
                                neg_calc = neg_calc[neg_calc['id'].astype(str) == str(neg_selecionada.get('id'))].copy()
                            else:
                                neg_calc = neg_calc[neg_calc['codigo_negociacao'].astype(str) == str(neg_selecionada.get('codigo_negociacao'))].copy()
                        data_ini = date(2000, 1, 1)
                        data_fim = date(2099, 12, 31)
                        ap_new = calcular_apuracao(compras_calc, neg_calc, data_ini, data_fim)
                        ap_prod_new = calcular_apuracao_produtos(compras_calc, neg_calc, data_ini, data_fim, vendas_calc)

                        # Se for uma negociação específica, atualiza apenas ela dentro do cache já existente.
                        if neg_selecionada is not None:
                            ap_old, ap_prod_old = carregar_apuracao_resumida()
                            ap_old_keep = ap_old.copy() if ap_old is not None else pd.DataFrame()
                            ap_prod_old_keep = ap_prod_old.copy() if ap_prod_old is not None else pd.DataFrame()
                            ap_old_keep = _filtrar_cache_negociacao(ap_old_keep, neg_selecionada)
                            ap_prod_old_keep = _filtrar_cache_negociacao(ap_prod_old_keep, neg_selecionada)
                            # remove selecionada do cache antigo, depois concatena a nova
                            ap_all = ap_cache.copy() if ap_cache is not None else pd.DataFrame()
                            ap_prod_all = ap_prod_cache.copy() if ap_prod_cache is not None else pd.DataFrame()
                            ap_keep = ap_all.drop(index=ap_old_keep.index, errors='ignore') if not ap_all.empty else pd.DataFrame()
                            ap_prod_keep = ap_prod_all.drop(index=ap_prod_old_keep.index, errors='ignore') if not ap_prod_all.empty else pd.DataFrame()
                            ap_final = pd.concat([ap_keep, ap_new], ignore_index=True) if not ap_new.empty else ap_keep
                            ap_prod_final = pd.concat([ap_prod_keep, ap_prod_new], ignore_index=True) if not ap_prod_new.empty else ap_prod_keep
                        else:
                            ap_final, ap_prod_final = ap_new, ap_prod_new
                        salvar_apuracao_resumida(ap_final, ap_prod_final)
                    st.success('Apuração recalculada e cache atualizado. A tela será recarregada com os novos dados.')
                    safe_rerun()
                except Exception as e:
                    st.error(f'Erro ao recalcular apuração: {e}')

    if (ap is None or ap.empty) and (ap_prod is None or ap_prod.empty):
        st.warning('Ainda não existe apuração em cache para esta seleção. Clique em Recalcular apuração agora ou aguarde a rotina automática.')
    else:
        if ap is not None and not ap.empty:
            total_compra = pd.to_numeric(ap.get('Valor Compra Base', pd.Series(dtype=float)), errors='coerce').fillna(0).sum()
            total_invest = pd.to_numeric(ap.get('Valor Investimento a Receber', pd.Series(dtype=float)), errors='coerce').fillna(0).sum()
            c1, c2, c3 = st.columns(3)
            c1.markdown(f'<div class="metric-card"><div class="metric-label">Compra base</div><div class="metric-value">{money(total_compra)}</div></div>', unsafe_allow_html=True)
            c2.markdown(f'<div class="metric-card"><div class="metric-label">Investimento a receber</div><div class="metric-value">{money(total_invest)}</div></div>', unsafe_allow_html=True)
            c3.markdown(f'<div class="metric-card"><div class="metric-label">Negociações apuradas</div><div class="metric-value">{len(ap)}</div></div>', unsafe_allow_html=True)
            show = ap.copy()
            for col_moeda in ['Valor Compra Base', 'Valor Investimento a Receber', 'Meta de Compra', 'Compra Realizada', 'Próxima Meta', 'Falta Próxima Meta', 'Verba Comercial']:
                if col_moeda in show.columns:
                    show[col_moeda] = show[col_moeda].map(money)
            if '% Investimento' in show.columns:
                show['% Investimento'] = show['% Investimento'].map(pct)
            if '% Atingimento Meta' in show.columns:
                show['% Atingimento Meta'] = show['% Atingimento Meta'].map(pct)
            st.dataframe(show, use_container_width=True)

        if ap_prod is not None and not ap_prod.empty:
            st.markdown('### Apuração por produto — Sell In / Sell Out')
            total_si = float(pd.to_numeric(ap_prod.get('Investimento Sell In', pd.Series(dtype=float)), errors='coerce').fillna(0).sum())
            total_so = float(pd.to_numeric(ap_prod.get('Investimento Sell Out', pd.Series(dtype=float)), errors='coerce').fillna(0).sum())
            total_prod = float(pd.to_numeric(ap_prod.get('Investimento', pd.Series(dtype=float)), errors='coerce').fillna(0).sum())
            csi, cso, ctot = st.columns(3)
            csi.markdown(f'<div class="metric-card"><div class="metric-label">Investimento Sell In (Compra)</div><div class="metric-value">{money(total_si)}</div><div class="metric-label">Calculado sobre compras/entradas</div></div>', unsafe_allow_html=True)
            cso.markdown(f'<div class="metric-card"><div class="metric-label">Investimento Sell Out (Venda)</div><div class="metric-value">{money(total_so)}</div><div class="metric-label">Calculado sobre vendas/saídas</div></div>', unsafe_allow_html=True)
            ctot.markdown(f'<div class="metric-card"><div class="metric-label">Investimento Total da Negociação</div><div class="metric-value">{money(total_prod)}</div><div class="metric-label">Soma de Sell In + Sell Out</div></div>', unsafe_allow_html=True)
            show_prod = ap_prod.copy()
            for col_moeda in ['Valor Compra', 'Valor Vendido', 'R$ Un SI', 'R$ Un SO', 'Investimento Sell In', 'Investimento Sell Out', 'Investimento', 'R$ Unitário']:
                if col_moeda in show_prod.columns:
                    show_prod[col_moeda] = show_prod[col_moeda].map(money)
            for col_perc in ['% Atingido SI', '% Atingido SO', '% SI', '% SO', '% Atingido', '% Acordo Produto']:
                if col_perc in show_prod.columns:
                    show_prod[col_perc] = show_prod[col_perc].map(pct)
            show_prod = show_prod.rename(columns={
                'Tipo Investimento': 'Tipo de Investimento',
                'Meta Sell In': 'Meta de Compra (Sell In)',
                'Compra Realizada': 'Compra Realizada (Qtd)',
                'Valor Compra': 'Compra Realizada (R$)',
                '% Atingido SI': 'Atingimento da Meta de Compra (%)',
                'Dif SI': 'Falta para Meta de Compra',
                'R$ Un SI': 'Valor por Unidade (Compra)',
                '% SI': 'Percentual sobre Compra (%)',
                'Investimento Sell In': 'Investimento da Compra (Sell In)',
                'Meta Sell Out': 'Meta de Venda Qtd (Sell Out)',
                'Meta Valor Sell Out': 'Meta de Venda Valor (R$)',
                'Venda Realizada': 'Venda Realizada (Qtd)',
                'Valor Vendido': 'Venda Realizada (R$)',
                '% Atingido SO': 'Atingimento da Meta de Venda (%)',
                'Dif SO': 'Falta para Meta de Venda',
                'R$ Un SO': 'Valor por Unidade (Venda)',
                '% SO': 'Percentual sobre Venda (%)',
                'Investimento Sell Out': 'Investimento da Venda (Sell Out)',
                'Investimento': 'Investimento Total',
                'Registros SI': 'Registros de Compras',
                'Registros SO': 'Registros de Vendas',
                'Observação Produto': 'Observações do Produto'
            })
            st.dataframe(show_prod, use_container_width=True)

        excel_path = APP_DIR / 'apuracao_investimento.xlsx'
        with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
            if ap is not None and not ap.empty:
                ap.to_excel(writer, index=False, sheet_name='Apuracao')
            if ap_prod is not None and not ap_prod.empty:
                ap_prod.to_excel(writer, index=False, sheet_name='Apuracao_Produto')
        with open(excel_path, 'rb') as f:
            st.download_button('Baixar apuração em Excel', f, file_name='apuracao_investimento.xlsx')


elif menu == 'Workflow':
    st.subheader('Workflow da Negociação')
    st.info('Controle as etapas da negociação: cadastro, aprovação, implantação, apuração, prestação de contas, cobrança, recebimento e finalização.')
    opcoes, mapa = listar_negociacoes_label()
    if not opcoes:
        st.warning('Nenhuma negociação cadastrada.')
    else:
        escolha = st.selectbox('Selecionar negociação', opcoes, key='workflow_neg')
        neg = mapa[escolha]
        neg_id = int(neg.get('id'))
        c1, c2, c3 = st.columns(3)
        c1.metric('Código', str(neg.get('codigo_curto') or neg.get('codigo_negociacao') or neg_id))
        c2.metric('Fabricante/Distribuidor', str(neg.get('nome','')))
        c3.metric('Status atual', str(neg.get('status','')))
        with st.form('form_workflow'):
            etapa = st.selectbox('Etapa', ['Cadastro','Aprovada','Implantada','Apuração','Prestação de contas','Cobrança','Recebida','Finalizada','Cancelada'])
            status_w = st.selectbox('Situação', ['Pendente','Em andamento','Concluída','Bloqueada','Cancelada'])
            responsavel = st.text_input('Responsável', value=usuario_atual())
            obs = st.text_area('Observação')
            ok = st.form_submit_button('Registrar etapa', use_container_width=True)
            if ok:
                registrar_workflow(neg_id, etapa, status_w, responsavel, obs)
                st.success('Etapa registrada com sucesso.')
        hist = load_table_safe('workflow_negociacao')
        if not hist.empty:
            view = hist[hist['negociacao_id'].astype(str)==str(neg_id)].copy()
            if not view.empty:
                st.markdown('### Histórico do workflow')
                st.dataframe(view.sort_values('id', ascending=False), use_container_width=True)
            else:
                st.caption('Ainda não há etapas registradas para esta negociação.')

elif menu == 'Extrato Financeiro':
    st.subheader('Extrato Financeiro — Conta Corrente')
    st.info('Extrato estilo bancário por fabricante/fornecedor: créditos apurados, recebimentos, glosas, abatimentos, baixas e saldo linha a linha.')
    compras, vendas, negociacoes, ap, ap_prod = carregar_visao_executiva()
    avisar_resumo_desatualizado()
    if negociacoes is None or negociacoes.empty:
        st.warning('Nenhuma negociação cadastrada.')
    else:
        entidades = listar_entidades_com_negociacao(negociacoes)
        if entidades.empty:
            st.warning('Nenhum fabricante/fornecedor com negociação cadastrada.')
        else:
            periodo_ini_padrao, periodo_fim_padrao = _periodo_global_negociacoes(negociacoes)
            st.markdown('### Filtros')
            f1, f2, f3, f4, f5 = st.columns([1.1,1.1,1.2,1.2,1.1])
            data_ini_ext = f1.date_input('De', value=periodo_ini_padrao, key='extrato_data_ini')
            data_fim_ext = f2.date_input('Até', value=periodo_fim_padrao, key='extrato_data_fim')
            fabricantes = entidades[entidades['Tipo Entidade'].astype(str).str.lower().str.contains('fabricante', na=False)]
            fornecedores = entidades[entidades['Tipo Entidade'].astype(str).str.lower().str.contains('fornecedor|distribuidor', na=False)]
            fab_sel = f3.selectbox('Fabricante', ['Todos'] + sorted(fabricantes['Fabricante/Fornecedor'].dropna().astype(str).unique().tolist()), key='extrato_fabricante')
            forn_sel = f4.selectbox('Fornecedor / Distribuidor', ['Todos'] + sorted(fornecedores['Fabricante/Fornecedor'].dropna().astype(str).unique().tolist()), key='extrato_fornecedor')
            if fab_sel != 'Todos' and forn_sel == 'Todos':
                tipo_ent, nome_ent = 'Fabricante', fab_sel
            elif forn_sel != 'Todos' and fab_sel == 'Todos':
                # Mantém compatibilidade com bases que usam 'Fornecedor' ou 'Distribuidor' no campo tipo.
                tipo_ent, nome_ent = 'Todos', forn_sel
            else:
                tipo_ent, nome_ent = 'Todos', 'Todos'
            situacao_ext = f5.selectbox('Situação', ['Todos','Em Aberto','Parcialmente Recebido','Recebido','Baixa','-'], key='extrato_situacao')
            f6, f7, f8 = st.columns([1.1, 1.5, 2.0])
            tipo_lanc_ext = f6.selectbox('Tipo', ['Todos','Crédito a Receber','Baixa / Recebimento'], key='extrato_tipo_lanc')
            neg_labels, neg_map = listar_negociacoes_label()
            neg_escolha = f7.selectbox('Negociação', ['Todas'] + neg_labels, key='extrato_negociacao') if neg_labels else 'Todas'
            f8.caption('Créditos nascem da apuração. Débitos são recebimentos, glosas, abatimentos e baixas.')

            ext = montar_extrato_bancario(ap, ap_prod, negociacoes, tipo_ent, nome_ent, data_ini_ext, data_fim_ext, situacao_ext, tipo_lanc_ext)
            competencias_opts = ['Todas'] + (sorted([c for c in ext.get('Competência', pd.Series(dtype=str)).dropna().astype(str).unique().tolist() if c]) if not ext.empty else [])
            competencia_sel = st.selectbox('Competência (mês/ano)', competencias_opts, key='extrato_competencia')
            if competencia_sel != 'Todas' and not ext.empty and 'Competência' in ext.columns:
                ext = ext[ext['Competência'].astype(str) == competencia_sel].copy()
            if neg_escolha != 'Todas' and not ext.empty:
                neg_sel = neg_map.get(neg_escolha, {})
                cods = [str(neg_sel.get('codigo_curto','')), str(neg_sel.get('codigo_negociacao',''))]
                ext = ext[ext['Negociação'].astype(str).isin([c for c in cods if c])]
                ext['Crédito (R$)'] = pd.to_numeric(ext.get('Crédito (R$)'), errors='coerce').fillna(0)
                ext['Débito (R$)'] = pd.to_numeric(ext.get('Débito (R$)'), errors='coerce').fillna(0)
                ext = ext.reset_index(drop=True)
                ext['Saldo (R$)'] = (ext['Crédito (R$)'] - ext['Débito (R$)']).cumsum()

            total_creditos = float(ext['Crédito (R$)'].sum()) if not ext.empty and 'Crédito (R$)' in ext.columns else 0.0
            total_debitos = float(ext['Débito (R$)'].sum()) if not ext.empty and 'Débito (R$)' in ext.columns else 0.0
            saldo_atual = float(ext['Saldo (R$)'].iloc[-1]) if not ext.empty and 'Saldo (R$)' in ext.columns else 0.0
            em_aberto = saldo_atual
            saldo_inicial = 0.0

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.markdown(f'<div class="metric-card"><div class="metric-label">Saldo Inicial</div><div class="metric-value">{money(saldo_inicial)}</div><div class="subtitle">No início do período</div></div>', unsafe_allow_html=True)
            c2.markdown(f'<div class="metric-card"><div class="metric-label">Créditos</div><div class="metric-value" style="color:#22c55e">{money(total_creditos)}</div><div class="subtitle">Total a receber</div></div>', unsafe_allow_html=True)
            c3.markdown(f'<div class="metric-card"><div class="metric-label">Débitos</div><div class="metric-value" style="color:#ef4444">{money(total_debitos)}</div><div class="subtitle">Recebido / baixado</div></div>', unsafe_allow_html=True)
            c4.markdown(f'<div class="metric-card"><div class="metric-label">Saldo Atual</div><div class="metric-value">{money(saldo_atual)}</div><div class="subtitle">Créditos - débitos</div></div>', unsafe_allow_html=True)
            c5.markdown(f'<div class="metric-card"><div class="metric-label">Em Aberto</div><div class="metric-value" style="color:#f59e0b">{money(em_aberto)}</div><div class="subtitle">A receber</div></div>', unsafe_allow_html=True)

            st.markdown('### Extrato financeiro')
            if ext.empty:
                st.warning('Nenhum lançamento encontrado para os filtros selecionados.')
            else:
                show = ext.copy()
                # mantém colunas principais no formato de extrato bancário
                cols_show = ['Competência','Data','Histórico','Negociação','Documento','Crédito (R$)','Débito (R$)','Saldo (R$)','Tipo','Situação','Data Recebimento','Comprovantes','Usuário','Observação','Origem Registro','Lançamento ID','Crédito Vinculado']
                cols_show = [c for c in cols_show if c in show.columns]
                if 'Lançamento ID' in show.columns:
                    show['Ações'] = show['Lançamento ID'].astype(str).apply(lambda x: '✏️ Editar / 🗑️ Excluir' if x.strip() not in ['', 'nan', 'None'] else '🔒 Apuração')
                    if 'Ações' not in cols_show:
                        cols_show.append('Ações')
                for c in ['Crédito (R$)','Débito (R$)','Saldo (R$)']:
                    if c in show.columns:
                        show[c] = show[c].map(money)
                st.dataframe(show[cols_show], use_container_width=True, height=430)

                # Ações visíveis para lançamentos financeiros manuais/avulsos
                lanc_acao = ext.copy()
                if 'Lançamento ID' in lanc_acao.columns:
                    lanc_acao = lanc_acao[lanc_acao['Lançamento ID'].astype(str).str.strip().isin(['', 'nan', 'None']) == False].copy()
                else:
                    lanc_acao = pd.DataFrame()
                if not lanc_acao.empty:
                    st.markdown('#### Ações do lançamento')
                    st.caption('Use esta área para visualizar, alterar ou excluir lançamentos financeiros manuais/avulsos do extrato. Lançamentos automáticos da apuração ficam protegidos.')
                    def _lbl_lanc_acao(rr):
                        valor_l = float(pd.to_numeric(pd.Series([rr.get('Crédito (R$)',0)]), errors='coerce').fillna(0).iloc[0]) or float(pd.to_numeric(pd.Series([rr.get('Débito (R$)',0)]), errors='coerce').fillna(0).iloc[0])
                        origem_lbl = str(rr.get('Origem Registro') or '')
                        return f"#{rr.get('Lançamento ID')} | {origem_lbl} | {rr.get('Data','')} | {rr.get('Histórico','')} | {rr.get('Negociação','')} | {money(valor_l)}"
                    labels_lanc_acao = [_lbl_lanc_acao(r) for _, r in lanc_acao.iterrows()]
                    sel_lanc_acao = st.selectbox('Selecionar lançamento para ação', labels_lanc_acao, key='acao_lancamento_extrato_sel')
                    row_acao = lanc_acao.iloc[labels_lanc_acao.index(sel_lanc_acao)]
                    lanc_id_acao = int(float(row_acao.get('Lançamento ID') or 0))
                    acao_lanc = st.radio('Ação', ['Visualizar', 'Editar', 'Excluir'], horizontal=True, key=f'acao_lancamento_extrato_{lanc_id_acao}')
                    origem_acao = str(row_acao.get('Origem Registro') or 'financeiro_lancamentos')
                    tabela_acao = 'recebimentos_negociacao' if 'recebimentos_negociacao' in origem_acao.lower() else 'financeiro_lancamentos'
                    with get_conn() as con:
                        det_lanc = pd.read_sql_query(f'SELECT * FROM {tabela_acao} WHERE id=?', con, params=(lanc_id_acao,))
                    if det_lanc.empty:
                        st.warning('Lançamento não encontrado no banco local.')
                    else:
                        det = det_lanc.iloc[0]
                        if acao_lanc == 'Visualizar':
                            v1, v2, v3, v4 = st.columns(4)
                            v1.metric('ID', str(lanc_id_acao))
                            v2.metric('Data', str(det.get('data_lancamento','')))
                            v3.metric('Tipo', str(det.get('tipo_movimento','')))
                            v4.metric('Valor', money(float(det.get('valor') or 0)))
                            st.write('**Observação:**', str(det.get('observacao') or ''))
                        elif acao_lanc == 'Editar':
                            if tabela_acao == 'recebimentos_negociacao':
                                st.info('Este registro veio da tabela antiga de recebimentos. Para manter segurança, ele pode ser visualizado ou excluído/estornado. Novas alterações devem ser feitas como lançamento financeiro avulso.')
                                st.stop()
                            entidades_edit = listar_entidades_com_negociacao(negociacoes)
                            nomes_fabricantes_edit = sorted(entidades_edit[entidades_edit['Tipo Entidade'].astype(str).str.lower().str.contains('fabricante', na=False)]['Fabricante/Fornecedor'].dropna().astype(str).unique().tolist()) if not entidades_edit.empty else []
                            nomes_fornecedores_edit = sorted(entidades_edit[entidades_edit['Tipo Entidade'].astype(str).str.lower().str.contains('fornecedor|distribuidor', na=False)]['Fabricante/Fornecedor'].dropna().astype(str).unique().tolist()) if not entidades_edit.empty else []
                            with st.form(f'form_editar_lancamento_visivel_{lanc_id_acao}'):
                                e1, e2, e3 = st.columns(3)
                                data_pad = pd.to_datetime(det.get('data_lancamento'), errors='coerce')
                                if pd.isna(data_pad):
                                    data_pad = pd.Timestamp(date.today())
                                data_edit2 = e1.date_input('Data do lançamento', value=data_pad.date(), key=f'edit_vis_data_{lanc_id_acao}')
                                comp_atual2 = str(det.get('competencia') or competencia_from_date(det.get('data_lancamento')) or f'{date.today().month:02d}/{date.today().year}')
                                meses_edit2 = ['01 - Janeiro','02 - Fevereiro','03 - Março','04 - Abril','05 - Maio','06 - Junho','07 - Julho','08 - Agosto','09 - Setembro','10 - Outubro','11 - Novembro','12 - Dezembro']
                                try:
                                    mes_idx2 = max(0, min(11, int(str(comp_atual2).split('/')[0]) - 1))
                                    ano_val2 = int(str(comp_atual2).split('/')[-1])
                                except Exception:
                                    mes_idx2, ano_val2 = date.today().month - 1, date.today().year
                                mes_edit2 = e2.selectbox('Competência mês', meses_edit2, index=mes_idx2, key=f'edit_vis_mes_{lanc_id_acao}')
                                ano_edit2 = e3.number_input('Competência ano', min_value=2020, max_value=2035, value=ano_val2, step=1, key=f'edit_vis_ano_{lanc_id_acao}')
                                comp_edit2 = f'{str(mes_edit2)[:2]}/{int(ano_edit2)}'
                                ce1, ce2 = st.columns([1,2])
                                tipo_ent_atual2 = str(det.get('entidade_tipo') or 'Fabricante')
                                tipo_ent_edit2 = ce1.selectbox('Conta', ['Fabricante','Fornecedor'], index=0 if tipo_ent_atual2 == 'Fabricante' else 1, key=f'edit_vis_ent_tipo_{lanc_id_acao}')
                                nomes_base2 = nomes_fabricantes_edit if tipo_ent_edit2 == 'Fabricante' else nomes_fornecedores_edit
                                nome_atual2 = str(det.get('entidade_nome') or '')
                                opts_nome2 = [''] + nomes_base2
                                if nome_atual2 and nome_atual2 not in opts_nome2:
                                    opts_nome2 = [''] + [nome_atual2] + nomes_base2
                                nome_ent_edit2 = ce2.selectbox(tipo_ent_edit2, opts_nome2, index=opts_nome2.index(nome_atual2) if nome_atual2 in opts_nome2 else 0, key=f'edit_vis_ent_nome_{lanc_id_acao}')
                                m1, m2, m3 = st.columns([1.2, 1.4, 1])
                                nat_atual2 = 'Crédito' if _is_credito_natureza(det.get('natureza')) else 'Débito'
                                natureza_edit2 = m1.selectbox('Natureza', ['Crédito','Débito'], index=0 if nat_atual2 == 'Crédito' else 1, key=f'edit_vis_nat_{lanc_id_acao}')
                                tipos_mov2 = listar_tipos_credito_ativos() if natureza_edit2 == 'Crédito' else ['Recebimento PIX','Recebimento TED','Recebimento boleto','Glosa','Abatimento','Estorno','Ajuste contra','Baixa manual','Outros débitos']
                                tipo_atual2 = str(det.get('tipo_movimento') or '')
                                if tipo_atual2 and tipo_atual2 not in tipos_mov2:
                                    tipos_mov2 = [tipo_atual2] + tipos_mov2
                                tipo_mov_edit2 = m2.selectbox('Tipo do lançamento', tipos_mov2, index=tipos_mov2.index(tipo_atual2) if tipo_atual2 in tipos_mov2 else 0, key=f'edit_vis_tipo_mov_{lanc_id_acao}')
                                valor_edit2 = m3.number_input('Valor (R$)', min_value=0.0, step=100.0, format='%.2f', value=float(det.get('valor') or 0), key=f'edit_vis_valor_{lanc_id_acao}')
                                documento_edit2 = st.text_input('Documento / referência', value=str(det.get('documento') or ''), key=f'edit_vis_doc_{lanc_id_acao}')
                                obs_edit2 = st.text_area('Observação / motivo', value=str(det.get('observacao') or ''), key=f'edit_vis_obs_{lanc_id_acao}')
                                salvar_edit2 = st.form_submit_button('Salvar alterações', use_container_width=True)
                                if salvar_edit2:
                                    if not nome_ent_edit2:
                                        st.warning('Selecione o fabricante/fornecedor.')
                                    elif valor_edit2 <= 0:
                                        st.warning('Informe um valor maior que zero.')
                                    else:
                                        ok, msg = atualizar_lancamento_avulso(lanc_id_acao, {
                                            'data_lancamento': str(data_edit2),
                                            'competencia': comp_edit2,
                                            'tipo_movimento': tipo_mov_edit2,
                                            'natureza': natureza_edit2,
                                            'valor': float(valor_edit2),
                                            'documento': documento_edit2,
                                            'observacao': obs_edit2,
                                            'entidade_tipo': tipo_ent_edit2,
                                            'entidade_nome': nome_ent_edit2,
                                        }, usuario=usuario_atual())
                                        if ok:
                                            st.success(msg)
                                            safe_rerun()
                                        else:
                                            st.warning(msg)
                        elif acao_lanc == 'Excluir':
                            st.error('A exclusão remove o lançamento do extrato e recalcula o saldo. O registro fica preservado para auditoria.')
                            motivo_excluir2 = st.text_area('Motivo da exclusão', value='Lançamento incorreto / cancelado', key=f'motivo_excluir_vis_{lanc_id_acao}')
                            confirmar_excluir2 = st.checkbox('Confirmo que desejo excluir este lançamento', key=f'conf_excluir_vis_{lanc_id_acao}')
                            if st.button('Excluir lançamento selecionado', type='primary', disabled=not confirmar_excluir2, key=f'btn_excluir_vis_{lanc_id_acao}'):
                                ok, msg = excluir_lancamento_extrato(lanc_id_acao, origem_acao, motivo_excluir2, usuario=usuario_atual())
                                if ok:
                                    st.success(msg)
                                    safe_rerun()
                                else:
                                    st.warning(msg)

                resumo = pd.DataFrame({
                    'Indicador': ['Total de créditos','Total de débitos','Saldo atual','Maior saldo','Menor saldo','Qtd. lançamentos'],
                    'Valor': [money(total_creditos), money(total_debitos), money(saldo_atual), money(float(ext['Saldo (R$)'].max())), money(float(ext['Saldo (R$)'].min())), str(len(ext))]
                })
                col_graf, col_resumo = st.columns([3,1])
                with col_graf:
                    chart_df = ext.copy()
                    chart_df['Data_dt'] = pd.to_datetime(chart_df['Data'], errors='coerce')
                    chart_df = chart_df.dropna(subset=['Data_dt'])
                    if not chart_df.empty:
                        chart_df = chart_df.groupby('Data_dt', as_index=False)['Saldo (R$)'].last().sort_values('Data_dt')
                        st.markdown('### Evolução do saldo')
                        st.line_chart(chart_df.set_index('Data_dt')['Saldo (R$)'], height=230)
                with col_resumo:
                    st.markdown('### Resumo do período')
                    st.dataframe(resumo, use_container_width=True, hide_index=True)

                csv_bytes = ext.to_csv(index=False, sep=';').encode('utf-8-sig')
                filtros_txt = f'Período: {data_ini_ext:%d/%m/%Y} a {data_fim_ext:%d/%m/%Y} | Tipo: {tipo_ent} | Conta: {nome_ent} | Competência: {competencia_sel} | Situação: {situacao_ext}'
                b1, b2, b3 = st.columns(3)
                b1.download_button('Exportar Excel/CSV', csv_bytes, file_name='extrato_financeiro.csv', mime='text/csv')
                try:
                    pdf_bytes = gerar_pdf_extrato_financeiro(ext, titulo='Extrato Financeiro - SB Farma', filtros=filtros_txt)
                    b2.download_button('Gerar PDF', pdf_bytes, file_name='extrato_financeiro.pdf', mime='application/pdf')
                except Exception as exc:
                    b2.error(str(exc))
                b3.button('Imprimir', help='Use Ctrl+P no navegador após filtrar o extrato.')

            st.divider()
            with st.expander('Novo lançamento financeiro / baixa / glosa / abatimento', expanded=False):
                opcoes, mapa = listar_negociacoes_label()
                entidades_lanc = listar_entidades_com_negociacao(negociacoes)
                nomes_fabricantes = sorted(entidades_lanc[entidades_lanc['Tipo Entidade'].astype(str).str.lower().str.contains('fabricante', na=False)]['Fabricante/Fornecedor'].dropna().astype(str).unique().tolist()) if not entidades_lanc.empty else []
                nomes_fornecedores = sorted(entidades_lanc[entidades_lanc['Tipo Entidade'].astype(str).str.lower().str.contains('fornecedor|distribuidor', na=False)]['Fabricante/Fornecedor'].dropna().astype(str).unique().tolist()) if not entidades_lanc.empty else []
                with st.form('form_lancamento_financeiro_extrato'):
                    origem_lanc = st.radio('Origem do lançamento', ['Vinculado a uma negociação', 'Lançamento financeiro avulso'], horizontal=True, key='extrato_origem_lanc')
                    neg_id_lanc = 0
                    neg_codigo = ''
                    entidade_tipo_lanc = ''
                    entidade_nome_lanc = ''
                    if origem_lanc == 'Vinculado a uma negociação':
                        if not opcoes:
                            st.warning('Não há negociação cadastrada. Use lançamento financeiro avulso para registrar valores diretamente na conta corrente.')
                        else:
                            escolha_neg = st.selectbox('Negociação', opcoes, key='extrato_lanc_neg')
                            neg = mapa[escolha_neg]
                            neg_id_lanc = int(neg.get('id') or 0)
                            neg_codigo = str(neg.get('codigo_negociacao') or neg.get('codigo_curto') or neg_id_lanc)
                            entidade_tipo_lanc = str(neg.get('tipo') or 'Fabricante')
                            entidade_nome_lanc = str(neg.get('nome') or '')
                    else:
                        a1, a2 = st.columns([1, 2])
                        entidade_tipo_lanc = a1.selectbox('Lançar na conta de', ['Fabricante', 'Fornecedor'], key='extrato_avulso_tipo_entidade')
                        base_nomes = nomes_fabricantes if entidade_tipo_lanc == 'Fabricante' else nomes_fornecedores
                        entidade_nome_lanc = a2.selectbox(f'{entidade_tipo_lanc}', [''] + base_nomes, key='extrato_avulso_entidade_nome')
                        if not entidade_nome_lanc:
                            st.caption('Selecione um fabricante/fornecedor que já tenha negociação cadastrada.')
                        neg_codigo = 'AVULSO'
                    l1, l2, l3 = st.columns(3)
                    data_lanc = l1.date_input('Data do lançamento', value=date.today(), key='extrato_data_lanc')
                    meses = ['01 - Janeiro','02 - Fevereiro','03 - Março','04 - Abril','05 - Maio','06 - Junho','07 - Julho','08 - Agosto','09 - Setembro','10 - Outubro','11 - Novembro','12 - Dezembro']
                    mes_comp = l2.selectbox('Competência mês', meses, index=date.today().month-1, key='extrato_mes_comp')
                    ano_comp = l3.number_input('Competência ano', min_value=2020, max_value=2035, value=date.today().year, step=1, key='extrato_ano_comp')
                    competencia = f"{str(mes_comp)[:2]}/{int(ano_comp)}"
                    st.markdown('#### Tipo de movimentação')
                    modo_lancamento = st.radio(
                        'O que deseja lançar?',
                        ['Crédito a receber', 'Recebimento / baixa de crédito', 'Glosa / abatimento / ajuste'],
                        horizontal=True,
                        key='extrato_modo_lancamento'
                    )

                    parent_credito_id = 0
                    if modo_lancamento == 'Crédito a receber':
                        natureza = 'Crédito'
                        tipo_lista = listar_tipos_credito_ativos()
                        tipo_mov = st.selectbox('Tipo do crédito', tipo_lista, key='extrato_tipo_credito_lanc')
                        st.caption('Crédito a receber aumenta o saldo que o fabricante/fornecedor deve para a SB Farma.')
                        st.markdown('##### Gerenciar tipos de crédito (cadastro rápido)')
                        with st.container():
                            tc1, tc2 = st.columns([2, 1])
                            novo_tipo_credito = tc1.text_input('Novo tipo de crédito', placeholder='Ex.: Verba de mídia, Ponta de gôndola, Campanha especial', key='novo_tipo_credito_fin')
                            if tc2.form_submit_button('Cadastrar tipo'):
                                ok, msg = salvar_tipo_credito(novo_tipo_credito)
                                if ok:
                                    st.success(msg)
                                    safe_rerun()
                                else:
                                    st.warning(msg)
                            tipos_para_excluir = listar_tipos_credito_ativos()
                            if tipos_para_excluir:
                                ex1, ex2 = st.columns([2, 1])
                                tipo_excluir = ex1.selectbox('Tipo de crédito cadastrado', tipos_para_excluir, key='tipo_credito_excluir_fin')
                                if ex2.form_submit_button('Excluir tipo'):
                                    ok, msg = excluir_tipo_credito(tipo_excluir)
                                    if ok:
                                        st.success(msg)
                                        safe_rerun()
                                    else:
                                        st.warning(msg)
                    else:
                        natureza = 'Débito'
                        # Para baixa/recebimento, permite vincular ao crédito em aberto.
                        creditos_abertos = listar_creditos_em_aberto(
                            entidade_tipo_lanc if entidade_tipo_lanc else None,
                            entidade_nome_lanc if entidade_nome_lanc else None,
                            neg_id_lanc if neg_id_lanc else None
                        )
                        if modo_lancamento == 'Recebimento / baixa de crédito':
                            tipo_lista = ['Recebimento PIX','Recebimento TED','Recebimento boleto','Desconto em duplicata','Baixa manual','Compensação','Outro recebimento']
                        else:
                            tipo_lista = ['Glosa','Abatimento','Estorno','Ajuste contra','Cancelamento parcial','Outros débitos']
                        tipo_mov = st.selectbox('Tipo da baixa', tipo_lista, key='extrato_tipo_baixa_lanc')
                        if not creditos_abertos.empty:
                            labels_creditos = ['Não vincular'] + creditos_abertos['Label'].tolist()
                            cred_sel = st.selectbox('Crédito a baixar', labels_creditos, key='extrato_credito_baixar')
                            if cred_sel != 'Não vincular':
                                cred_row = creditos_abertos[creditos_abertos['Label'].eq(cred_sel)].iloc[0]
                                parent_credito_id = int(cred_row.get('id_int') or 0)
                                st.caption(f"Saldo deste crédito: {money(float(cred_row.get('Saldo em Aberto',0)))}")
                        else:
                            st.warning('Não há crédito em aberto para a conta selecionada. O débito será lançado no extrato sem vínculo específico.')

                    valor = st.number_input('Valor (R$)', min_value=0.0, step=100.0, format='%.2f', key='extrato_valor_lanc')
                    data_vencimento = None
                    forma_pagamento = ''
                    if modo_lancamento == 'Crédito a receber':
                        data_vencimento = st.date_input('Previsão de recebimento / vencimento', value=data_lanc, key='extrato_venc_credito')
                    else:
                        forma_pagamento = st.selectbox('Forma de recebimento / baixa', ['PIX','TED','Boleto','Duplicata','Compensação','Bonificação','Outro'], key='extrato_forma_pgto')
                    documento = st.text_input('Documento / referência', key='extrato_doc_lanc')
                    comprovantes = st.file_uploader('Comprovantes em anexo (PDF, imagem, Excel, Word)', type=['pdf','png','jpg','jpeg','xlsx','xls','docx','doc'], accept_multiple_files=True, key='extrato_comprovantes_lanc')
                    observacao = st.text_area('Observação / motivo', key='extrato_obs_lanc')
                    salvar = st.form_submit_button('Salvar lançamento financeiro', use_container_width=True)
                    if salvar:
                        if valor <= 0:
                            st.warning('Informe um valor maior que zero.')
                        elif origem_lanc == 'Vinculado a uma negociação' and not neg_id_lanc:
                            st.warning('Selecione uma negociação válida ou altere a origem para lançamento financeiro avulso.')
                        elif origem_lanc == 'Lançamento financeiro avulso' and not entidade_nome_lanc:
                            st.warning('Selecione o fabricante/fornecedor do lançamento avulso.')
                        else:
                            with connect() as con:
                                con.execute("""
                                    INSERT INTO financeiro_lancamentos
                                    (negociacao_id, data_lancamento, competencia, tipo_movimento, natureza, valor, documento, usuario, observacao, origem_lancamento, entidade_tipo, entidade_nome, parent_credito_id, status_credito, data_vencimento, forma_pagamento)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, (int(neg_id_lanc or 0), str(data_lanc), competencia, tipo_mov, natureza, float(valor), documento, usuario_atual(), observacao, origem_lanc, entidade_tipo_lanc, entidade_nome_lanc, int(parent_credito_id or 0), 'Em Aberto' if natureza == 'Crédito' else '', str(data_vencimento or ''), forma_pagamento))
                                lancamento_id = con.execute('SELECT last_insert_rowid()').fetchone()[0]
                                if neg_id_lanc:
                                    registrar_historico(con, int(neg_id_lanc), neg_codigo, f'Lançamento financeiro - {natureza}', '', money(valor), usuario=usuario_atual(), observacao=f'{tipo_mov} | Competência {competencia} | {documento} | {observacao}')
                                con.commit()
                            try:
                                atualizar_status_creditos_financeiros()
                            except Exception:
                                pass
                            if comprovantes:
                                salvar_comprovantes_financeiros(lancamento_id, int(neg_id_lanc or 0), comprovantes, competencia, entidade_nome_lanc)
                            st.success('Lançamento financeiro salvo com sucesso.')
                            safe_rerun()


            st.divider()
            with st.expander('Editar ou excluir lançamentos avulsos', expanded=False):
                st.caption('Use esta área para corrigir lançamentos financeiros avulsos. Exclusões são lógicas: o lançamento sai do extrato, mas permanece preservado para auditoria.')
                filtro_tipo_edit = 'Todos'
                filtro_nome_edit = 'Todos'
                try:
                    if fab_sel != 'Todos' and forn_sel == 'Todos':
                        filtro_tipo_edit, filtro_nome_edit = 'Fabricante', fab_sel
                    elif forn_sel != 'Todos' and fab_sel == 'Todos':
                        filtro_tipo_edit, filtro_nome_edit = 'Fornecedor', forn_sel
                except Exception:
                    pass
                avulsos = listar_lancamentos_avulsos_financeiros(filtro_tipo_edit, filtro_nome_edit)
                if avulsos.empty:
                    st.info('Nenhum lançamento avulso ativo encontrado para os filtros atuais.')
                else:
                    av_view = avulsos.copy()
                    av_view['Valor (R$)'] = to_numero(av_view.get('valor')).fillna(0).map(money)
                    cols_av = [c for c in ['id','data_lancamento','competencia','entidade_tipo','entidade_nome','tipo_movimento','natureza','Valor (R$)','documento','usuario','observacao'] if c in av_view.columns]
                    st.dataframe(av_view[cols_av], use_container_width=True, height=260)
                    labels_av = []
                    mapa_av = {}
                    for _, rr in avulsos.iterrows():
                        lbl = f"#{int(rr.get('id'))} | {rr.get('data_lancamento','')} | {rr.get('entidade_nome','')} | {rr.get('tipo_movimento','')} | {money(float(rr.get('valor') or 0))}"
                        labels_av.append(lbl)
                        mapa_av[lbl] = rr
                    selecionado_av = st.selectbox('Selecionar lançamento avulso', labels_av, key='editar_avulso_sel')
                    row_av = mapa_av[selecionado_av]
                    tabs_av = st.tabs(['Alterar', 'Excluir'])
                    with tabs_av[0]:
                        with st.form(f"form_editar_lanc_avulso_{int(row_av.get('id'))}"):
                            e1, e2, e3 = st.columns(3)
                            data_padrao = pd.to_datetime(row_av.get('data_lancamento'), errors='coerce')
                            if pd.isna(data_padrao):
                                data_padrao = pd.Timestamp(date.today())
                            data_edit = e1.date_input('Data do lançamento', value=data_padrao.date(), key=f"edit_av_data_{int(row_av.get('id'))}")
                            comp_atual = str(row_av.get('competencia') or competencia_from_date(row_av.get('data_lancamento')) or f"{date.today().month:02d}/{date.today().year}")
                            meses_edit = ['01 - Janeiro','02 - Fevereiro','03 - Março','04 - Abril','05 - Maio','06 - Junho','07 - Julho','08 - Agosto','09 - Setembro','10 - Outubro','11 - Novembro','12 - Dezembro']
                            try:
                                mes_idx = max(0, min(11, int(str(comp_atual).split('/')[0]) - 1))
                                ano_val = int(str(comp_atual).split('/')[-1])
                            except Exception:
                                mes_idx, ano_val = date.today().month - 1, date.today().year
                            mes_edit = e2.selectbox('Competência mês', meses_edit, index=mes_idx, key=f"edit_av_mes_{int(row_av.get('id'))}")
                            ano_edit = e3.number_input('Competência ano', min_value=2020, max_value=2035, value=ano_val, step=1, key=f"edit_av_ano_{int(row_av.get('id'))}")
                            comp_edit = f"{str(mes_edit)[:2]}/{int(ano_edit)}"
                            c_ent1, c_ent2 = st.columns([1,2])
                            tipo_ent_atual = str(row_av.get('entidade_tipo') or 'Fabricante')
                            tipo_ent_edit = c_ent1.selectbox('Conta', ['Fabricante','Fornecedor'], index=0 if tipo_ent_atual == 'Fabricante' else 1, key=f"edit_av_ent_tipo_{int(row_av.get('id'))}")
                            nomes_base_edit = nomes_fabricantes if tipo_ent_edit == 'Fabricante' else nomes_fornecedores
                            nome_atual = str(row_av.get('entidade_nome') or '')
                            opts_nome_edit = [''] + nomes_base_edit
                            if nome_atual and nome_atual not in opts_nome_edit:
                                opts_nome_edit = [''] + [nome_atual] + nomes_base_edit
                            nome_ent_edit = c_ent2.selectbox(tipo_ent_edit, opts_nome_edit, index=opts_nome_edit.index(nome_atual) if nome_atual in opts_nome_edit else 0, key=f"edit_av_ent_nome_{int(row_av.get('id'))}")
                            m1, m2, m3 = st.columns([1.2, 1.4, 1])
                            nat_atual = 'Crédito' if _is_credito_natureza(row_av.get('natureza')) else 'Débito'
                            natureza_edit = m1.selectbox('Natureza', ['Crédito','Débito'], index=0 if nat_atual == 'Crédito' else 1, key=f"edit_av_nat_{int(row_av.get('id'))}")
                            if natureza_edit == 'Crédito':
                                tipos_credito = listar_tipos_credito_ativos()
                                tipo_atual_mov = str(row_av.get('tipo_movimento') or '')
                                if tipo_atual_mov and tipo_atual_mov not in tipos_credito:
                                    tipos_credito = [tipo_atual_mov] + tipos_credito
                                tipo_mov_edit = m2.selectbox('Tipo do crédito', tipos_credito, index=tipos_credito.index(tipo_atual_mov) if tipo_atual_mov in tipos_credito else 0, key=f"edit_av_tipo_mov_{int(row_av.get('id'))}")
                            else:
                                tipos_debito = ['Recebimento PIX','Recebimento TED','Recebimento boleto','Glosa','Abatimento','Estorno','Ajuste contra','Baixa manual','Outros débitos']
                                tipo_atual_mov = str(row_av.get('tipo_movimento') or '')
                                if tipo_atual_mov and tipo_atual_mov not in tipos_debito:
                                    tipos_debito = [tipo_atual_mov] + tipos_debito
                                tipo_mov_edit = m2.selectbox('Tipo da baixa/débito', tipos_debito, index=tipos_debito.index(tipo_atual_mov) if tipo_atual_mov in tipos_debito else 0, key=f"edit_av_tipo_mov_{int(row_av.get('id'))}")
                            valor_edit = m3.number_input('Valor (R$)', min_value=0.0, step=100.0, format='%.2f', value=float(row_av.get('valor') or 0), key=f"edit_av_valor_{int(row_av.get('id'))}")
                            d1, d2 = st.columns(2)
                            documento_edit = d1.text_input('Documento / referência', value=str(row_av.get('documento') or ''), key=f"edit_av_doc_{int(row_av.get('id'))}")
                            forma_edit = d2.text_input('Forma pagamento/baixa', value=str(row_av.get('forma_pagamento') or ''), key=f"edit_av_forma_{int(row_av.get('id'))}")
                            venc_atual = pd.to_datetime(row_av.get('data_vencimento'), errors='coerce')
                            data_venc_edit = None
                            if natureza_edit == 'Crédito':
                                data_venc_edit = st.date_input('Previsão de recebimento / vencimento', value=(venc_atual.date() if not pd.isna(venc_atual) else data_edit), key=f"edit_av_venc_{int(row_av.get('id'))}")
                            obs_edit = st.text_area('Observação / motivo', value=str(row_av.get('observacao') or ''), key=f"edit_av_obs_{int(row_av.get('id'))}")
                            novos_comprovantes = st.file_uploader('Adicionar novos comprovantes', type=['pdf','png','jpg','jpeg','xlsx','xls','docx','doc'], accept_multiple_files=True, key=f"edit_av_comp_{int(row_av.get('id'))}")
                            salvar_edit_av = st.form_submit_button('Salvar alterações do lançamento', use_container_width=True)
                            if salvar_edit_av:
                                if not nome_ent_edit:
                                    st.warning('Selecione o fabricante/fornecedor.')
                                elif valor_edit <= 0:
                                    st.warning('Informe um valor maior que zero.')
                                else:
                                    dados_edit = {
                                        'data_lancamento': str(data_edit),
                                        'competencia': comp_edit,
                                        'tipo_movimento': tipo_mov_edit,
                                        'natureza': natureza_edit,
                                        'valor': float(valor_edit),
                                        'documento': documento_edit,
                                        'observacao': obs_edit,
                                        'entidade_tipo': tipo_ent_edit,
                                        'entidade_nome': nome_ent_edit,
                                        'data_vencimento': str(data_venc_edit or ''),
                                        'forma_pagamento': forma_edit,
                                    }
                                    ok, msg = atualizar_lancamento_avulso(int(row_av.get('id')), dados_edit, usuario=usuario_atual())
                                    if ok and novos_comprovantes:
                                        salvar_comprovantes_financeiros(int(row_av.get('id')), int(row_av.get('negociacao_id') or 0), novos_comprovantes, comp_edit, nome_ent_edit)
                                    if ok:
                                        st.success(msg)
                                        safe_rerun()
                                    else:
                                        st.warning(msg)
                    with tabs_av[1]:
                        st.warning('A exclusão retira o lançamento do extrato e da conta corrente, mas mantém o registro preservado para auditoria.')
                        motivo_del = st.text_area('Motivo da exclusão', value='Lançamento incorreto / cancelado', key=f"motivo_del_av_{int(row_av.get('id'))}")
                        confirmar_del = st.checkbox('Confirmo a exclusão deste lançamento avulso', key=f"conf_del_av_{int(row_av.get('id'))}")
                        if st.button('Excluir lançamento avulso selecionado', type='secondary', disabled=not confirmar_del, key=f"btn_del_av_{int(row_av.get('id'))}"):
                            ok, msg = excluir_lancamento_avulso(int(row_av.get('id')), motivo_del, usuario=usuario_atual())
                            if ok:
                                st.success(msg)
                                safe_rerun()
                            else:
                                st.warning(msg)

elif menu == 'Documentos':
    st.subheader('Central de Documentos')
    st.info('Anexe propostas comerciais, contratos, e-mails, planilhas, evidências, PDFs e documentos de cobrança vinculados ao código da negociação.')
    opcoes, mapa = listar_negociacoes_label()
    if not opcoes:
        st.warning('Nenhuma negociação cadastrada.')
    else:
        escolha = st.selectbox('Selecionar negociação', opcoes, key='docs_neg')
        neg = mapa[escolha]
        neg_id = int(neg.get('id'))
        with st.form('form_documento'):
            tipo_doc = st.selectbox('Tipo do documento', ['Proposta comercial','Contrato','E-mail','Planilha','Nota fiscal','Evidência','PDF de prestação de contas','Comprovante de pagamento','Outro'])
            arq = st.file_uploader('Selecionar arquivo', type=None)
            obs = st.text_area('Observação do documento')
            ok = st.form_submit_button('Salvar documento', use_container_width=True)
            if ok:
                if arq is None:
                    st.warning('Selecione um arquivo para salvar.')
                else:
                    destino = salvar_documento_negociacao(neg_id, arq, tipo_doc, obs)
                    st.success(f'Documento salvo: {destino.name}')
        docs = load_table_safe('documentos_negociacao')
        if not docs.empty:
            view = docs[docs['negociacao_id'].astype(str)==str(neg_id)].copy()
            st.markdown('### Documentos vinculados')
            st.dataframe(view.sort_values('id', ascending=False), use_container_width=True)
        else:
            st.caption('Nenhum documento anexado ainda.')

elif menu == 'Agenda':
    st.subheader('Agenda de Negociações e Pendências')
    st.info('Acompanhe vencimentos, negociações vencidas e ações que precisam de atenção.')
    negociacoes = load_table('negociacoes')
    compras, vendas, negociacoes, ap, ap_prod = carregar_visao_executiva()
    if negociacoes is None or negociacoes.empty:
        st.warning('Nenhuma negociação cadastrada.')
    else:
        hoje = date.today()
        n = negociacoes.copy()
        n['data_inicio_dt'] = pd.to_datetime(n.get('data_inicio'), errors='coerce').dt.date
        n['data_fim_dt'] = pd.to_datetime(n.get('data_fim'), errors='coerce').dt.date
        n['Dias para vencer'] = n['data_fim_dt'].apply(lambda d: (d - hoje).days if pd.notna(d) else None)
        vencidas = n[n['Dias para vencer'].fillna(999999) < 0]
        proximas = n[(n['Dias para vencer'].fillna(999999) >= 0) & (n['Dias para vencer'].fillna(999999) <= 30)]
        sem_apuracao = pd.DataFrame()
        if ap is not None and not ap.empty:
            cods_apurados = set(ap['Código Negociação'].astype(str))
            sem_apuracao = n[~n.get('codigo_negociacao','').astype(str).isin(cods_apurados)].copy()
        else:
            sem_apuracao = n.copy()
        c1, c2, c3 = st.columns(3)
        c1.markdown(f'<div class="metric-card"><div class="metric-label">Vencidas</div><div class="metric-value">{len(vencidas)}</div></div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="metric-card"><div class="metric-label">Vencem em até 30 dias</div><div class="metric-value">{len(proximas)}</div></div>', unsafe_allow_html=True)
        c3.markdown(f'<div class="metric-card"><div class="metric-label">Sem apuração geral</div><div class="metric-value">{len(sem_apuracao)}</div></div>', unsafe_allow_html=True)
        abas = st.tabs(['Vencendo', 'Vencidas', 'Sem apuração', 'Todas'])
        cols = [c for c in ['codigo_curto','codigo_negociacao','nome','tipo','tipo_negociacao','tipo_investimento','status','data_inicio_dt','data_fim_dt','Dias para vencer','observacao'] if c in n.columns]
        with abas[0]: st.dataframe(proximas[cols].sort_values('Dias para vencer'), use_container_width=True)
        with abas[1]: st.dataframe(vencidas[cols].sort_values('Dias para vencer'), use_container_width=True)
        with abas[2]: st.dataframe(sem_apuracao[cols], use_container_width=True)
        with abas[3]: st.dataframe(n[cols].sort_values('Dias para vencer'), use_container_width=True)

elif menu == 'Painel Executivo':
    st.subheader('Dashboard Executivo')
    compras, vendas, negociacoes, ap, ap_prod = carregar_visao_executiva()
    avisar_resumo_desatualizado()
    if negociacoes is None or negociacoes.empty:
        st.info('Cadastre uma negociação e importe os dados para visualizar o dashboard executivo.')
    else:
        inv_geral = float(_num_col(ap, 'Valor Investimento a Receber').sum()) if ap is not None else 0.0
        inv_si = float(_num_col(ap_prod, 'Investimento Sell In').sum()) if ap_prod is not None else 0.0
        inv_so = float(_num_col(ap_prod, 'Investimento Sell Out').sum()) if ap_prod is not None else 0.0
        inv_prod = float(_num_col(ap_prod, 'Investimento').sum()) if ap_prod is not None else 0.0
        venda_apurada = float(_num_col(ap_prod, 'Valor Vendido').sum()) if ap_prod is not None else 0.0
        compra_apurada = float(_num_col(ap, 'Valor Compra Base').sum()) if ap is not None else 0.0
        total_invest = inv_geral + inv_prod
        roi = (venda_apurada / total_invest) if total_invest > 0 else 0
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f'<div class="metric-card"><div class="metric-label">Investimento total</div><div class="metric-value">{money(total_invest)}</div></div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="metric-card"><div class="metric-label">Sell In</div><div class="metric-value">{money(inv_si)}</div></div>', unsafe_allow_html=True)
        c3.markdown(f'<div class="metric-card"><div class="metric-label">Sell Out</div><div class="metric-value">{money(inv_so)}</div></div>', unsafe_allow_html=True)
        c4.markdown(f'<div class="metric-card"><div class="metric-label">ROI gerencial</div><div class="metric-value">{roi:,.2f}x</div></div>'.replace(',', 'X').replace('.', ',').replace('X','.'), unsafe_allow_html=True)
        c5, c6, c7 = st.columns(3)
        c5.metric('Compra apurada', money(compra_apurada))
        c6.metric('Venda apurada', money(venda_apurada))
        c7.metric('Negociações ativas', len(negociacoes[negociacoes.get('status','').eq('Ativo')]) if 'status' in negociacoes.columns else len(negociacoes))
        st.divider()
        if ap is not None and not ap.empty:
            rank = ap.groupby('Fabricante/Distribuidor', as_index=False)['Valor Investimento a Receber'].sum().sort_values('Valor Investimento a Receber', ascending=False).head(15)
            st.markdown('### Ranking por investimento geral')
            st.bar_chart(rank.set_index('Fabricante/Distribuidor'))
        if ap_prod is not None and not ap_prod.empty:
            st.markdown('### Produtos com maior investimento')
            top_prod = ap_prod.sort_values('Investimento', ascending=False).head(20)
            cols = [c for c in ['Fabricante/Distribuidor','Produto','Meta Sell Out','Venda Realizada','Valor Vendido','Investimento Sell Out','Investimento'] if c in top_prod.columns]
            mostrar_df_moeda(top_prod[cols], moedas=['Valor Vendido','Investimento Sell Out','Investimento'])

elif menu == 'Controle Financeiro':
    st.subheader('Controle Financeiro das Negociações')
    st.info('Registre recebimentos, acompanhe saldo pendente e mantenha o conta corrente atualizado por negociação.')
    compras = load_table('compras')
    vendas = load_table('vendas')
    negociacoes = load_table('negociacoes')
    if negociacoes.empty:
        st.warning('Nenhuma negociação cadastrada.')
    else:
        opcoes, mapa = listar_negociacoes_label()
        escolha = st.selectbox('Selecionar negociação', opcoes, key='controle_financeiro_neg')
        neg = mapa[escolha]
        neg_id = int(neg.get('id'))
        data_ini = pd.to_datetime(neg.get('data_inicio'), errors='coerce').date()
        data_fim = pd.to_datetime(neg.get('data_fim'), errors='coerce').date()
        neg_filtro = negociacoes[negociacoes['id'].astype(str) == str(neg_id)].copy()
        ap_resumo = calcular_apuracao(compras, neg_filtro, data_ini, data_fim)
        ap_produtos = calcular_apuracao_produtos(compras, neg_filtro, data_ini, data_fim, vendas)
        inv_geral = float(pd.to_numeric(ap_resumo.get('Valor Investimento a Receber', pd.Series(dtype=float)), errors='coerce').fillna(0).sum()) if ap_resumo is not None and not ap_resumo.empty else 0.0
        inv_prod = float(pd.to_numeric(ap_produtos.get('Investimento', pd.Series(dtype=float)), errors='coerce').fillna(0).sum()) if ap_produtos is not None and not ap_produtos.empty else 0.0
        investimento = inv_prod if inv_prod else inv_geral
        recebido = total_recebido_negociacao(neg_id)
        saldo = investimento - recebido
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f'<div class="metric-card"><div class="metric-label">Investimento apurado</div><div class="metric-value">{money(investimento)}</div></div>', unsafe_allow_html=True)
        c2.markdown(f'<div class="metric-card"><div class="metric-label">Recebido</div><div class="metric-value">{money(recebido)}</div></div>', unsafe_allow_html=True)
        c3.markdown(f'<div class="metric-card"><div class="metric-label">Saldo pendente</div><div class="metric-value">{money(saldo)}</div></div>', unsafe_allow_html=True)
        c4.markdown(f'<div class="metric-card"><div class="metric-label">Status</div><div class="metric-value">{str(neg.get("status", ""))}</div></div>', unsafe_allow_html=True)
        st.markdown('### Registrar recebimento')
        with st.form('form_recebimento_v10'):
            r1, r2, r3 = st.columns([1,1,1.5])
            with r1:
                data_recebimento = st.date_input('Data do recebimento', value=date.today())
            with r2:
                valor_recebido = st.number_input('Valor recebido (R$)', min_value=0.0, step=100.0, format='%.2f')
            with r3:
                forma_recebimento = st.selectbox('Forma/Documento', ['Depósito', 'Crédito em conta', 'Desconto em duplicata', 'Bonificação', 'Nota de crédito', 'Outro'])
            obs_rec = st.text_area('Observação financeira')
            salvar_rec = st.form_submit_button('Salvar recebimento', use_container_width=True)
            if salvar_rec:
                if valor_recebido <= 0:
                    st.warning('Informe um valor recebido maior que zero.')
                else:
                    with connect() as con:
                        con.execute('INSERT INTO recebimentos_negociacao (negociacao_id, data_recebimento, valor_recebido, forma_recebimento, usuario, observacao) VALUES (?, ?, ?, ?, ?, ?)', (neg_id, str(data_recebimento), float(valor_recebido), forma_recebimento, usuario_atual(), obs_rec))
                        registrar_historico(con, neg_id, str(neg.get('codigo_negociacao','')), 'Recebimento financeiro', '', money(valor_recebido), usuario=usuario_atual(), observacao=forma_recebimento)
                        con.commit()
                    st.success('Recebimento registrado com sucesso. Recarregue a tela para atualizar o saldo.')
        rec = load_table_safe('recebimentos_negociacao')
        if not rec.empty:
            view = rec[rec['negociacao_id'].astype(str) == str(neg_id)].copy()
            if not view.empty:
                view['valor_recebido'] = to_numero(view['valor_recebido']).fillna(0)
                show = view.sort_values('id', ascending=False).copy()
                show['valor_recebido'] = show['valor_recebido'].map(money)
                st.markdown('### Histórico de recebimentos')
                st.dataframe(show, use_container_width=True)

elif menu == 'Prestação de Contas':
    st.subheader('Prestação de Contas')
    st.info('Monte o dossiê da negociação para envio ao laboratório/distribuidor, com resumo, apuração, documentos e status financeiro.')
    compras = load_table('compras')
    vendas = load_table('vendas')
    negociacoes = load_table('negociacoes')
    if negociacoes.empty:
        st.warning('Nenhuma negociação cadastrada.')
    else:
        opcoes, mapa = listar_negociacoes_label()
        escolha = st.selectbox('Selecionar negociação', opcoes, key='prestacao_neg')
        neg = mapa[escolha]
        neg_id = int(neg.get('id'))
        data_ini = pd.to_datetime(neg.get('data_inicio'), errors='coerce').date()
        data_fim = pd.to_datetime(neg.get('data_fim'), errors='coerce').date()
        neg_filtro = negociacoes[negociacoes['id'].astype(str) == str(neg_id)].copy()
        ap_resumo = calcular_apuracao(compras, neg_filtro, data_ini, data_fim)
        ap_produtos = calcular_apuracao_produtos(compras, neg_filtro, data_ini, data_fim, vendas)
        docs = load_table_safe('documentos_negociacao')
        qtd_docs = len(docs[docs['negociacao_id'].astype(str)==str(neg_id)]) if not docs.empty else 0
        investimento = 0.0
        if ap_produtos is not None and not ap_produtos.empty:
            investimento = float(pd.to_numeric(ap_produtos.get('Investimento', pd.Series(dtype=float)), errors='coerce').fillna(0).sum())
        if investimento == 0 and ap_resumo is not None and not ap_resumo.empty:
            investimento = float(pd.to_numeric(ap_resumo.get('Valor Investimento a Receber', pd.Series(dtype=float)), errors='coerce').fillna(0).sum())
        recebido = total_recebido_negociacao(neg_id)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric('Documentos anexados', qtd_docs)
        c2.metric('Produtos no dossiê', len(ap_produtos) if ap_produtos is not None else 0)
        c3.metric('Investimento apurado', money(investimento))
        c4.metric('Saldo pendente', money(investimento - recebido))
        st.markdown('### Checklist')
        st.checkbox('Proposta comercial anexada', value=qtd_docs > 0, disabled=True)
        st.checkbox('Apuração executada', value=(ap_resumo is not None and not ap_resumo.empty) or (ap_produtos is not None and not ap_produtos.empty), disabled=True)
        st.checkbox('PDF pronto para envio', value=True, disabled=True)
        try:
            pdf_bytes = gerar_pdf_relatorio_negociacao(neg, ap_resumo, ap_produtos, 'Relatório completo', data_ini, data_fim)
            nome_arq = f"prestacao_contas_{str(neg.get('codigo_curto') or neg.get('codigo_negociacao') or neg_id).replace('/', '-')}.pdf"
            st.download_button('📄 Baixar prestação de contas em PDF', pdf_bytes, file_name=nome_arq, mime='application/pdf', use_container_width=True)
        except Exception as e:
            st.error(f'Não foi possível gerar o PDF: {e}')
        if st.button('Registrar etapa: Prestação de contas enviada', use_container_width=True):
            registrar_workflow(neg_id, 'Prestação de contas', 'Concluída', usuario_atual(), 'Prestação de contas gerada/enviada pela versão 10.0')
            st.success('Etapa registrada no workflow.')

elif menu == 'Inteligência Analítica':
    st.subheader('Inteligência Analítica')
    st.info('Insights automáticos com base nas negociações, apurações e saldos financeiros.')
    compras, vendas, negociacoes, ap, ap_prod = carregar_visao_executiva()
    if negociacoes is None or negociacoes.empty:
        st.warning('Sem negociações para analisar.')
    else:
        cc = consolidar_conta_corrente(ap, ap_prod)
        total_pendente = float(cc['Saldo a Receber'].sum()) if not cc.empty and 'Saldo a Receber' in cc.columns else 0.0
        total_inv = float(cc['Investimento Total a Receber'].sum()) if not cc.empty and 'Investimento Total a Receber' in cc.columns else 0.0
        qtd_neg = len(negociacoes)
        c1, c2, c3 = st.columns(3)
        c1.metric('Negociações analisadas', qtd_neg)
        c2.metric('Investimento apurado', money(total_inv))
        c3.metric('Saldo pendente', money(total_pendente))
        st.markdown('### Alertas automáticos')
        if total_pendente > 0:
            st.warning(f'Existe saldo pendente estimado de {money(total_pendente)} para acompanhamento financeiro.')
        if ap_prod is not None and not ap_prod.empty and 'Meta Sell Out' in ap_prod.columns and 'Venda Realizada' in ap_prod.columns:
            tmp = ap_prod.copy()
            tmp['Meta Sell Out'] = to_numero(tmp['Meta Sell Out']).fillna(0)
            tmp['Venda Realizada'] = to_numero(tmp['Venda Realizada']).fillna(0)
            abaixo = tmp[(tmp['Meta Sell Out'] > 0) & (tmp['Venda Realizada'] < tmp['Meta Sell Out'])].copy()
            if not abaixo.empty:
                st.error(f'{len(abaixo)} produto(s) estão abaixo da meta de Sell Out.')
                cols = [c for c in ['Fabricante/Distribuidor','Produto','Meta Sell Out','Venda Realizada','Valor Vendido','Investimento'] if c in abaixo.columns]
                mostrar_df_moeda(abaixo[cols].head(30), moedas=['Valor Vendido','Investimento'])
            else:
                st.success('Nenhum produto abaixo da meta de Sell Out nas apurações carregadas.')
        if not cc.empty:
            st.markdown('### Ranking de saldo por fabricante/distribuidor')
            cols = [c for c in ['Fabricante/Distribuidor','Investimento Total a Receber','Recebido','Saldo a Receber','Negociações','Produtos'] if c in cc.columns]
            mostrar_df_moeda(cc[cols].head(20), moedas=['Investimento Total a Receber','Recebido','Saldo a Receber'])


elif menu == 'Parâmetros Financeiros':
    st.subheader('Parâmetros Financeiros')
    st.info('Cadastre, edite, inative ou reative os tipos usados nos lançamentos de crédito a receber. Tipos ativos aparecem automaticamente no campo "Tipo do crédito" do Extrato Financeiro.')

    tab_credito, tab_tipo_neg, tab_orientacao = st.tabs(['Tipos de Crédito', 'Tipos de Negociação', 'Como usar'])

    with tab_credito:
        st.markdown('### Cadastrar novo tipo de crédito')
        with st.form('form_novo_tipo_credito_parametros'):
            c1, c2 = st.columns([3, 1])
            novo_tipo = c1.text_input('Descrição do tipo de crédito', placeholder='Ex.: Verba de mídia, Ponta de gôndola, Campanha especial')
            c2.selectbox('Status', ['Ativo'], disabled=True)
            salvar_tipo = st.form_submit_button('Cadastrar tipo de crédito', use_container_width=True)
            if salvar_tipo:
                ok, msg = salvar_tipo_credito(novo_tipo)
                if ok:
                    st.success(msg)
                    safe_rerun()
                else:
                    st.warning(msg)

        st.markdown('### Tipos cadastrados')
        tipos_df = listar_tipos_credito_todos()
        if tipos_df.empty:
            st.warning('Nenhum tipo cadastrado.')
        else:
            exibir = tipos_df.rename(columns={'nome':'Tipo de Crédito','usuario':'Último usuário','criado_em':'Criado em'})[['id','Tipo de Crédito','Status','Último usuário','Criado em']]
            st.dataframe(exibir, use_container_width=True, hide_index=True)

            st.markdown('### Editar / inativar')
            labels = [f"{int(r['id'])} | {r['nome']} | {r['Status']}" for _, r in tipos_df.iterrows()]
            mapa_tipos = {f"{int(r['id'])} | {r['nome']} | {r['Status']}": r for _, r in tipos_df.iterrows()}
            selecionado = st.selectbox('Selecione o tipo de crédito', labels, key='param_tipo_credito_select')
            row_tipo = mapa_tipos.get(selecionado)
            if row_tipo is not None:
                with st.form('form_editar_tipo_credito_parametros'):
                    e1, e2 = st.columns([3, 1])
                    nome_edit = e1.text_input('Descrição', value=str(row_tipo.get('nome','')))
                    status_edit = e2.selectbox('Status', ['Ativo','Inativo'], index=0 if int(row_tipo.get('ativo',1) or 0)==1 else 1)
                    col_a, col_b, col_c = st.columns(3)
                    salvar_edit = col_a.form_submit_button('Salvar alteração', use_container_width=True)
                    inativar = col_b.form_submit_button('Inativar / excluir da lista', use_container_width=True)
                    reativar = col_c.form_submit_button('Reativar', use_container_width=True)
                    if salvar_edit:
                        ok, msg = atualizar_tipo_credito_por_id(int(row_tipo['id']), nome_edit, 1 if status_edit == 'Ativo' else 0)
                        if ok:
                            st.success(msg)
                            safe_rerun()
                        else:
                            st.warning(msg)
                    if inativar:
                        ok, msg = inativar_tipo_credito_por_id(int(row_tipo['id']))
                        if ok:
                            st.success(msg)
                            safe_rerun()
                        else:
                            st.warning(msg)
                    if reativar:
                        ok, msg = reativar_tipo_credito_por_id(int(row_tipo['id']))
                        if ok:
                            st.success(msg)
                            safe_rerun()
                        else:
                            st.warning(msg)


    with tab_tipo_neg:
        st.markdown('### Tipos de negociação')
        st.caption('Cadastre novos tipos como Trade Marketing, Evento, Patrocínio ou Contrato. Tipos sem apuração geram crédito financeiro direto e aparecem no extrato/conta corrente sem consultar compras/vendas.')
        tipos_neg_df = listar_tipos_negociacao_parametros()
        if not tipos_neg_df.empty:
            vis = tipos_neg_df.copy()
            for c in ['ativo','controla_produtos','faz_apuracao','gera_financeiro','utiliza_faixas','utiliza_metas']:
                if c in vis.columns:
                    vis[c] = vis[c].fillna(0).astype(int).map({1:'Sim',0:'Não'})
            cols = [c for c in ['nome','ativo','controla_produtos','faz_apuracao','gera_financeiro','utiliza_faixas','utiliza_metas','observacao'] if c in vis.columns]
            st.dataframe(vis[cols].rename(columns={'nome':'Tipo','ativo':'Ativo','controla_produtos':'Produtos','faz_apuracao':'Apuração','gera_financeiro':'Financeiro','utiliza_faixas':'Faixas','utiliza_metas':'Metas','observacao':'Observação'}), use_container_width=True, hide_index=True)
        with st.form('form_tipo_negociacao_parametro'):
            c1, c2, c3 = st.columns([2,1,1])
            nome_tipo_neg = c1.text_input('Nome do tipo de negociação', placeholder='Ex.: Trade Marketing, Campanha Nacional, Evento, Mídia Digital')
            ativo_tipo_neg = c2.selectbox('Status', ['Ativo','Inativo'])
            gera_fin_tipo = c3.checkbox('Gera financeiro', value=True)
            c4, c5, c6, c7 = st.columns(4)
            controla_prod = c4.checkbox('Controla produtos', value=False)
            faz_apur = c5.checkbox('Faz apuração de compra/venda', value=False)
            utiliza_faixas_tipo = c6.checkbox('Utiliza faixas', value=False)
            utiliza_metas_tipo = c7.checkbox('Utiliza metas', value=False)
            obs_tipo_neg = st.text_area('Observação do tipo', placeholder='Explique quando usar este tipo.')
            if st.form_submit_button('Salvar tipo de negociação', use_container_width=True):
                ok, msg = salvar_tipo_negociacao_parametro(nome_tipo_neg, controla_prod, faz_apur, gera_fin_tipo, utiliza_faixas_tipo, utiliza_metas_tipo, 1 if ativo_tipo_neg == 'Ativo' else 0, obs_tipo_neg)
                if ok:
                    st.success(msg)
                    safe_rerun()
                else:
                    st.warning(msg)
        tipos_ativos_para_inativar = listar_tipos_negociacao_ativos()
        if tipos_ativos_para_inativar:
            tipo_inativar = st.selectbox('Inativar tipo de negociação', tipos_ativos_para_inativar, key='inativar_tipo_neg_select')
            if st.button('Inativar tipo selecionado', use_container_width=True):
                ok, msg = inativar_tipo_negociacao_parametro(tipo_inativar)
                if ok:
                    st.success(msg)
                    safe_rerun()
                else:
                    st.warning(msg)

    with tab_orientacao:
        st.markdown("""
        **Onde o tipo aparece?**  
        Após cadastrar um tipo ativo, ele aparece em **Extrato Financeiro → Novo lançamento → Crédito a receber → Tipo do crédito**.

        **Exclusão segura:**  
        Para preservar o histórico financeiro, o sistema não apaga definitivamente tipos já cadastrados. O botão **Inativar / excluir da lista** remove o tipo das opções de lançamento, mas mantém os lançamentos antigos intactos.
        """)

elif menu == 'Relatórios':
    st.subheader('Relatórios em PDF por ação')
    st.info('Selecione a ação/negociação e gere um PDF apenas com os principais campos conforme o tipo de investimento.')
    compras = load_table('compras')
    vendas = load_table('vendas')
    negociacoes = load_table('negociacoes')

    if negociacoes.empty:
        st.warning('Nenhuma negociação cadastrada para gerar relatório.')
    else:
        neg_tmp = negociacoes.copy()
        neg_tmp['data_inicio_dt'] = pd.to_datetime(neg_tmp.get('data_inicio'), errors='coerce').dt.date
        neg_tmp['data_fim_dt'] = pd.to_datetime(neg_tmp.get('data_fim'), errors='coerce').dt.date
        opcoes = []
        mapa = {}
        for _, nrow in neg_tmp.sort_values(['data_inicio_dt', 'nome'], ascending=[False, True]).iterrows():
            label = f"{nrow.get('codigo_curto') or nrow.get('codigo_negociacao') or nrow.get('id')} | {nrow.get('nome','')} | {nrow.get('tipo_investimento','')} | {nrow.get('data_inicio_dt')} a {nrow.get('data_fim_dt')}"
            opcoes.append(label)
            mapa[label] = nrow
        escolha = st.selectbox('Selecionar ação/negociação', opcoes)
        neg = mapa.get(escolha)
        if neg is not None:
            data_ini = pd.to_datetime(neg.get('data_inicio'), errors='coerce').date()
            data_fim = pd.to_datetime(neg.get('data_fim'), errors='coerce').date()
            relatorios = ['Automático pela ação', 'Resumo executivo', 'Geral da negociação', 'Sell In - Compras', 'Sell Out - Vendas', 'Produtos abaixo da meta']
            tipo_relatorio = st.selectbox('Modelo do relatório', relatorios, index=0)
            st.caption(f"Período usado no relatório: {data_ini.strftime('%d/%m/%Y')} até {data_fim.strftime('%d/%m/%Y')}")

            neg_filtro = negociacoes[negociacoes['id'].astype(str) == str(neg.get('id'))].copy() if 'id' in negociacoes.columns else pd.DataFrame([neg])
            ap_resumo = calcular_apuracao(compras, neg_filtro, data_ini, data_fim)
            ap_produtos = calcular_apuracao_produtos(compras, neg_filtro, data_ini, data_fim, vendas)

            c1, c2, c3 = st.columns(3)
            qtd_prod = len(ap_produtos) if ap_produtos is not None else 0
            inv_prod = float(pd.to_numeric(ap_produtos.get('Investimento', pd.Series(dtype=float)), errors='coerce').fillna(0).sum()) if ap_produtos is not None and not ap_produtos.empty else 0.0
            inv_geral = float(pd.to_numeric(ap_resumo.get('Valor Investimento a Receber', pd.Series(dtype=float)), errors='coerce').fillna(0).sum()) if ap_resumo is not None and not ap_resumo.empty else 0.0
            inv_total_rel = inv_prod if inv_prod else inv_geral
            c1.metric('Produtos no relatório', qtd_prod)
            c2.metric('Negociações no relatório', len(ap_resumo) if ap_resumo is not None else 0)
            c3.metric('Investimento Total', money(inv_total_rel))

            if ap_produtos is None or ap_produtos.empty:
                st.info('Esta ação não possui produtos vinculados. O PDF será gerado com os campos principais do cabeçalho e da apuração geral da negociação.')
                resumo_cols = [c for c in ['Código Curto','Fabricante/Distribuidor','Tipo Negociação','% Investimento','Meta de Compra','Compra Realizada','% Atingimento Meta','Valor Investimento a Receber','Observação'] if c in ap_resumo.columns]
                if resumo_cols:
                    st.dataframe(ap_resumo[resumo_cols].head(50), use_container_width=True)
            else:
                preview_cols = [c for c in ['Produto','Tipo Investimento','Meta Sell In','Compra Realizada','Meta Sell Out','Venda Realizada','Valor Vendido','Investimento Sell In','Investimento Sell Out','Investimento'] if c in ap_produtos.columns]
                st.dataframe(ap_produtos[preview_cols].head(50), use_container_width=True)

            try:
                pdf_bytes = gerar_pdf_relatorio_negociacao(neg, ap_resumo, ap_produtos, tipo_relatorio, data_ini, data_fim)
                nome_arq = f"relatorio_negociacao_{str(neg.get('codigo_curto') or neg.get('codigo_negociacao') or neg.get('id')).replace('/', '-')}.pdf"
                st.download_button('📄 Baixar relatório em PDF', pdf_bytes, file_name=nome_arq, mime='application/pdf', use_container_width=True)
            except Exception as e:
                msg = str(e)
                if 'reportlab' in msg.lower():
                    st.error('Não foi possível gerar o PDF: dependência ReportLab não instalada.')
                    st.code('python -m pip install -r requirements.txt', language='bash')
                    st.caption('Também é possível executar o arquivo Instalar_Dependencias.bat dentro da pasta do projeto e reiniciar o sistema.')
                else:
                    st.error(f'Não foi possível gerar o PDF: {e}')
                    st.caption('Se aparecer dependência ausente, execute: python -m pip install -r requirements.txt')


elif menu == 'Auditoria':
    st.subheader('Auditoria das negociações')
    st.info('Aqui aparece quem lançou, alterou ou excluiu cada negociação, com data/hora, antes/depois e observação.')
    with connect() as con:
        hist = pd.read_sql_query('''
            SELECT
                h.data_hora AS 'Data/Hora',
                h.usuario AS 'Usuário',
                h.codigo_negociacao AS 'Código',
                COALESCE(n.nome, '') AS 'Fabricante/Distribuidor',
                h.campo AS 'Ação/Campo',
                h.valor_anterior AS 'Antes',
                h.valor_novo AS 'Depois',
                h.observacao AS 'Observação'
            FROM historico_negociacoes h
            LEFT JOIN negociacoes n ON n.id = h.negociacao_id
            ORDER BY h.id DESC
        ''', con)
        neg_aud = pd.read_sql_query('''
            SELECT codigo_negociacao AS 'Código', nome AS 'Fabricante/Distribuidor', tipo AS 'Apurar por', tipo_negociacao AS 'Tipo Negociação', status AS 'Status', criado_por AS 'Lançado por', criado_em AS 'Lançado em', atualizado_por AS 'Última alteração por', atualizado_em AS 'Última alteração em', excluido_por AS 'Excluído por', excluido_em AS 'Excluído em'
            FROM negociacoes
            ORDER BY id DESC
        ''', con)
    tab_a1, tab_a2 = st.tabs(['Histórico completo', 'Resumo por negociação'])
    with tab_a1:
        if hist.empty:
            st.info('Ainda não existe histórico registrado.')
        else:
            usuario_filtro = st.multiselect('Filtrar usuário', sorted([x for x in hist['Usuário'].dropna().unique() if str(x).strip()]))
            acao_filtro = st.multiselect('Filtrar ação/campo', sorted([x for x in hist['Ação/Campo'].dropna().unique() if str(x).strip()]))
            show_hist = hist.copy()
            if usuario_filtro:
                show_hist = show_hist[show_hist['Usuário'].isin(usuario_filtro)]
            if acao_filtro:
                show_hist = show_hist[show_hist['Ação/Campo'].isin(acao_filtro)]
            st.dataframe(show_hist, use_container_width=True)
            st.download_button('Baixar auditoria em CSV', show_hist.to_csv(index=False, sep=';').encode('utf-8-sig'), file_name='auditoria_negociacoes.csv')
    with tab_a2:
        st.dataframe(neg_aud, use_container_width=True)

elif menu == 'Base de dados':
    st.subheader('Base de dados e conexão PostgreSQL')
    st.info('Configure e salve aqui a conexão do banco. Essa configuração será usada pelas rotinas de entradas, vendas e atualização automática. No Streamlit Cloud, para persistência permanente após reiniciar/deploy, também cadastre os mesmos dados em Settings → Secrets.')

    st.markdown('### Configuração salva')
    col_status1, col_status2, col_status3 = st.columns(3)
    with col_status1:
        st.metric('Host salvo', get_config('auto_host', 'Não configurado') or 'Não configurado')
    with col_status2:
        st.metric('Banco salvo', get_config('auto_database', 'Não configurado') or 'Não configurado')
    with col_status3:
        senha_salva = bool(get_config('auto_password', ''))
        st.metric('Senha', 'Salva' if senha_salva else 'Não salva')

    with st.form('form_config_banco_principal'):
        st.markdown('### Salvar configuração do banco')
        c1, c2, c3 = st.columns(3)
        with c1:
            host_cfg = st.text_input('Host do banco', value=get_config('auto_host', DEFAULT_DB_CONFIG['host']), key='base_host_cfg')
            database_cfg = st.text_input('Banco de dados', value=get_config('auto_database', DEFAULT_DB_CONFIG['database']), key='base_database_cfg')
        with c2:
            port_cfg = st.text_input('Porta', value=get_config('auto_port', DEFAULT_DB_CONFIG['port']), key='base_port_cfg')
            user_cfg = st.text_input('Usuário', value=get_config('auto_user', DEFAULT_DB_CONFIG['user']), key='base_user_cfg')
        with c3:
            password_cfg = st.text_input('Senha', value=get_config('auto_password', DEFAULT_DB_CONFIG['password']), type='password', key='base_password_cfg')
            habilitado_cfg = st.checkbox('Ativar atualização automática local', value=get_config('auto_enabled', '0') == '1', key='base_auto_enabled_cfg')
        cbtn1, cbtn2 = st.columns([1, 1])
        with cbtn1:
            salvar_cfg = st.form_submit_button('💾 Salvar configuração do banco')
        with cbtn2:
            testar_cfg = st.form_submit_button('🔌 Testar conexão')

    if salvar_cfg:
        erros = validar_config_banco(host_cfg, port_cfg, database_cfg, user_cfg, password_cfg)
        if erros:
            st.error('Corrija a configuração antes de salvar:\n' + '\n'.join(f'- {e}' for e in erros))
        else:
            salvar_configuracao_sql(host_cfg, port_cfg, database_cfg, user_cfg, password_cfg, habilitado_cfg, SCHEDULE_HOUR, SCHEDULE_MINUTE)
            limpar_cache_telas()
            st.success('Configuração do banco salva com sucesso. A senha ficará armazenada na base local do app. No Streamlit Cloud, use também Settings → Secrets para não perder após redeploy.')

    if testar_cfg:
        with st.spinner('Testando conexão com o PostgreSQL...'):
            ok, msg = testar_conexao_postgres(host_cfg, port_cfg, database_cfg, user_cfg, password_cfg)
        if ok:
            st.success(msg)
        else:
            st.error('Falha ao conectar:\n' + msg)

    st.markdown('### Caminhos utilizados')
    st.code(f'Dados persistentes: {DATA_DIR}\nBanco local: {DB_PATH}\nCache entradas: {CACHE_PARQUET}\nCache vendas: {CACHE_VENDAS_PARQUET}')
    st.markdown('### Modelo para Streamlit Cloud Secrets')
    st.code('[postgres]\nhost = "SEU_HOST"\nport = "5432"\ndatabase = "SEU_BANCO"\nuser = "SEU_USUARIO"\npassword = "SUA_SENHA"\n\n[app]\nmode = "cloud"', language='toml')

elif menu == 'Performance':
    st.subheader('Monitor de Performance')
    st.info('Tela técnica para identificar gargalos. As telas devem ler cache pronto; qualquer etapa lenta aparece no log abaixo.')
    c1, c2, c3 = st.columns(3)
    c1.metric('Meta troca de tela', '< 0,3s')
    c2.metric('Alerta configurado', f'> {PERF_WARN_SECONDS:.1f}s')
    c3.metric('Arquivo de log', 'performance_telas.log')
    if PERF_LOG_PATH.exists():
        try:
            perf = pd.read_csv(PERF_LOG_PATH, sep=';', header=None, names=['Data/Hora','Etapa','Segundos','Detalhe'])
            perf = perf.tail(500).sort_values('Data/Hora', ascending=False)
            st.dataframe(perf, use_container_width=True)
            st.download_button('Baixar log de performance', perf.to_csv(index=False, sep=';').encode('utf-8-sig'), file_name='performance_telas.csv')
            if st.button('Limpar log de performance'):
                try:
                    PERF_LOG_PATH.unlink()
                    st.success('Log limpo.')
                    safe_rerun()
                except Exception as e:
                    st.error(f'Não foi possível limpar o log: {e}')
        except Exception as e:
            st.error(f'Não foi possível ler o log de performance: {e}')
    else:
        st.success('Nenhum gargalo registrado acima do limite configurado.')

elif menu == 'Dashboard antigo':
    compras = load_table('compras')
    vendas = load_table('vendas')
    negociacoes = load_table('negociacoes')
    ap = calcular_apuracao(compras, negociacoes, date(2000,1,1), date(2099,12,31))
    total_compra = ap['Valor Compra Base'].sum() if not ap.empty else 0
    total_invest = ap['Valor Investimento a Receber'].sum() if not ap.empty else 0
    ativos = len(negociacoes[negociacoes['status'].eq('Ativo')]) if not negociacoes.empty else 0
    c1, c2, c3 = st.columns(3)
    c1.markdown(f'<div class="metric-card"><div class="metric-label">Compra base negociada</div><div class="metric-value">{money(total_compra)}</div></div>', unsafe_allow_html=True)
    c2.markdown(f'<div class="metric-card"><div class="metric-label">Investimento previsto</div><div class="metric-value">{money(total_invest)}</div></div>', unsafe_allow_html=True)
    c3.markdown(f'<div class="metric-card"><div class="metric-label">Negociações ativas</div><div class="metric-value">{ativos}</div></div>', unsafe_allow_html=True)
    st.divider()
    if not ap.empty:
        chart = ap.groupby('Fabricante/Distribuidor', as_index=False)['Valor Investimento a Receber'].sum().sort_values('Valor Investimento a Receber', ascending=False).head(15)
        st.bar_chart(chart.set_index('Fabricante/Distribuidor'))
    else:
        st.info('Cadastre uma negociação e importe compras para visualizar o dashboard.')

else:
    tab1, tab2 = st.tabs(['Negociações', 'Compras'])
    with tab1:
        ver_excluidas = st.checkbox('Mostrar negociações excluídas')
        neg = load_negociacoes_todas() if ver_excluidas else load_table('negociacoes')
        st.dataframe(neg, use_container_width=True)
    with tab2:
        compras = load_table('compras')
        st.dataframe(compras.tail(1000), use_container_width=True)



try:
    _sb_tela_segundos = time_module.perf_counter() - _sb_tela_inicio
    if _sb_tela_segundos >= PERF_WARN_SECONDS:
        registrar_performance('TELA', _sb_tela_segundos, menu)
    st.sidebar.caption(f'Tela carregada em {_sb_tela_segundos:.2f}s')
except Exception:
    pass

st.markdown('<div class="sb-footer">SB Farma | Projeto Negociação v14.5 Streamlit Cloud</div>', unsafe_allow_html=True)
