FROM us-central1-docker.pkg.dev/bespokelabs/nebula-devops-registry/nebula-devops:1.0.0

ENV ALLOWED_NAMESPACES="default,ingress-system"

USER root

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl=7.81* \
    openssl \
    python3 && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /grader
RUN mkdir -p /mcp_server/tests

COPY setup.sh /setup.sh
COPY solution.sh /solution.sh
COPY grader.py /grader/grader.py
COPY grader.py /mcp_server/tests/grader.py

RUN chmod +x /setup.sh /solution.sh
RUN chmod 700 /grader