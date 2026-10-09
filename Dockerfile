FROM python:3.12-slim

COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*'

RUN pip install --no-cache-dir "openrouter-agent-sdk==0.8.0" "openrouter==1.1.26"

WORKDIR /opt/golem
COPY authority.json README.md ./
COPY golem ./golem
ENV PYTHONPATH=/opt/golem PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "golem"]
