FROM us-central1-docker.pkg.dev/bespokelabs/nebula-devops-registry/nebula-devops:1.0.0

USER root

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    jq=1.6* \
    curl=7.81* && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /grader
RUN mkdir -p /mcp_server/tests

COPY setup.sh /setup.sh
COPY solution.sh /solution.sh
COPY grader.py /mcp_server/tests/grader.py

RUN chmod +x /setup.sh
RUN chmod +x /solution.sh

CMD ["/bin/bash", "/setup.sh"]