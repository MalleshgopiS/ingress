FROM us-central1-docker.pkg.dev/bespokelabs/nebula-devops-registry/nebula-devops:1.0.0

WORKDIR /workdir

COPY setup.sh /workdir/setup.sh
COPY solution.sh /workdir/solution.sh
COPY grader.py /workdir/grader.py
COPY task.yaml /workdir/task.yaml

RUN chmod +x /workdir/setup.sh
RUN chmod +x /workdir/solution.sh