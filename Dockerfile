# Golem as one container. It holds the OpenRouter key and runs the kernel; it never runs generated code.
# Generated code runs in separate sandbox containers that Golem starts on the host's Docker through the
# mounted socket. Run it with scripts/golem-docker, which mounts the repository, the attachments, and a
# work directory at the same paths inside and outside, so the sandbox containers can mount them too.
FROM python:3.12-slim

COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*'

# openrouter-agent-sdk 0.8.0 needs openrouter 1.1.26; newer openrouter releases removed names it imports.
RUN pip install --no-cache-dir "openrouter-agent-sdk==0.8.0" "openrouter==1.1.26"

WORKDIR /opt/golem
COPY authority.json README.md ./
COPY golem ./golem
ENV PYTHONPATH=/opt/golem PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "golem"]
