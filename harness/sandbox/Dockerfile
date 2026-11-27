# Imagem do sandbox do REPL (spec v0, "Segurança e limites"; D4 e D12).
#
# Construção, a partir da raiz do repositório:
#   docker build -t harness-sandbox:dev -f harness/sandbox/Dockerfile .
#
# O endurecimento em tempo de execução (rede, somente leitura, capacidades, limites)
# fica no cliente do host (harness/sandbox/docker_sandbox.py), porque é o host que
# cria o contêiner. Esta imagem só define o conteúdo e o usuário sem privilégio.

# Base fixada por digest: a mesma imagem sempre, mesmo que a tag mude.
FROM python:3.12-bookworm@sha256:5560e9ab8709f459489e5b8aa696eda8a07ef821e14bb122be62d91234bfa98b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/mpl \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Usuário sem privilégio com uid e gid fixos, sem shell de login e sem diretório pessoal.
# O host usa o mesmo uid em `user=` ao criar o contêiner.
RUN groupadd --gid 10001 sandbox \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin sandbox

# Dependências instaladas com hashes e sem resolver nada extra (o lock já traz tudo).
COPY harness/sandbox/requirements.txt /tmp/requirements.txt
RUN python -m pip install --require-hashes --no-deps -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

WORKDIR /app

# Só o necessário do pacote: o runner, o protocolo, o cliente de ferramentas (`tools`) e a
# descrição do contexto (`context_info`). Nada de harness/host: o registro, as ferramentas e os
# perfis executam no host e não entram na imagem.
COPY harness/__init__.py /app/harness/__init__.py
COPY harness/sandbox/__init__.py /app/harness/sandbox/__init__.py
COPY harness/sandbox/protocol.py harness/sandbox/runner.py harness/sandbox/tools_client.py harness/sandbox/contexto.py harness/sandbox/rastreio.py /app/harness/sandbox/

# Diretórios de montagem. /dados recebe bind somente leitura; /saida e /tmp são tmpfs
# montados pelo host. Os arquivos ficam como root, então o usuário sem privilégio não
# consegue alterá-los.
RUN mkdir -p /dados /saida \
 && chown 10001:10001 /saida

# Remove o pip depois da instalação: não sobra nenhum `pip install` disponível em execução.
# Site-packages pertence ao root e fica somente leitura para o usuário do sandbox.
RUN python -m pip uninstall -y pip setuptools wheel

USER 10001:10001

ENTRYPOINT ["python", "-m", "harness.sandbox.runner", "--stdio"]
