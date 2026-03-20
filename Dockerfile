FROM us-central1-docker.pkg.dev/bespokelabs/nebula-devops-registry/nebula-devops:1.1.0

# Mandatory environment variables
ENV DISPLAY_NUM=1
ENV COMPUTER_HEIGHT_PX=768
ENV COMPUTER_WIDTH_PX=1024

# Allow access to the ingress-system namespace only
ENV ALLOWED_NAMESPACES="ingress-system"

# Install dependencies (openssl for TLS cert generation in setup/solution scripts)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    skopeo \
    openssl \
    python3 && \
    rm -rf /var/lib/apt/lists/*

# Pull and store container images offline (nginx pod needs this without internet access)
RUN mkdir -p /images && \
    skopeo copy --override-os linux --override-arch amd64 \
    docker://nginx:1.27-alpine \
    oci-archive:/images/nginx_1.27-alpine.oci.tar:nginx:1.27-alpine

RUN skopeo copy --override-os linux --override-arch amd64 \
    docker://alpine/k8s:1.30.4 \
    oci-archive:/images/alpine_k8s_1.30.4.oci.tar:alpine/k8s:1.30.4

RUN mkdir -p /grader
RUN mkdir -p /mcp_server/tests

COPY setup.sh /setup.sh
COPY solution.sh /solution.sh
COPY grader.py /grader/grader.py

RUN chmod 700 /setup.sh && chmod 700 /solution.sh
RUN chmod 700 /grader
